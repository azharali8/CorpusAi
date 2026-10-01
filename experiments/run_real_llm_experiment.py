"""
CorpusAI Real Local LLM Selector Generation Experiment.

Executes real inference using Ollama (qwen2.5-coder:3b) with strict experimental separation:
- Generation Split: 3 representative pages (page01_changed.html - page03_changed.html)
- Validation Split: 2 unseen pages (page04_changed.html - page05_changed.html)
- Held-Out Split: 1 unseen test page (page06_changed.html)

Saves results to:
- results/real_llm_experiment.json
- results/real_llm_raw_response.txt
"""

import json
import os
import sys
import time
import yaml

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extractor import DeterministicExtractor
from src.rule_generator import OllamaRuleGenerator, SchemaValidationError, GenerationError
from src.selector_validator import SelectorValidator


def load_html_files(file_list):
    samples = []
    for fname in file_list:
        fpath = os.path.join(os.path.dirname(__file__), "..", "data", "modified_html", fname)
        with open(fpath, "r", encoding="utf-8") as f:
            samples.append((fname, f.read()))
    return samples


def run_experiment():
    print("=" * 60)
    print("CorpusAI Real Local LLM Selector Generation Experiment")
    print("=" * 60)

    gen_files = ["page01_changed.html", "page02_changed.html", "page03_changed.html"]
    val_files = ["page04_changed.html", "page05_changed.html"]
    held_out_files = ["page06_changed.html"]

    print(f"\nModel: qwen2.5-coder:3b")
    print(f"Generation pages ({len(gen_files)}): {gen_files}")
    print(f"Validation pages ({len(val_files)}): {val_files}")
    print(f"Held-out page    ({len(held_out_files)}): {held_out_files}")

    # Load HTML splits
    gen_samples = load_html_files(gen_files)
    val_samples = load_html_files(val_files)
    held_out_samples = load_html_files(held_out_files)

    gen_html_texts = [html for _, html in gen_samples]
    val_html_texts = [html for _, html in val_samples]
    held_out_name, held_out_html = held_out_samples[0]

    # Initialize generator
    generator = OllamaRuleGenerator(
        model=os.environ.get("CORPUSAI_MODEL", "qwen2.5-coder:3b"),
        temperature=0.0,
        seed=42,
    )

    print("\n[Step 1] Sending 3 representative pages to Ollama...")
    start_time = time.time()
    generation_failed = False
    candidate_rules = None
    schema_status = "UNKNOWN"
    failure_reason = None

    try:
        candidate_rules = generator.generate_rules(gen_html_texts)
        schema_status = "PASS"
    except GenerationError as e:
        generation_failed = True
        schema_status = "FAIL"
        failure_reason = str(e)
        print(f"\nGeneration / Schema Error: {failure_reason}")

    elapsed_time = round(time.time() - start_time, 3)
    print(f"Inference latency: {generator.last_latency_seconds}s (Total: {elapsed_time}s)")

    # Save raw response text
    results_dir = os.path.join(os.path.dirname(__file__), "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    raw_response_path = os.path.join(results_dir, "real_llm_raw_response.txt")
    with open(raw_response_path, "w", encoding="utf-8") as f:
        f.write(generator.last_raw_response or "<No raw response received>")
    print(f"Raw model response saved to: {os.path.relpath(raw_response_path)}")

    validation_results = {}
    held_out_results = {}
    field_accuracy_score = 0.0

    if not generation_failed and candidate_rules:
        print("\n[Step 2] Model-generated candidate rules:")
        for field, spec in candidate_rules.items():
            print(f"  {field:<8} -> {spec.get('selector')} (type: {spec.get('type')})")

        print(f"\nSchema validation: {schema_status}")

        print("\n[Step 3] Validating candidates on 2 unseen validation pages...")
        validator = SelectorValidator(threshold=0.90)
        all_passed_val = True

        for field, spec in candidate_rules.items():
            report = validator.validate_selector(val_html_texts, spec)
            pct = int(report["success_rate"] * 100)
            status_str = "ACCEPTED" if report["is_acceptable"] else "REJECTED"
            validation_results[field] = {
                "selector": spec.get("selector"),
                "type": spec.get("type"),
                "success_rate": report["success_rate"],
                "accepted": report["is_acceptable"],
            }
            if not report["is_acceptable"]:
                all_passed_val = False
            print(f"  {field:<8}: {pct}% -> {status_str}")

        print(f"\n[Step 4] Deterministic extraction on unseen held-out page ({held_out_name})...")
        extractor = DeterministicExtractor()
        held_out_extraction = extractor.extract(held_out_html, candidate_rules)

        # Load ground truth for evaluation only
        gt_path = os.path.join(os.path.dirname(__file__), "..", "data", "ground_truth", "modified_portal.yaml")
        ground_truth = {}
        if os.path.exists(gt_path):
            with open(gt_path, "r", encoding="utf-8") as f:
                gt_data = yaml.safe_load(f)
                ground_truth = gt_data.get("pages", {}).get(held_out_name, {})

        correct_count = 0
        total_fields = len(candidate_rules)

        for field, ext_res in held_out_extraction.items():
            ext_val = ext_res.get("value")
            is_success = ext_res.get("success", False)
            is_correct = False

            # Evaluation check
            if is_success and ext_val:
                if field == "body":
                    expected_sub = ground_truth.get("body_substring", "")
                    is_correct = expected_sub in ext_val if expected_sub else bool(ext_val)
                else:
                    expected_val = ground_truth.get(field, "")
                    is_correct = (expected_val == ext_val) if expected_val else bool(ext_val)

            if is_correct:
                correct_count += 1

            held_out_results[field] = {
                "selector": candidate_rules[field].get("selector"),
                "extracted_value": ext_val,
                "expected_value": ground_truth.get(field, ground_truth.get("body_substring")),
                "success": is_success,
                "correct": is_correct,
            }
            status_text = "CORRECT" if is_correct else "INCORRECT"
            print(f"  {field:<8}: {status_text} (extracted: {repr(ext_val)[:50]}...)")

        field_accuracy_score = round(correct_count / total_fields, 4) if total_fields > 0 else 0.0
        print(f"\nHeld-out field accuracy: {int(field_accuracy_score * 100)}% ({correct_count}/{total_fields} fields)")

    # Save structured experiment artifact
    experiment_record = {
        "experiment_id": "real_llm_selector_generation_v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": generator.model,
        "temperature": generator.temperature,
        "seed": generator.seed,
        "prompt_version": generator.prompt_version,
        "latency_seconds": generator.last_latency_seconds,
        "generation_pages": gen_files,
        "validation_pages": val_files,
        "held_out_pages": held_out_files,
        "schema_validation": schema_status,
        "candidate_selectors": candidate_rules,
        "validation_results": validation_results,
        "held_out_results": held_out_results,
        "field_accuracy": field_accuracy_score,
        "failure_reason": failure_reason,
    }

    json_result_path = os.path.join(results_dir, "real_llm_experiment.json")
    with open(json_result_path, "w", encoding="utf-8") as f:
        json.dump(experiment_record, f, indent=2)

    print(f"\nStructured experiment record saved to: {os.path.relpath(json_result_path)}")
    print("=" * 60)


if __name__ == "__main__":
    run_experiment()

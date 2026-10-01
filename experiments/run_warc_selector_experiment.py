"""
CorpusAI WARC-Based Selector Generation Experiment (Phase 4).

Demonstrates end-to-end CorpusAI pipeline using HTML extracted from a WARC archive
produced by the WebArticleCurator / ELTE-DH workflow.

Workflow:
  1. Validate WARC + compute SHA-256 provenance
  2. Extract all HTML records from WARC
  3. Split records: gen(3) / val(2) / held-out(1)
  4. Preprocess gen HTML for LLM inspection
  5. Call OllamaRuleGenerator.generate_rules() — AI proposes selectors
  6. Schema-validate the proposal
  7. Validate candidates on val HTML with SelectorValidator (≥90% threshold)
  8. Run DeterministicExtractor on held-out HTML
  9. Save results/warc_experiment_manifest.json with full provenance

Usage:
  python experiments/run_warc_selector_experiment.py --warc data/warc/synthetic_portal.warc
  python experiments/run_warc_selector_experiment.py --warc data/warc/synthetic_portal.warc --mock

The --mock flag substitutes MockRuleGenerator for offline testing.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import yaml

from src.extractor import DeterministicExtractor
from src.html_preprocessor import preprocess_html
from src.rule_generator import (
    MockRuleGenerator,
    OllamaRuleGenerator,
    SchemaValidationError,
    GenerationError,
    validate_rule_proposal,
)
from src.selector_validator import SelectorValidator
from src.warc_utils import (
    calculate_warc_sha256,
    extract_html_samples,
    split_warc_records,
    validate_warc_input,
)
from src.webarticlecurator_adapter import WebArticleCuratorAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_package_version(package_name: str) -> str:
    """Return installed version of a package without importing it."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True,
            text=True,
            shell=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


def _get_ollama_version() -> str:
    """Query Ollama version via API, gracefully returns 'unavailable' on failure."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/version", timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return data.get("version", "unknown")
    except Exception:
        return "unavailable"


def _get_corpusai_version() -> str:
    """Return version from pyproject.toml."""
    try:
        toml_path = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(toml_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.1.0"


# ---------------------------------------------------------------------------
# Main experiment
# ---------------------------------------------------------------------------

def run_warc_experiment(warc_path: str, use_mock: bool = False):
    print("=" * 65)
    print("CorpusAI WARC-Based Selector Generation Experiment (Phase 4)")
    print("=" * 65)

    # -----------------------------------------------------------------------
    # Step 1 — Validate WARC + compute SHA-256
    # -----------------------------------------------------------------------
    print(f"\n[Step 1] Validating WARC: {warc_path}")
    if not os.path.isfile(warc_path):
        print(f"  ERROR: WARC file not found: {warc_path}")
        sys.exit(1)

    warc_sha256 = calculate_warc_sha256(warc_path)
    print(f"  SHA-256: {warc_sha256}")

    validation_info = validate_warc_input(warc_path)
    if not validation_info["valid"]:
        print(f"  ERROR: WARC validation failed: {validation_info.get('error')}")
        sys.exit(1)

    print(
        f"  Valid: True | total_records={validation_info['total_records']} "
        f"| html_records={validation_info['html_records']} "
        f"| size={validation_info['file_size_bytes']} bytes"
    )

    # WebArticleCurator adapter info (non-blocking)
    adapter = WebArticleCuratorAdapter()
    wac_available = adapter.is_available()
    wac_version = adapter.get_version() if wac_available else "not installed"
    print(f"  webarticlecurator: v{wac_version}")

    # -----------------------------------------------------------------------
    # Step 2 — Extract HTML records from WARC
    # -----------------------------------------------------------------------
    print(f"\n[Step 2] Extracting HTML records from WARC...")
    all_records = extract_html_samples(warc_path)
    print(f"  Extracted {len(all_records)} HTML records")
    for rec in all_records:
        print(f"    {rec['url']}")

    # -----------------------------------------------------------------------
    # Step 3 — Split records deterministically
    # -----------------------------------------------------------------------
    print(f"\n[Step 3] Splitting records (gen=3 / val=2 / held-out=1)...")
    gen_records, val_records, held_out_records = split_warc_records(
        all_records, gen_count=3, val_count=2, test_count=1
    )

    gen_urls = [r["url"] for r in gen_records]
    val_urls = [r["url"] for r in val_records]
    held_out_urls = [r["url"] for r in held_out_records]

    print(f"  Generation:  {gen_urls}")
    print(f"  Validation:  {val_urls}")
    print(f"  Held-out:    {held_out_urls}")

    # -----------------------------------------------------------------------
    # Step 4 — Preprocess generation HTML
    # -----------------------------------------------------------------------
    print(f"\n[Step 4] Preprocessing {len(gen_records)} generation pages for LLM...")
    gen_html_texts = []
    for rec in gen_records:
        cleaned = preprocess_html(rec["html"])
        gen_html_texts.append(cleaned)
        print(f"  {rec['url']}: {len(cleaned)} chars after preprocessing")

    val_html_texts = [rec["html"] for rec in val_records]
    held_out_html = held_out_records[0]["html"]
    held_out_url = held_out_records[0]["url"]

    # -----------------------------------------------------------------------
    # Step 5 — Generate selector candidates (AI proposes)
    # -----------------------------------------------------------------------
    model_name = os.environ.get("CORPUSAI_MODEL", "qwen2.5-coder:3b")
    prompt_version = "v1.0"

    if use_mock:
        print(f"\n[Step 5] Using MockRuleGenerator (--mock flag set)")
        generator = MockRuleGenerator()
        prompt_version = "mock"
    else:
        print(f"\n[Step 5] Sending gen pages to Ollama model: {model_name}")
        generator = OllamaRuleGenerator(
            model=model_name,
            temperature=0.0,
            seed=42,
        )
        prompt_version = getattr(generator, "prompt_version", "v1.0")

    start_time = time.time()
    candidate_rules = None
    schema_status = "UNKNOWN"
    failure_reason = None
    generation_failed = False

    try:
        candidate_rules = generator.generate_rules(gen_html_texts)
        schema_status = "PASS"
        print(f"  Candidate selectors proposed successfully.")
    except (GenerationError, SchemaValidationError) as e:
        generation_failed = True
        schema_status = "FAIL"
        failure_reason = str(e)
        print(f"  Generation/Schema Error: {failure_reason}")

    latency = round(time.time() - start_time, 3)

    # Save raw response (real LLM only)
    results_dir = os.path.join(os.path.dirname(__file__), "..", "results")
    os.makedirs(results_dir, exist_ok=True)

    raw_response = getattr(generator, "last_raw_response", None)
    raw_response_path = os.path.join(results_dir, "warc_llm_raw_response.txt")
    if raw_response is not None:
        with open(raw_response_path, "w", encoding="utf-8") as f:
            f.write(raw_response)
        print(f"  Raw model response saved to: results/warc_llm_raw_response.txt")

    if generation_failed or candidate_rules is None:
        print("\n  Cannot continue experiment without valid candidate rules.")
        _save_manifest(
            results_dir=results_dir,
            warc_path=warc_path,
            warc_sha256=warc_sha256,
            validation_info=validation_info,
            wac_version=wac_version,
            model_name=model_name if not use_mock else "mock",
            prompt_version=prompt_version,
            latency=latency,
            gen_urls=gen_urls,
            val_urls=val_urls,
            held_out_urls=held_out_urls,
            schema_status=schema_status,
            candidate_rules=None,
            validation_results={},
            held_out_results={},
            field_accuracy=0.0,
            failure_reason=failure_reason,
        )
        return

    # -----------------------------------------------------------------------
    # Step 6 — Display proposed selectors
    # -----------------------------------------------------------------------
    print(f"\n[Step 6] Proposed candidate selectors (schema: {schema_status}):")
    for field, spec in candidate_rules.items():
        print(f"  {field:<8} -> {spec.get('selector')} (type: {spec.get('type')})")

    # -----------------------------------------------------------------------
    # Step 7 — Validate on val HTML (Validator verifies)
    # -----------------------------------------------------------------------
    print(f"\n[Step 7] Validating candidates on {len(val_records)} unseen val pages (threshold=90%)...")
    validator = SelectorValidator(threshold=0.90)
    validation_results = {}
    all_accepted = True

    for field, spec in candidate_rules.items():
        report = validator.validate_selector(val_html_texts, spec)
        pct = int(report["success_rate"] * 100)
        accepted = report["is_acceptable"]
        status_str = "ACCEPTED" if accepted else "REJECTED"
        if not accepted:
            all_accepted = False
        validation_results[field] = {
            "selector": spec.get("selector"),
            "type": spec.get("type"),
            "success_rate": report["success_rate"],
            "accepted": accepted,
        }
        print(f"  {field:<8}: {pct}% -> {status_str}")

    overall_validation = "ALL_ACCEPTED" if all_accepted else "SOME_REJECTED"
    print(f"\n  Validation outcome: {overall_validation}")

    # -----------------------------------------------------------------------
    # Step 8 — Deterministic extraction on held-out page (Code executes)
    # -----------------------------------------------------------------------
    print(f"\n[Step 8] Deterministic extraction on held-out page ({held_out_url})...")
    extractor = DeterministicExtractor()
    held_out_extraction = extractor.extract(held_out_html, candidate_rules)

    # Load ground truth for evaluation only (never given to generator)
    gt_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "ground_truth", "modified_portal.yaml"
    )
    ground_truth = {}
    if os.path.exists(gt_path):
        with open(gt_path, "r", encoding="utf-8") as f:
            gt_data = yaml.safe_load(f)
            # WARC fixture uses page06 which maps to page06_changed.html in ground truth
            ground_truth = gt_data.get("pages", {}).get("page06_changed.html", {})

    correct_count = 0
    total_fields = len(candidate_rules)
    held_out_results = {}

    for field, ext_res in held_out_extraction.items():
        ext_val = ext_res.get("value")
        is_success = ext_res.get("success", False)
        is_correct = False

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
            "type": candidate_rules[field].get("type"),
            "extracted_value": ext_val,
            "expected_value": ground_truth.get(field, ground_truth.get("body_substring")),
            "success": is_success,
            "correct": is_correct,
        }

        status_text = "[OK] CORRECT" if is_correct else "[FAIL] INCORRECT"
        print(f"  {field:<8}: {status_text} (extracted: {repr(ext_val)[:60]})")

    field_accuracy = round(correct_count / total_fields, 4) if total_fields > 0 else 0.0
    print(
        f"\n  Held-out field accuracy: {int(field_accuracy * 100)}% "
        f"({correct_count}/{total_fields} fields)"
    )

    # -----------------------------------------------------------------------
    # Step 9 — Save manifest
    # -----------------------------------------------------------------------
    _save_manifest(
        results_dir=results_dir,
        warc_path=warc_path,
        warc_sha256=warc_sha256,
        validation_info=validation_info,
        wac_version=wac_version,
        model_name=model_name if not use_mock else "mock",
        prompt_version=prompt_version,
        latency=latency,
        gen_urls=gen_urls,
        val_urls=val_urls,
        held_out_urls=held_out_urls,
        schema_status=schema_status,
        candidate_rules=candidate_rules,
        validation_results=validation_results,
        held_out_results=held_out_results,
        field_accuracy=field_accuracy,
        failure_reason=failure_reason,
    )

    print("\n" + "=" * 65)
    print("Experiment complete.")
    print("=" * 65)


def _save_manifest(
    results_dir, warc_path, warc_sha256, validation_info, wac_version,
    model_name, prompt_version, latency, gen_urls, val_urls, held_out_urls,
    schema_status, candidate_rules, validation_results, held_out_results,
    field_accuracy, failure_reason,
):
    """Persist the full provenance manifest to results/warc_experiment_manifest.json."""
    import platform as _platform
    try:
        import warcio
        warcio_version = getattr(warcio, "__version__", _get_package_version("warcio"))
    except Exception:
        warcio_version = _get_package_version("warcio")

    manifest = {
        "experiment_id": "warc_selector_generation_phase4",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpusai_version": _get_corpusai_version(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "webarticlecurator_version": wac_version,
        "warcio_version": warcio_version,
        "model": model_name,
        "ollama_version": _get_ollama_version() if model_name != "mock" else "N/A",
        "prompt_version": prompt_version,
        "warc": {
            "filename": os.path.basename(warc_path),
            "path": os.path.abspath(warc_path),
            "sha256": warc_sha256,
            "file_size_bytes": validation_info.get("file_size_bytes", 0),
            "total_records": validation_info.get("total_records", 0),
            "html_records": validation_info.get("html_records", 0),
        },
        "splits": {
            "generation_urls": gen_urls,
            "validation_urls": val_urls,
            "held_out_urls": held_out_urls,
        },
        "schema_validation": schema_status,
        "latency_seconds": latency,
        "candidate_selectors": candidate_rules,
        "validation_results": validation_results,
        "held_out_results": held_out_results,
        "field_accuracy": field_accuracy,
        "failure_reason": failure_reason,
    }

    manifest_path = os.path.join(results_dir, "warc_experiment_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n[Step 9] Manifest saved to: results/warc_experiment_manifest.json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CorpusAI Phase 4 — WARC-Based Selector Generation Experiment"
    )
    parser.add_argument(
        "--warc",
        default=os.path.join("data", "warc", "synthetic_portal.warc"),
        help="Path to input WARC file (default: data/warc/synthetic_portal.warc)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Use MockRuleGenerator instead of Ollama (offline testing)",
    )
    args = parser.parse_args()

    run_warc_experiment(warc_path=args.warc, use_mock=args.mock)

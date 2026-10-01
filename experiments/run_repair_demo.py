"""
CorpusAI Layout-Change Experiment Demonstration.

Demonstrates failure detection, candidate generation via MockRuleGenerator,
statistical validation gatekeeping, human approval boundary, versioned history tracking,
and successful restoration of deterministic extraction.
"""

import glob
import json
import os
import sys
import yaml

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extractor import DeterministicExtractor
from src.rule_repair import RuleRepairManager
from src.rule_generator import MockRuleGenerator
from src.selector_validator import SelectorValidator


def run_experiment():
    print("=" * 60)
    print("CorpusAI Layout-Change Experiment Demonstration")
    print("=" * 60)

    # 1. Load baseline configuration
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "selectors.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        baseline_config = yaml.safe_load(f)

    baseline_selectors = baseline_config["selectors"]

    print("\nBaseline selectors:")
    for field, rule in baseline_selectors.items():
        sel = rule.get("selector", "") if isinstance(rule, dict) else str(rule)
        print(f"  {field:<8} -> {sel}")

    # 2. Load modified HTML fixtures
    mod_dir = os.path.join(os.path.dirname(__file__), "..", "data", "modified_html")
    html_files = sorted(glob.glob(os.path.join(mod_dir, "*.html")))
    html_samples = []
    for p in html_files:
        with open(p, "r", encoding="utf-8") as f:
            html_samples.append(f.read())

    print(f"\nTesting baseline selectors on {len(html_samples)} redesigned pages...")

    extractor = DeterministicExtractor()
    validator = SelectorValidator(threshold=0.90)
    repair_manager = RuleRepairManager(validator=validator, generator=MockRuleGenerator(), extractor=extractor)

    # 3. Test baseline extraction on redesigned pages
    before_status = {}
    for field, rule in baseline_selectors.items():
        rep = validator.validate_selector(html_samples, rule)
        status_str = "SUCCESS" if rep["is_acceptable"] else "FAILED"
        before_status[field] = rep["is_acceptable"]
        print(f"  {field:<8}: {status_str}")

    # 4. Propose repairs
    print("\nGenerating candidate rules via MockRuleGenerator...")
    repair_proposals = repair_manager.propose_repairs(html_samples, baseline_selectors)

    print("\nProposed candidate selectors:")
    for field, prop in repair_proposals["proposals"].items():
        print(f"  {field:<8}: {prop['candidate_selector']}")

    print("\nValidating candidate rules against acceptance threshold (>= 90%)...")
    validation_summary = {}
    for field, prop in repair_proposals["proposals"].items():
        pct = int(prop["success_rate"] * 100)
        validation_summary[field] = {
            "success_rate": prop["success_rate"],
            "accepted": prop["accepted"],
        }
        print(f"  {field:<8}: {pct}% -> {'ACCEPTED' if prop['accepted'] else 'REJECTED'}")

    if repair_proposals["all_accepted"]:
        print("\nAll candidate rules accepted by validator gatekeeper.")

    # 5. Apply approved repairs
    print("\nApplying approved repairs (explicit approval granted)...")
    repaired_config = repair_manager.apply_repairs(
        baseline_config, repair_proposals, approved=True, source="mock-ai", reason="simulated redesign repair"
    )

    print(f"Updated configuration version: {repaired_config['metadata']['version']}")

    # 6. Re-run deterministic extraction on modified pages
    print("\nRunning deterministic extraction again with repaired rules...")
    sample_doc = html_samples[0]
    extraction_after = extractor.extract(sample_doc, repaired_config["selectors"])

    after_status = {}
    for field, res in extraction_after.items():
        status_str = "SUCCESS" if res["success"] else "FAILED"
        after_status[field] = res["success"]
        print(f"  {field:<8}: {status_str} (value: {res['value'][:45]!r}...)")

    # 7. Write results artifact
    result_artifact = {
        "experiment": "simulated_layout_change_repair",
        "generator": "MockRuleGenerator",
        "pages_tested": len(html_samples),
        "threshold": validator.threshold,
        "before_repair": before_status,
        "proposed_rules": {
            f: p["candidate_selector"] for f, p in repair_proposals["proposals"].items()
        },
        "validation_results": validation_summary,
        "approved": True,
        "after_repair": after_status,
        "repaired_config_sample": repaired_config["selectors"],
    }

    results_path = os.path.join(os.path.dirname(__file__), "..", "results", "repair_experiment.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(result_artifact, f, indent=2)

    print(f"\nSaved experiment artifact to: {os.path.relpath(results_path)}")
    print("=" * 60)


if __name__ == "__main__":
    run_experiment()

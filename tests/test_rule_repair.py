"""
Unit and integration tests for Rule Repair and Mock Rule Generator (Phase 2).
"""

import glob
import os
import pytest
from src.extractor import DeterministicExtractor
from src.rule_generator import (
    MockRuleGenerator,
    SchemaValidationError,
    validate_rule_proposal,
)
from src.rule_repair import RuleRepairManager
from src.selector_validator import SelectorValidator

MODIFIED_HTML_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "modified_html"
)
RAW_HTML_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "raw_html"
)


@pytest.fixture
def modified_html_samples():
    html_files = sorted(glob.glob(os.path.join(MODIFIED_HTML_DIR, "*.html")))
    samples = []
    for fpath in html_files:
        with open(fpath, "r", encoding="utf-8") as f:
            samples.append(f.read())
    return samples


@pytest.fixture
def raw_html_samples():
    html_files = sorted(glob.glob(os.path.join(RAW_HTML_DIR, "*.html")))
    samples = []
    for fpath in html_files:
        with open(fpath, "r", encoding="utf-8") as f:
            samples.append(f.read())
    return samples


@pytest.fixture
def baseline_rules():
    return {
        "title": {"selector": "h1.article-title", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},
        "date": {"selector": "time.published", "type": "css"},
        "author": {"selector": ".author-name", "type": "css"},
    }


@pytest.fixture
def baseline_config(baseline_rules):
    return {
        "portal": {
            "name": "synthetic_test_portal",
            "domain": None,
            "description": "Local synthetic HTML fixtures",
        },
        "selectors": baseline_rules,
        "metadata": {
            "version": 1,
            "created_by": "manual",
            "validated": True,
            "acceptance_threshold": 0.90,
        },
    }


# Test 1: MockRuleGenerator returns valid rule schema
def test_mock_rule_generator_valid_schema(modified_html_samples):
    gen = MockRuleGenerator()
    rules = gen.generate_rules(modified_html_samples)
    assert isinstance(rules, dict)
    assert "title" in rules
    assert "body" in rules
    assert "date" in rules
    assert "author" in rules
    # Schema check passes without error
    validate_rule_proposal(rules)


# Test 2: Malformed generated rule is rejected
def test_malformed_rule_rejected():
    with pytest.raises(SchemaValidationError, match="must be a dict"):
        validate_rule_proposal("not a dict")  # type: ignore

    with pytest.raises(SchemaValidationError, match="disallowed keys"):
        validate_rule_proposal({"title": {"selector": "h1", "type": "css", "malicious_payload": "rm -rf"}})


# Test 3: Empty selector is rejected
def test_empty_selector_rejected():
    with pytest.raises(SchemaValidationError, match="cannot be empty or whitespace"):
        validate_rule_proposal({"title": {"selector": "   ", "type": "css"}})


# Test 4: Unsupported selector type is rejected
def test_unsupported_selector_type_rejected():
    with pytest.raises(SchemaValidationError, match="unsupported selector type"):
        validate_rule_proposal({"title": {"selector": "h1", "type": "regex"}})


# Test 5: Old selectors fail against redesigned fixtures
def test_old_selectors_fail_against_redesigned_fixtures(modified_html_samples, baseline_rules):
    validator = SelectorValidator(threshold=0.90)
    for field, rule in baseline_rules.items():
        report = validator.validate_selector(modified_html_samples, rule)
        assert report["is_acceptable"] is False
        assert report["success_rate"] == 0.0


# Test 6: Repair manager correctly identifies failing fields
def test_repair_manager_identifies_failing_fields(modified_html_samples, baseline_rules):
    manager = RuleRepairManager()
    failed = manager.detect_failures(modified_html_samples, baseline_rules)
    assert set(failed) == {"title", "body", "date", "author"}


# Test 7: Good replacement selector passes validator
def test_good_replacement_selector_passes(modified_html_samples):
    manager = RuleRepairManager()
    good_cand = {"selector": "section.story-text", "type": "css"}
    report = manager.validator.validate_selector(modified_html_samples, good_cand)
    assert report["is_acceptable"] is True
    assert report["success_rate"] == 1.0


# Test 8: Bad replacement selector is rejected
def test_bad_replacement_selector_rejected(modified_html_samples):
    manager = RuleRepairManager()
    bad_cand = {"selector": "div.nonexistent-box", "type": "css"}
    report = manager.validator.validate_selector(modified_html_samples, bad_cand)
    assert report["is_acceptable"] is False
    assert report["success_rate"] == 0.0


# Test 9 & 10: Propose repairs produces accepted proposals for valid candidates
def test_propose_repairs_valid_candidates(modified_html_samples, baseline_rules):
    manager = RuleRepairManager()
    proposal_report = manager.propose_repairs(modified_html_samples, baseline_rules)

    assert proposal_report["all_accepted"] is True
    assert set(proposal_report["failed_fields"]) == {"title", "body", "date", "author"}
    assert proposal_report["proposals"]["title"]["candidate_selector"] == "h2.headline"
    assert proposal_report["proposals"]["title"]["accepted"] is True
    assert proposal_report["proposals"]["body"]["candidate_selector"] == "section.story-text"
    assert proposal_report["proposals"]["body"]["accepted"] is True


# Test 11: Rule is not applied before explicit approval
def test_rule_not_applied_without_approval(modified_html_samples, baseline_config):
    manager = RuleRepairManager()
    proposal_report = manager.propose_repairs(modified_html_samples, baseline_config["selectors"])

    with pytest.raises(PermissionError, match="Cannot apply repairs without explicit approval"):
        manager.apply_repairs(baseline_config, proposal_report, approved=False)


# Test 12, 13, 14: Approved rule updates selector, preserves history, and increments version
def test_apply_repairs_updates_selectors_and_preserves_history(modified_html_samples, baseline_config):
    manager = RuleRepairManager()
    proposal_report = manager.propose_repairs(modified_html_samples, baseline_config["selectors"])

    updated_config = manager.apply_repairs(baseline_config, proposal_report, approved=True)

    # Version increments
    assert updated_config["metadata"]["version"] == 2
    assert updated_config["metadata"]["updated_by"] == "mock-ai"

    # Current selector updated
    assert updated_config["selectors"]["title"]["selector"] == "h2.headline"
    assert updated_config["selectors"]["body"]["selector"] == "section.story-text"

    # History preserved
    title_history = updated_config["selectors"]["title"]["history"]
    assert len(title_history) == 1
    assert title_history[0]["selector"] == "h1.article-title"
    assert title_history[0]["version"] == 1
    assert title_history[0]["source"] == "manual"


# Test 15: Repaired rules restore deterministic extraction
def test_repaired_rules_restore_deterministic_extraction(modified_html_samples, baseline_config):
    manager = RuleRepairManager()
    extractor = DeterministicExtractor()

    # Step 1: Extraction fails with baseline rules on modified HTML
    initial_results = extractor.extract(modified_html_samples[0], baseline_config["selectors"])
    assert initial_results["title"]["success"] is False
    assert initial_results["body"]["success"] is False

    # Step 2: Propose and apply repairs
    proposals = manager.propose_repairs(modified_html_samples, baseline_config["selectors"])
    repaired_config = manager.apply_repairs(baseline_config, proposals, approved=True)

    # Step 3: Extraction succeeds with repaired rules on modified HTML
    repaired_results = extractor.extract(modified_html_samples[0], repaired_config["selectors"])
    assert repaired_results["title"]["success"] is True
    assert repaired_results["title"]["value"] == "Oral Histories and Indigenous Archival Practices"
    assert repaired_results["body"]["success"] is True
    assert "Indigenous knowledge systems" in repaired_results["body"]["value"]
    assert repaired_results["date"]["success"] is True
    assert repaired_results["date"]["value"] == "March 15, 2024"
    assert repaired_results["author"]["success"] is True
    assert repaired_results["author"]["value"] == "Dr. Elena Ramos"


# Test 16: Repairing one field does not unnecessarily replace working fields
def test_partial_repair_only_updates_failing_field():
    manager = RuleRepairManager()
    # Sample where title works, but body fails
    mixed_samples = [
        """<html><body>
            <h1 class="article-title">Working Title</h1>
            <section class="story-text">New body layout</section>
        </body></html>"""
    ]
    mixed_rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},  # this fails
    }
    config = {
        "selectors": mixed_rules,
        "metadata": {"version": 1, "created_by": "manual"},
    }

    proposals = manager.propose_repairs(mixed_samples, mixed_rules)
    assert proposals["failed_fields"] == ["body"]
    assert "title" not in proposals["proposals"]
    assert "body" in proposals["proposals"]

    updated = manager.apply_repairs(config, proposals, approved=True)
    # Title rule remains unchanged
    assert updated["selectors"]["title"]["selector"] == "h1.article-title"
    # Body rule updated
    assert updated["selectors"]["body"]["selector"] == "section.story-text"

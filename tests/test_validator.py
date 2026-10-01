"""
Unit tests for Selector Validator (Phase 2).
"""

import glob
import os
import pytest
from src.selector_validator import SelectorValidator, validate_selector

RAW_HTML_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "raw_html"
)


@pytest.fixture
def raw_html_samples():
    html_files = sorted(glob.glob(os.path.join(RAW_HTML_DIR, "*.html")))
    samples = []
    for fpath in html_files:
        with open(fpath, "r", encoding="utf-8") as f:
            samples.append(f.read())
    return samples


@pytest.fixture
def validator():
    return SelectorValidator(threshold=0.90)


def test_validator_perfect_selector(validator, raw_html_samples):
    # h1.article-title exists on all 10 raw pages
    report = validator.validate_selector(raw_html_samples, "h1.article-title", "css")

    assert report["tested_pages"] == len(raw_html_samples)
    assert report["successful_pages"] == len(raw_html_samples)
    assert report["success_rate"] == 1.0
    assert report["empty_count"] == 0
    assert report["failure_count"] == 0
    assert report["is_acceptable"] is True


def test_validator_failing_selector(validator, raw_html_samples):
    # Obsolete or wrong selector
    report = validator.validate_selector(raw_html_samples, "article.main-story", "css")

    assert report["tested_pages"] == len(raw_html_samples)
    assert report["successful_pages"] == 0
    assert report["success_rate"] == 0.0
    assert report["failure_count"] == len(raw_html_samples)
    assert report["is_acceptable"] is False


def test_validator_threshold_boundary():
    # 2 good pages, 1 failing page
    samples = [
        "<html><body><h1 class='target'>Doc 1</h1></body></html>",
        "<html><body><h1 class='target'>Doc 2</h1></body></html>",
        "<html><body><p>No target here</p></body></html>",
    ]
    # 2/3 = 0.6667
    v_high = SelectorValidator(threshold=0.90)
    res_high = v_high.validate_selector(samples, "h1.target")
    assert res_high["success_rate"] == 0.6667
    assert res_high["is_acceptable"] is False

    v_low = SelectorValidator(threshold=0.60)
    res_low = v_low.validate_selector(samples, "h1.target")
    assert res_low["is_acceptable"] is True


def test_validate_rule_set(validator, raw_html_samples):
    rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "author": {"selector": ".author-name", "type": "css"},
        "date": {"selector": "time.published", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},
    }
    report = validator.validate_rule_set(raw_html_samples, rules)

    assert report["tested_pages"] == len(raw_html_samples)
    assert report["overall_acceptable"] is True
    assert "title" in report["fields"]
    assert report["fields"]["title"]["is_acceptable"] is True
    assert report["fields"]["body"]["is_acceptable"] is True


def test_validate_rule_set_with_failing_field(validator, raw_html_samples):
    rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "broken_field": {"selector": ".nonexistent-class", "type": "css"},
    }
    report = validator.validate_rule_set(raw_html_samples, rules)

    assert report["overall_acceptable"] is False
    assert report["fields"]["title"]["is_acceptable"] is True
    assert report["fields"]["broken_field"]["is_acceptable"] is False

"""
Unit tests for Deterministic HTML Extractor (Phase 1).
"""

import os
import pytest
from src.extractor import DeterministicExtractor, extract

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
    <article>
        <h1 class="article-title">Indigenous Data Sovereignty</h1>
        <div class="meta">
            <span class="author-name">Dr. T. Walker</span>
            <time class="published">2024-02-10</time>
        </div>
        <div class="article-body">
            <p>First paragraph detailing research findings.</p>
            <p>Second paragraph on methodology and community protocols.</p>
        </div>
        <div class="empty-field">   </div>
    </article>
</body>
</html>
"""

MALFORMED_HTML = "<article><h1 class='article-title'>Broken HTML<div class='article-body'>Unclosed tags"


@pytest.fixture
def extractor():
    return DeterministicExtractor()


def test_valid_css_extraction(extractor):
    rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "author": {"selector": ".author-name", "type": "css"},
        "date": {"selector": "time.published", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},
    }
    results = extractor.extract(SAMPLE_HTML, rules)

    assert results["title"]["success"] is True
    assert results["title"]["value"] == "Indigenous Data Sovereignty"

    assert results["author"]["success"] is True
    assert results["author"]["value"] == "Dr. T. Walker"

    assert results["date"]["success"] is True
    assert results["date"]["value"] == "2024-02-10"

    assert results["body"]["success"] is True
    assert "First paragraph" in results["body"]["value"]
    assert "Second paragraph" in results["body"]["value"]


def test_invalid_or_missing_selector(extractor):
    rules = {
        "nonexistent": {"selector": "div.missing-class", "type": "css"},
        "syntax_error": {"selector": "div[invalid===selector]", "type": "css"},
    }
    results = extractor.extract(SAMPLE_HTML, rules)

    assert results["nonexistent"]["success"] is False
    assert results["nonexistent"]["value"] is None
    assert "No element found" in results["nonexistent"]["error"]

    assert results["syntax_error"]["success"] is False
    assert results["syntax_error"]["value"] is None
    assert "Invalid CSS selector" in results["syntax_error"]["error"]


def test_empty_element_handling(extractor):
    rule = {"selector": ".empty-field", "type": "css"}
    result = extractor.extract_field(SAMPLE_HTML, rule)

    assert result["success"] is False
    assert result["value"] is None
    assert "empty text" in result["error"].lower()


def test_xpath_extraction(extractor):
    rules = {
        "title": {"selector": "//h1[@class='article-title']", "type": "xpath"},
        "author": {"selector": "//span[contains(@class, 'author-name')]", "type": "xpath"},
    }
    results = extractor.extract(SAMPLE_HTML, rules)

    assert results["title"]["success"] is True
    assert results["title"]["value"] == "Indigenous Data Sovereignty"
    assert results["author"]["success"] is True
    assert results["author"]["value"] == "Dr. T. Walker"


def test_malformed_html_handling(extractor):
    rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},
    }
    results = extractor.extract(MALFORMED_HTML, rules)

    assert results["title"]["success"] is True
    assert "Broken HTML" in results["title"]["value"]


def test_empty_html_input(extractor):
    result = extractor.extract_field("", {"selector": "h1", "type": "css"})
    assert result["success"] is False
    assert result["value"] is None
    assert "Empty or missing HTML" in result["error"]


def test_real_raw_html_fixtures(extractor):
    fixture_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "raw_html", "page01.html"
    )
    with open(fixture_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    rules = {
        "title": {"selector": "h1.article-title", "type": "css"},
        "author": {"selector": ".author-name", "type": "css"},
        "date": {"selector": "time.published", "type": "css"},
        "body": {"selector": "div.article-body", "type": "css"},
    }
    results = extractor.extract(html_content, rules)

    assert results["title"]["success"] is True
    assert results["title"]["value"] == "Oral Histories and Indigenous Archival Practices"
    assert results["author"]["value"] == "Dr. Elena Ramos"
    assert results["date"]["value"] == "March 15, 2024"
    assert "oral tradition" in results["body"]["value"].lower()

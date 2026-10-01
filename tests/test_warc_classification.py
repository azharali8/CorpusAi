"""
Unit tests for isolated WARC record classification and HTML filtering.
Verifies all 10 required test criteria:
1. HTML detected by MIME type.
2. extensionless HTML URL detected.
3. misleading .html image URL rejected.
4. CSS detected.
5. JavaScript detected.
6. image detected.
7. JSON detected.
8. non-response records ignored for HTML extraction.
9. malformed HTTP metadata handled safely.
10. HTML filter returns exactly expected records.
"""

import os
import pytest
import sys

from research.archiving.experiments.inspect_warc_records import (
    classify_record_content,
    filter_usable_html_records,
    inspect_warc_file,
)

FIXTURE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "warc", "mixed_content_test.warc")
)


def test_fixture_exists():
    assert os.path.isfile(FIXTURE_PATH), f"Fixture not found: {FIXTURE_PATH}"


# 1. HTML detected by MIME type
def test_html_detected_by_mime():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="text/html; charset=utf-8",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/page.html",
    )
    assert cat == "HTML"


# 2. Extensionless HTML URL detected
def test_extensionless_html_detected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="text/html; charset=utf-8",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/articles/123",
    )
    assert cat == "HTML"


# 3. Misleading .html image URL rejected from HTML category
def test_misleading_html_image_rejected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="image/png",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/image.html",
    )
    assert cat == "IMAGE"
    assert cat != "HTML"


# 4. CSS detected
def test_css_detected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="text/css",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/style.css",
    )
    assert cat == "CSS"


# 5. JavaScript detected
def test_javascript_detected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="application/javascript",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/app.js",
    )
    assert cat == "JAVASCRIPT"


# 6. Image detected
def test_image_detected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="image/jpeg",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/photo.jpg",
    )
    assert cat == "IMAGE"


# 7. JSON detected
def test_json_detected():
    cat = classify_record_content(
        rec_type="response",
        http_content_type="application/json",
        warc_content_type="application/http; msgtype=response",
        url="https://example.com/api/data",
    )
    assert cat == "JSON"


# 8. Non-response records ignored for HTML extraction
def test_non_response_records_ignored_for_html():
    cat_info = classify_record_content(
        rec_type="warcinfo",
        http_content_type=None,
        warc_content_type="application/warc-fields",
        url="",
    )
    assert cat_info != "HTML"

    cat_resource = classify_record_content(
        rec_type="resource",
        http_content_type=None,
        warc_content_type="text/html",  # outer WARC type may say text/html, but it's a resource
        url="urn:example:res",
    )
    assert cat_resource != "HTML"


# 9. Malformed HTTP metadata handled safely
def test_malformed_http_metadata_handled_safely():
    # None values, malformed strings, semicolons without type
    cat1 = classify_record_content(
        rec_type="response",
        http_content_type=None,
        warc_content_type=None,
        url="https://example.com/unknown",
    )
    assert isinstance(cat1, str)

    cat2 = classify_record_content(
        rec_type="response",
        http_content_type=";;;malformed-mime",
        warc_content_type="application/http",
        url="https://example.com/test",
    )
    assert isinstance(cat2, str)


# 10. HTML filter returns exactly expected records from controlled fixture
def test_html_filter_returns_exact_expected_records():
    html_records = filter_usable_html_records(FIXTURE_PATH)
    urls = [r["warc_target_uri"] for r in html_records]

    expected = [
        "https://example.test/articles/science-today.html",
        "https://example.test/article/123",
        "https://example.test/news?id=45",
    ]

    assert len(html_records) == 3
    assert urls == expected
    assert "https://example.test/image.html" not in urls
    assert "https://example.test/assets/style.css" not in urls
    assert "https://example.test/assets/app.js" not in urls
    assert "https://example.test/robots.txt" not in urls

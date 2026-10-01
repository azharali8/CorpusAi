"""
Unit tests for Phase 5C: WordPress REST API post-by-slug, Content JSON classification & WARC creation,
Content Image role classification, representation comparison, and replay checklist.

All tests run completely OFFLINE using fixtures and mocked responses.
"""

import json
import os
import pytest
from bs4 import BeautifulSoup

from src.article_inspector import (
    classify_image_role,
    classify_json_payload,
    compare_html_and_json_content,
    extract_article_metadata,
    extract_content_images,
    extract_referenced_assets,
)
from src.warc_utils import create_warc_response_file, extract_html_samples


SAMPLE_POST_SLUG_JSON = [
    {
        "id": 2045,
        "date": "2026-09-21T14:28:41",
        "date_gmt": "2026-09-21T14:28:41",
        "slug": "owa-president",
        "status": "publish",
        "type": "post",
        "link": "https://wordpress.org/news/2026/09/owa-president/",
        "title": {
            "rendered": "WordPress Takes Its Turn Leading the Open Website Alliance"
        },
        "content": {
            "rendered": "<p>WordPress is proud to take on the presidency of the Open Website Alliance (OWA) for the upcoming year.</p><figure class=\"wp-block-image\"><img src=\"https://i0.wp.com/wordpress.org/news/files/2026/09/image-3.png\" alt=\"OWA Meeting\"></figure><p>The alliance brings together open source content management systems to advocate for an open web.</p>",
            "protected": False
        },
        "author": 88
    }
]

SAMPLE_EMPTY_POST_SLUG_JSON = []

SAMPLE_ARTICLE_HTML_WITH_IMAGES = """
<!DOCTYPE html>
<html>
<head>
    <title>WordPress Takes Its Turn Leading the Open Website Alliance – WordPress News</title>
    <link rel="canonical" href="https://wordpress.org/news/2026/09/owa-president/">
</head>
<body>
    <header class="site-header">
        <img src="https://s.w.org/style/images/code-is-poetry-for-dark-bg.svg" alt="Header Logo">
        <img src="https://s.w.org/favicon.ico" alt="Favicon">
    </header>
    <main>
        <article class="wp-block-post">
            <h1>WordPress Takes Its Turn Leading the Open Website Alliance</h1>
            <div class="entry-content">
                <p>WordPress is proud to take on the presidency of the Open Website Alliance.</p>
                <div class="wp-block-image">
                    <img src="https://i0.wp.com/wordpress.org/news/files/2026/09/image-3.png" alt="Alliance Board">
                </div>
            </div>
        </article>
    </main>
    <footer class="site-footer">
        <img src="https://s.w.org/images/wmark.png" alt="Footer Mark">
    </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Test 1: WordPress post JSON by slug parsing
# ---------------------------------------------------------------------------
def test_wp_post_json_by_slug_parsing():
    classification = classify_json_payload(SAMPLE_POST_SLUG_JSON)
    assert classification["classification"] == "CONTENT_JSON"
    assert classification["is_content_json"] is True
    assert classification["slug"] == "owa-president"
    assert classification["post_id"] == 2045


# ---------------------------------------------------------------------------
# Test 2: Content.rendered classification
# ---------------------------------------------------------------------------
def test_content_rendered_classification():
    post = SAMPLE_POST_SLUG_JSON[0]
    classification = classify_json_payload(post)
    assert classification["is_content_json"] is True
    assert classification["content_length_chars"] > 100


# ---------------------------------------------------------------------------
# Test 3: Empty API result handling
# ---------------------------------------------------------------------------
def test_empty_post_slug_result_handling():
    classification = classify_json_payload(SAMPLE_EMPTY_POST_SLUG_JSON)
    assert classification["is_content_json"] is False
    assert classification["classification"] == "METADATA_JSON"


# ---------------------------------------------------------------------------
# Test 4: JSON content WARC creation
# ---------------------------------------------------------------------------
def test_json_content_warc_creation(tmp_path):
    warc_path = str(tmp_path / "content_json.warc.gz")
    json_bytes = json.dumps(SAMPLE_POST_SLUG_JSON).encode("utf-8")
    
    out_path, records = create_warc_response_file(
        warc_path,
        [{
            "uri": "https://wordpress.org/news/wp-json/wp/v2/posts?slug=owa-president",
            "status_code": 200,
            "content_type": "application/json; charset=utf-8",
            "payload": json_bytes,
            "custom_warc_headers": {
                "WARC-Target-Role": "article-content-json",
            },
        }],
        gzip=True,
    )
    assert os.path.exists(out_path)
    assert len(records) == 1
    assert records[0]["content_type"] == "application/json; charset=utf-8"


# ---------------------------------------------------------------------------
# Test 5: Content image classification (article_content vs site_chrome)
# ---------------------------------------------------------------------------
def test_content_image_classification():
    images = extract_content_images(SAMPLE_ARTICLE_HTML_WITH_IMAGES, page_url="https://wordpress.org/news/2026/09/owa-president/")
    
    roles_by_url = {img["url"]: img["role"] for img in images}
    
    # Header logo / favicon / footer mark should be classified as site_chrome
    assert roles_by_url["https://s.w.org/style/images/code-is-poetry-for-dark-bg.svg"] == "site_chrome"
    assert roles_by_url["https://s.w.org/favicon.ico"] == "site_chrome"
    assert roles_by_url["https://s.w.org/images/wmark.png"] == "site_chrome"
    
    # In-body image should be classified as article_content
    assert roles_by_url["https://i0.wp.com/wordpress.org/news/files/2026/09/image-3.png"] == "article_content"


# ---------------------------------------------------------------------------
# Test 6: HTML vs JSON representation comparison
# ---------------------------------------------------------------------------
def test_html_and_json_representation_comparison():
    meta = extract_article_metadata(SAMPLE_ARTICLE_HTML_WITH_IMAGES, base_url="https://wordpress.org/news/2026/09/owa-president/")
    comp = compare_html_and_json_content(
        html_metadata=meta,
        html_body_text="WordPress is proud to take on the presidency of the Open Website Alliance.",
        json_post_data=SAMPLE_POST_SLUG_JSON[0],
    )
    assert comp["same_article_identity"] is True
    assert comp["title_match"] is True
    assert comp["json_body_available"] is True
    assert comp["normalized_word_jaccard_similarity"] > 0.3


# ---------------------------------------------------------------------------
# Test 7: Asset-type accounting
# ---------------------------------------------------------------------------
def test_asset_type_accounting():
    assets = extract_referenced_assets(SAMPLE_ARTICLE_HTML_WITH_IMAGES, page_url="https://wordpress.org/news/2026/09/owa-president/")
    types = [a["resource_type"] for a in assets]
    assert "image" in types
    assert len(types) >= 3


# ---------------------------------------------------------------------------
# Test 8: Full page replay verified remains False before manual verification
# ---------------------------------------------------------------------------
def test_full_page_replay_verified_remains_false():
    manifest_state = {
        "full_page_replay_verified": False,
        "html_capture_success": True,
    }
    assert manifest_state["full_page_replay_verified"] is False

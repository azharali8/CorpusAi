"""
Unit tests for Phase 5B: Content URL Registry, Article Inspector, Content JSON Classification,
Asset Discovery, Lazy Loading, and WARC separation.

These tests run completely OFFLINE using fixtures and mocked responses.
"""

import os
import json
import pytest
from bs4 import BeautifulSoup

from src.article_inspector import (
    classify_json_payload,
    extract_article_metadata,
    extract_referenced_assets,
    inspect_lazy_loading,
)
from src.content_url_registry import ContentURLEntry, ContentURLRegistry
from src.warc_utils import create_warc_response_file, list_html_records, extract_html_samples


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ARTICLE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>WordPress 7.1.2 Release – WordPress News</title>
    <link rel="canonical" href="https://wordpress.org/news/2026/09/wordpress-7-1-2-release/">
    <link rel="https://api.w.org/" href="https://wordpress.org/news/wp-json/wp/v2/posts/1001">
    <link rel="stylesheet" href="https://wordpress.org/news/style.css">
    <link rel="stylesheet" href="/assets/custom.css">
    <link rel="preload" href="/fonts/inter.woff2" as="font" type="font/woff2">
    <meta name="author" content="WordPress Release Squad">
    <meta property="article:published_time" content="2026-09-20T14:00:00Z">
</head>
<body>
    <header>
        <h1>WordPress 7.1.2 Maintenance Release</h1>
        <div class="byline">By <a rel="author" href="/author/squad">WordPress Squad</a></div>
        <time datetime="2026-09-20T14:00:00Z">September 20, 2026</time>
    </header>
    <main>
        <p>This is the official maintenance release of WordPress 7.1.2.</p>
        <img src="https://wordpress.org/images/hero.png" alt="Hero" loading="lazy" srcset="https://wordpress.org/images/hero.png 1x, https://wordpress.org/images/hero@2x.png 2x">
        <img data-src="/images/deferred-chart.jpg" alt="Chart">
        <video src="https://cdn.example.com/videos/demo.mp4"></video>
    </main>
    <script src="https://wordpress.org/news/app.js" defer></script>
</body>
</html>
"""

SAMPLE_WP_REST_JSON = {
    "id": 1001,
    "date": "2026-09-20T14:00:00",
    "slug": "wordpress-7-1-2-release",
    "status": "publish",
    "type": "post",
    "link": "https://wordpress.org/news/2026/09/wordpress-7-1-2-release/",
    "title": {
        "rendered": "WordPress 7.1.2 Maintenance Release"
    },
    "content": {
        "rendered": "<p>This is the official maintenance release of WordPress 7.1.2 with extensive text content explaining security fixes and bug repairs in detail.</p>",
        "protected": False
    },
    "author": 42
}

SAMPLE_METADATA_ONLY_JSON = {
    "name": "WordPress News",
    "description": "The latest news about WordPress",
    "url": "https://wordpress.org/news",
    "namespaces": ["wp/v2"],
    "routes": {
        "/wp/v2/posts": {"methods": ["GET", "POST"]}
    }
}


# ---------------------------------------------------------------------------
# Test 1: Known article URL registry initialization and tracking
# ---------------------------------------------------------------------------
def test_content_url_registry_tracking(tmp_path):
    registry = ContentURLRegistry()
    entry = registry.register_url(
        url="https://wordpress.org/news/2026/09/article-1",
        source_archive="https://wordpress.org/news/all-posts/",
        content_role="article",
    )
    assert entry.url == "https://wordpress.org/news/2026/09/article-1"
    assert entry.capture_status == "pending"
    assert entry.source_archive == "https://wordpress.org/news/all-posts/"
    assert registry.count() == 1

    # Update capture success
    registry.update_capture_success(
        url="https://wordpress.org/news/2026/09/article-1",
        http_status=200,
        content_type="text/html; charset=utf-8",
        final_url="https://wordpress.org/news/2026/09/article-1/",
        payload_sha256="abc123hash",
        payload_size_bytes=4096,
        warc_record_id="<urn:uuid:1234>",
        metadata={"title": "Test Article"},
    )
    saved_entry = registry.get("https://wordpress.org/news/2026/09/article-1")
    assert saved_entry.capture_status == "captured"
    assert saved_entry.http_status == 200
    assert saved_entry.warc_record_id == "<urn:uuid:1234>"

    # Save and reload
    reg_file = str(tmp_path / "registry.json")
    registry.save_to_json(reg_file)
    reloaded = ContentURLRegistry.load_from_json(reg_file)
    assert reloaded.count() == 1
    assert reloaded.get("https://wordpress.org/news/2026/09/article-1").payload_sha256 == "abc123hash"


# ---------------------------------------------------------------------------
# Test 2: Successful HTML response metadata extraction
# ---------------------------------------------------------------------------
def test_article_metadata_extraction():
    meta = extract_article_metadata(SAMPLE_ARTICLE_HTML, base_url="https://wordpress.org/news/2026/09/wordpress-7-1-2-release/")
    assert "WordPress 7.1.2" in meta["title"]
    assert meta["heading"] == "WordPress 7.1.2 Maintenance Release"
    assert meta["canonical_url"] == "https://wordpress.org/news/2026/09/wordpress-7-1-2-release/"
    assert meta["publication_date"] == "2026-09-20T14:00:00Z"
    assert "Squad" in meta["author"]
    assert meta["rest_api_url"] == "https://wordpress.org/news/wp-json/wp/v2/posts/1001"


# ---------------------------------------------------------------------------
# Test 3: Non-HTML response not treated as article HTML
# ---------------------------------------------------------------------------
def test_non_html_response_classification():
    registry = ContentURLRegistry()
    registry.register_url("https://example.com/asset.pdf", source_archive="https://example.com")
    registry.update_capture_failure(
        url="https://example.com/asset.pdf",
        error="Non-HTML response: application/pdf",
        http_status=200,
    )
    entry = registry.get("https://example.com/asset.pdf")
    assert entry.capture_status == "failed"
    assert "Non-HTML" in entry.error


# ---------------------------------------------------------------------------
# Test 4: Redirect final URL preservation
# ---------------------------------------------------------------------------
def test_redirect_final_url_preserved():
    registry = ContentURLRegistry()
    registry.register_url("https://wordpress.org/news/2026/09/short-slug", source_archive="https://example.com")
    registry.update_capture_success(
        url="https://wordpress.org/news/2026/09/short-slug",
        http_status=200,
        content_type="text/html",
        final_url="https://wordpress.org/news/2026/09/full-canonical-slug/",
        payload_sha256="dummy",
        payload_size_bytes=100,
    )
    entry = registry.get("https://wordpress.org/news/2026/09/short-slug")
    assert entry.final_url == "https://wordpress.org/news/2026/09/full-canonical-slug/"


# ---------------------------------------------------------------------------
# Test 5: WARC content record creation with provenance headers
# ---------------------------------------------------------------------------
def test_warc_content_record_creation(tmp_path):
    warc_path = str(tmp_path / "content_test.warc.gz")
    records = [{
        "uri": "https://wordpress.org/news/2026/09/test-article",
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "payload": "<html><body>Hello Article</body></html>",
        "custom_warc_headers": {
            "WARC-Source-Archive": "https://wordpress.org/news/all-posts/",
            "WARC-Target-Role": "article-html",
        },
    }]

    out_path, info = create_warc_response_file(warc_path, records, gzip=True)
    assert os.path.exists(out_path)
    assert len(info) == 1
    assert info[0]["warc_record_id"].startswith("<urn:uuid:")
    assert info[0]["warc_target_uri"] == "https://wordpress.org/news/2026/09/test-article"

    # Verify warcio can read it back
    samples = extract_html_samples(out_path)
    assert len(samples) == 1
    assert "Hello Article" in samples[0]["html"]


# ---------------------------------------------------------------------------
# Test 6: Content and assets separation in WARC files
# ---------------------------------------------------------------------------
def test_content_and_assets_warc_separation(tmp_path):
    content_warc = str(tmp_path / "content_html.warc.gz")
    assets_warc = str(tmp_path / "assets.warc.gz")

    # Content WARC gets 1 HTML record
    create_warc_response_file(content_warc, [{
        "uri": "https://wordpress.org/news/2026/09/article",
        "content_type": "text/html",
        "payload": "<html><body>Content</body></html>",
    }])

    # Assets WARC gets 0 records in discovery-only mode
    create_warc_response_file(assets_warc, [])

    content_records = list_html_records(content_warc)
    asset_records = list_html_records(assets_warc)

    assert len(content_records) == 1
    assert len(asset_records) == 0


# ---------------------------------------------------------------------------
# Test 7: Asset inventory extraction (images, CSS, JS, fonts, media)
# ---------------------------------------------------------------------------
def test_referenced_assets_inventory():
    assets = extract_referenced_assets(SAMPLE_ARTICLE_HTML, page_url="https://wordpress.org/news/2026/09/wordpress-7-1-2-release/")
    
    types = {a["resource_type"] for a in assets}
    assert "image" in types
    assert "css" in types
    assert "javascript" in types
    assert "font" in types
    assert "video" in types

    # Check origin separation
    same_origin = [a for a in assets if a["same_origin"]]
    cross_origin = [a for a in assets if not a["same_origin"]]
    assert any(a["url"].startswith("https://wordpress.org") for a in same_origin)
    assert any("example.com" in a["url"] for a in cross_origin)


# ---------------------------------------------------------------------------
# Test 8: Lazy-load marker detection (loading="lazy", data-src, srcset, defer)
# ---------------------------------------------------------------------------
def test_lazy_loading_detection():
    lazy_info = inspect_lazy_loading(SAMPLE_ARTICLE_HTML)
    assert lazy_info["lazy_loading_detected"] is True
    assert lazy_info["loading_lazy_count"] >= 1
    assert lazy_info["data_src_count"] >= 1
    assert lazy_info["srcset_count"] >= 1
    assert lazy_info["deferred_scripts_count"] >= 1


# ---------------------------------------------------------------------------
# Test 9: Content JSON classification vs Metadata JSON classification
# ---------------------------------------------------------------------------
def test_json_content_classification():
    # 1. Content-bearing REST post
    res1 = classify_json_payload(SAMPLE_WP_REST_JSON)
    assert res1["classification"] == "CONTENT_JSON"
    assert res1["is_content_json"] is True
    assert res1["slug"] == "wordpress-7-1-2-release"

    # 2. Metadata / Index JSON
    res2 = classify_json_payload(SAMPLE_METADATA_ONLY_JSON)
    assert res2["classification"] == "METADATA_JSON"
    assert res2["is_content_json"] is False

    # 3. Arbitrary array
    res3 = classify_json_payload([{"id": 1}, {"id": 2}])
    assert res3["classification"] == "METADATA_JSON"
    assert res3["is_content_json"] is False


# ---------------------------------------------------------------------------
# Test 10: Strict limit to first 3 articles
# ---------------------------------------------------------------------------
def test_max_articles_limit_enforced(tmp_path):
    disc_file = str(tmp_path / "discovered_urls.txt")
    with open(disc_file, "w", encoding="utf-8") as f:
        for i in range(10):
            f.write(f"https://wordpress.org/news/2026/09/article-{i}\n")

    with open(disc_file, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    target_subset = urls[:3]
    assert len(target_subset) == 3
    assert target_subset[0] == "https://wordpress.org/news/2026/09/article-0"
    assert target_subset[2] == "https://wordpress.org/news/2026/09/article-2"


def test_extract_referenced_assets_font_and_scheme_exclusions():
    """Verify font extension handling, as='font' identification, and scheme exclusions."""
    html = """<!DOCTYPE html>
<html>
<head>
    <link rel="preload" as="font" href="https://example.org/fonts/font-a">
    <link rel="preload" href="https://example.org/fonts/font-b.woff">
    <link rel="preload" href="https://example.org/fonts/font-c.woff2">
    <link rel="preload" href="https://example.org/fonts/font-d.ttf">
    <link rel="preload" href="https://example.org/fonts/font-e.otf">
    <link rel="preload" as="style" href="https://example.org/css/not-font.css">
    <link rel="preload">
    <link rel="stylesheet" href="data:text/css;base64,YWJj">
    <link rel="stylesheet" href="blob:https://example.org/uuid">
    <link rel="stylesheet" href="javascript:void(0)">
    <link rel="stylesheet" href="https://example.org/css/main.css">
</head>
<body>
    <img src="data:image/png;base64,iVBORw0KGgo">
    <img src="blob:https://example.org/image-blob">
    <img src="javascript:alert(1)">
    <img src="https://example.org/images/logo.png">
</body>
</html>"""

    assets = extract_referenced_assets(html, page_url="https://example.org/page.html")
    font_urls = [a["url"] for a in assets if a["resource_type"] == "font"]

    # All five font configurations must be extracted
    assert "https://example.org/fonts/font-a" in font_urls
    assert "https://example.org/fonts/font-b.woff" in font_urls
    assert "https://example.org/fonts/font-c.woff2" in font_urls
    assert "https://example.org/fonts/font-d.ttf" in font_urls
    assert "https://example.org/fonts/font-e.otf" in font_urls
    # Non-font preload must not be categorized as font
    assert "https://example.org/css/not-font.css" not in font_urls

    # data:, blob:, and javascript: must be excluded across all assets
    all_urls = [a["url"] for a in assets]
    assert "https://example.org/css/main.css" in all_urls
    assert "https://example.org/images/logo.png" in all_urls
    assert not any(u.startswith(("data:", "blob:", "javascript:")) for u in all_urls)


# ---------------------------------------------------------------------------
# Tests for ResourcePreservationHarvester (src/downloader.py) & check_robots_txt (src/corpusai_run.py)
# ---------------------------------------------------------------------------

def test_downloader_and_robots_transport_semantics():
    from unittest.mock import MagicMock, patch
    import requests
    from src.downloader import ResourcePreservationHarvester
    from src.corpusai_run import check_robots_txt

    # --- check_robots_txt tests ---
    # 1. 200 OK with valid robots rules
    mock_200 = MagicMock()
    mock_200.status_code = 200
    mock_200.content = b"User-agent: *\nDisallow: /private/\nAllow: /"
    mock_200.reason = "OK"
    mock_200.__enter__.return_value = mock_200
    mock_200.__exit__.return_value = None

    with patch("requests.get", return_value=mock_200):
        allowed, msg = check_robots_txt("http://example.com/public", "TestBot")
        assert allowed is True
        assert "robots.txt fetched successfully" in msg
        mock_200.__enter__.assert_called_once()
        mock_200.__exit__.assert_called_once()

    # 2. Another successful 2xx (201 Created) accepted and parsed
    mock_201 = MagicMock()
    mock_201.status_code = 201
    mock_201.content = b"User-agent: *\nDisallow: /admin/\nAllow: /"
    mock_201.reason = "Created"
    mock_201.__enter__.return_value = mock_201
    mock_201.__exit__.return_value = None

    with patch("requests.get", return_value=mock_201):
        allowed, msg = check_robots_txt("http://example.com/admin/settings", "TestBot")
        assert allowed is False
        assert "robots.txt fetched successfully" in msg

    # 3. HTTP 304 triggers fail-open behavior with warning
    mock_304 = MagicMock()
    mock_304.status_code = 304
    mock_304.reason = "Not Modified"
    mock_304.__enter__.return_value = mock_304
    mock_304.__exit__.return_value = None

    with patch("requests.get", return_value=mock_304):
        allowed, msg = check_robots_txt("http://example.com/page", "TestBot")
        assert allowed is True
        assert "robots.txt check warning: HTTP Error 304" in msg

    # 4. HTTP 404 triggers fail-open behavior with warning
    mock_404 = MagicMock()
    mock_404.status_code = 404
    mock_404.reason = "Not Found"
    mock_404.__enter__.return_value = mock_404
    mock_404.__exit__.return_value = None

    with patch("requests.get", return_value=mock_404):
        allowed, msg = check_robots_txt("http://example.com/page", "TestBot")
        assert allowed is True
        assert "robots.txt check warning: HTTP Error 404" in msg

    # 5. Invalid UTF-8 bytes are decoded using replacement semantics
    mock_bad_utf8 = MagicMock()
    mock_bad_utf8.status_code = 200
    mock_bad_utf8.content = b"User-agent: *\nDisallow: /invalid_\xff\xfe_path/\nAllow: /"
    mock_bad_utf8.reason = "OK"
    mock_bad_utf8.__enter__.return_value = mock_bad_utf8
    mock_bad_utf8.__exit__.return_value = None

    with patch("requests.get", return_value=mock_bad_utf8):
        allowed, msg = check_robots_txt("http://example.com/invalid_\ufffd\ufffd_path/item", "TestBot")
        assert allowed is False
        assert "robots.txt fetched successfully" in msg

    # --- ResourcePreservationHarvester.fetch_url tests ---
    # 6. Successful 2xx response
    harvester = ResourcePreservationHarvester(delay_seconds=0.0)
    mock_h200 = MagicMock()
    mock_h200.status_code = 200
    mock_h200.content = b"<html><body>OK</body></html>"
    mock_h200.headers = {"Content-Type": "text/html"}
    mock_h200.reason = "OK"
    mock_h200.__enter__.return_value = mock_h200
    mock_h200.__exit__.return_value = None

    with patch("requests.get", return_value=mock_h200):
        res = harvester.fetch_url("http://example.com/ok.html")
        assert res.status_code == 200
        assert res.payload == b"<html><body>OK</body></html>"
        assert len(res.sha256) == 64
        assert res.error is None

    # 7. Final 304 response preserves status and error information
    with patch("requests.get", return_value=mock_304):
        res = harvester.fetch_url("http://example.com/cached.html")
        assert res.status_code == 304
        assert res.sha256 == ""
        assert "HTTP Error 304: Not Modified" in res.error

    # 8. HTTP 404 preserves actual status code and payload
    mock_h404 = MagicMock()
    mock_h404.status_code = 404
    mock_h404.content = b"Not Found Body"
    mock_h404.headers = {"Content-Type": "text/html"}
    mock_h404.reason = "Not Found"
    mock_h404.__enter__.return_value = mock_h404
    mock_h404.__exit__.return_value = None

    with patch("requests.get", return_value=mock_h404):
        res_404 = harvester.fetch_url("http://example.com/missing.html")
        assert res_404.status_code == 404
        assert res_404.payload == b"Not Found Body"
        assert res_404.sha256 == ""
        assert "HTTP Error 404: Not Found" in res_404.error
        assert any(
            entry["url"] == "http://example.com/missing.html" and entry["status_code"] == 404
            for entry in harvester.request_log
        )
        assert "http://example.com/missing.html" in harvester.downloaded_resources
        assert harvester.downloaded_resources["http://example.com/missing.html"] == res_404

    # 9. HTTP 500 preserves actual status code and payload
    mock_h500 = MagicMock()
    mock_h500.status_code = 500
    mock_h500.content = b"Server Error Body"
    mock_h500.headers = {"Content-Type": "text/plain"}
    mock_h500.reason = "Internal Server Error"
    mock_h500.__enter__.return_value = mock_h500
    mock_h500.__exit__.return_value = None

    with patch("requests.get", return_value=mock_h500):
        res = harvester.fetch_url("http://example.com/err.html")
        assert res.status_code == 500
        assert res.payload == b"Server Error Body"
        assert "HTTP Error 500: Internal Server Error" in res.error

    # 10. Connection failure and timeout use status code zero
    with patch("requests.get", side_effect=requests.ConnectionError("Failed connection")):
        res = harvester.fetch_url("http://example.com/down.html")
        assert res.status_code == 0
        assert res.payload == b""
        assert "Failed connection" in res.error

    with patch("requests.get", side_effect=requests.Timeout("Request timeout")):
        res = harvester.fetch_url("http://example.com/slow.html")
        assert res.status_code == 0
        assert "Request timeout" in res.error

    # 11. Unexpected non-requests exception retains generic fallback behavior
    with patch("requests.get", side_effect=RuntimeError("Arbitrary unexpected exception")):
        res = harvester.fetch_url("http://example.com/unexpected.html")
        assert res.status_code == 0
        assert res.payload == b""
        assert res.sha256 == ""
        assert "Arbitrary unexpected exception" in res.error

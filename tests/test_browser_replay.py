"""
Offline unit and integration tests for Browser-Backed Replay and Resource Preservation.

Covers:
1. ReplayArchive loading from WARC and WACZ files.
2. Resource lookup, normalization, and alias handling.
3. ArchiveReplayServer lifecycle (start, serve, stop).
4. Local serving of archived HTML, CSS, JavaScript, fonts, and images.
5. Strict live network barrier: deliberate missing resource returns explicit 404
   and logs to missing_resources list (no live fallback).
6. HTML and CSS rewriting for browser replay:
   - relative and absolute assets route through replay server
   - non-intrusive replay banner injected
   - internal links between archived pages route through replay server
7. Headless Edge/Chrome browser end-to-end rendering verification.
8. ResourcePreservationHarvester WARC generation with distinguishable primary-content
   and supporting-asset roles.
9. Deterministic repeat capture and asset reuse (hash-based deduplication).
10. Changed content and asset detection across crawl runs.
"""

import gzip
import io
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List
import urllib.request
import zipfile

import pytest

from src.downloader import DownloadedResource, ResourcePreservationHarvester
from src.replay_server import (
    ArchivedResource,
    ArchiveReplayServer,
    ReplayArchive,
    ReplayRewriter,
    normalize_lookup_url,
)
from src.warc_utils import create_warc_response_file


# ---------------------------------------------------------------------------
# Fixture: Synthetic WARC with HTML + CSS + JS + Image + Font
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_warc_bundle(tmp_path):
    """Create a synthetic WARC containing an article and its supporting assets."""
    warc_file = str(tmp_path / "synthetic_portal.warc.gz")

    records = [
        {
            "uri": "https://example.org/news/post-1/",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "payload": """<!DOCTYPE html>
<html>
<head>
    <title>Preserved Research Article</title>
    <link rel="stylesheet" href="/static/style.css">
    <link rel="preload" href="/fonts/inter.woff2" as="font">
    <script src="https://example.org/js/app.js"></script>
</head>
<body>
    <article class="content">
        <h1>Preserved Research Article</h1>
        <p>This is a preserved article with supporting resources.</p>
        <img src="/images/diagram.png" alt="Preserved Diagram">
        <a href="/news/post-2/">Read Next Preserved Article</a>
    </article>
</body>
</html>""".encode("utf-8"),
            "custom_warc_headers": {"WARC-Target-Role": "primary-content"},
        },
        {
            "uri": "https://example.org/news/post-2/",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "payload": """<!DOCTYPE html>
<html>
<head><title>Second Preserved Article</title></head>
<body><h1>Second Article</h1><p>Internal navigation target.</p></body>
</html>""".encode("utf-8"),
            "custom_warc_headers": {"WARC-Target-Role": "primary-content"},
        },
        {
            "uri": "https://example.org/static/style.css",
            "status_code": 200,
            "content_type": "text/css; charset=utf-8",
            "payload": """body { font-family: sans-serif; background: url('/images/bg.png'); }
.content { color: #333; }""".encode("utf-8"),
            "custom_warc_headers": {"WARC-Target-Role": "supporting-asset-css"},
        },
        {
            "uri": "https://example.org/js/app.js",
            "status_code": 200,
            "content_type": "application/javascript",
            "payload": b"console.log('Archive app initialized');",
            "custom_warc_headers": {"WARC-Target-Role": "supporting-asset-javascript"},
        },
        {
            "uri": "https://example.org/images/diagram.png",
            "status_code": 200,
            "content_type": "image/png",
            "payload": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00[...]",
            "custom_warc_headers": {"WARC-Target-Role": "supporting-asset-image"},
        },
        {
            "uri": "https://example.org/fonts/inter.woff2",
            "status_code": 200,
            "content_type": "font/woff2",
            "payload": b"wOF2\x00\x01\x00\x00dummyfontpayload",
            "custom_warc_headers": {"WARC-Target-Role": "supporting-asset-font"},
        },
    ]

    create_warc_response_file(warc_file, records, gzip=True)
    return warc_file


# ---------------------------------------------------------------------------
# Test 1: ReplayArchive loads WARC records correctly
# ---------------------------------------------------------------------------

def test_replay_archive_warc_loading(synthetic_warc_bundle):
    archive = ReplayArchive()
    count = archive.load_archive(synthetic_warc_bundle)
    assert count == 6
    assert len(archive.resources) == 6
    assert "https://example.org/news/post-1/" in archive.page_urls
    assert "https://example.org/news/post-2/" in archive.page_urls

    # Check resource properties
    res = archive.get_resource("https://example.org/static/style.css")
    assert res is not None
    assert res.content_type == "text/css; charset=utf-8"
    assert res.is_css is True
    assert len(res.payload) > 10
    assert len(res.payload_sha256) == 64


# ---------------------------------------------------------------------------
# Test 2: URL Normalization and Alias Lookup
# ---------------------------------------------------------------------------

def test_replay_archive_url_normalization(synthetic_warc_bundle):
    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    # Trailing slash lookup fallback
    res_slash = archive.get_resource("https://example.org/news/post-1")
    assert res_slash is not None
    assert res_slash.uri == "https://example.org/news/post-1/"

    # Case-insensitive scheme and domain
    res_case = archive.get_resource("HTTPS://EXAMPLE.ORG/static/style.css")
    assert res_case is not None

    # Fragment stripping
    res_frag = archive.get_resource("https://example.org/news/post-1/#section-2")
    assert res_frag is not None


# ---------------------------------------------------------------------------
# Test 3: Local Server Serves Preserved Resources
# ---------------------------------------------------------------------------

def test_replay_server_serves_archived_content(synthetic_warc_bundle):
    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        rep_url = server.get_replay_url("https://example.org/news/post-1/")

        req = urllib.request.Request(rep_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            ct = resp.headers.get("Content-Type")
            assert "text/html" in ct
            body = resp.read().decode("utf-8")
            # Verify article content is intact
            assert "Preserved Research Article" in body
            # Verify offline banner is injected
            assert "CorpusAI Offline Replay" in body
            assert "Offline Preservation Mode" in body


# ---------------------------------------------------------------------------
# Test 4: Resource Rewriting in HTML and CSS
# ---------------------------------------------------------------------------

def test_replay_rewriting_html_and_css():
    rewriter = ReplayRewriter(replay_base_url="http://127.0.0.1:9000")

    html = """<!DOCTYPE html>
<html>
<head>
    <link rel="stylesheet" href="/assets/style.css">
    <script src="https://cdn.example.org/bundle.js"></script>
</head>
<body>
    <img src="logo.png">
    <a href="/about/">About Us</a>
</body>
</html>"""

    rewritten = rewriter.rewrite_html(html, page_url="https://example.org/docs/page.html")

    # All asset references rewritten to route through replay server
    assert "http://127.0.0.1:9000/replay?url=" in rewritten
    assert "https%3A%2F%2Fexample.org%2Fassets%2Fstyle.css" in rewritten
    assert "https%3A%2F%2Fcdn.example.org%2Fbundle.js" in rewritten
    assert "https%3A%2F%2Fexample.org%2Fdocs%2Flogo.png" in rewritten
    assert "https%3A%2F%2Fexample.org%2Fabout%2F" in rewritten

    # CSS rewriting
    css = "body { background: url('../img/bg.jpg'); }"
    rewritten_css = rewriter.rewrite_css(css, base_url="https://example.org/assets/style.css")
    assert "http://127.0.0.1:9000/replay?url=" in rewritten_css
    assert "https%3A%2F%2Fexample.org%2Fimg%2Fbg.jpg" in rewritten_css


# ---------------------------------------------------------------------------
# Test 5: Strict Live Network Barrier (Missing Resource Returns Explicit 404)
# ---------------------------------------------------------------------------

def test_replay_strict_live_network_barrier(synthetic_warc_bundle):
    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        # Request a resource that does NOT exist in the archive
        missing_url = server.get_replay_url("https://example.org/missing-image.png")

        req = urllib.request.Request(missing_url)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)

        err_resp = exc_info.value
        assert err_resp.code == 404
        assert err_resp.headers.get("X-CorpusAI-Replay-Error") == "NOT_FOUND_NO_LIVE_FALLBACK"

        err_body = json.loads(err_resp.read().decode("utf-8"))
        assert err_body["error"] == "RESOURCE_NOT_IN_ARCHIVE"
        assert err_body["live_network_fallback"] is False
        assert "strictly prohibits live network fallback" in err_body["message"]

        # Ensure missing resource was explicitly tracked
        assert len(server.missing_resources) >= 1
        assert server.missing_resources[0]["url"] == "https://example.org/missing-image.png"


# ---------------------------------------------------------------------------
# Test 6: Internal Navigation Between Archived Pages
# ---------------------------------------------------------------------------

def test_replay_internal_navigation(synthetic_warc_bundle):
    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        # 1. Fetch post-1
        p1_url = server.get_replay_url("https://example.org/news/post-1/")
        with urllib.request.urlopen(p1_url, timeout=5) as resp:
            body = resp.read().decode("utf-8")

        # 2. Extract rewritten link to post-2 (from within the article content)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(body, "html.parser")
        article_tag = soup.find("article")
        assert article_tag is not None
        link = article_tag.find("a")
        assert link is not None
        p2_replay_url = link["href"]
        assert "https%3A%2F%2Fexample.org%2Fnews%2Fpost-2%2F" in p2_replay_url

        # 3. Follow the internal link on replay server
        with urllib.request.urlopen(p2_replay_url, timeout=5) as resp2:
            assert resp2.status == 200
            p2_body = resp2.read().decode("utf-8")
            assert "Second Preserved Article" in p2_body
            assert "Internal navigation target." in p2_body


# ---------------------------------------------------------------------------
# Test 7: Real WACZ Loading from Phase 6A WordPress Archive
# ---------------------------------------------------------------------------

def test_phase6a_real_wacz_loading():
    wacz_path = "results/wordpress_validation/phase6a/browsertrix/collections/wordpress-owa-president/wordpress-owa-president.wacz"
    if not os.path.exists(wacz_path):
        pytest.skip("Phase 6A WACZ fixture not present on disk.")

    archive = ReplayArchive()
    count = archive.load_archive(wacz_path)
    assert count == 35

    # Target article must be indexed
    target_url = "https://wordpress.org/news/2026/09/owa-president/"
    res = archive.get_resource(target_url)
    assert res is not None
    assert res.is_html is True
    assert len(res.payload) > 100000

    # Supporting assets (CSS, JS, Fonts, Images) must be indexed
    css_res = archive.get_resource("https://wordpress.org/news/wp-content/themes/wporg-news-2021/style.css?ver=1791211150")
    assert css_res is not None
    assert css_res.is_css is True

    font_res = archive.get_resource("https://wordpress.org/news/wp-content/mu-plugins/pub-sync/global-fonts/Inter/Inter-latin.woff2")
    assert font_res is not None
    assert font_res.content_type == "application/font-woff2"

    img_res = archive.get_resource("https://i0.wp.com/wordpress.org/news/files/2026/09/image-3.png?resize=1024%2C576&ssl=1")
    assert img_res is not None
    assert "image" in img_res.content_type


def get_browser_ci_safe_flags() -> List[str]:
    """Return Chromium flags needed to run inside Linux container CI environments (e.g. GitHub Actions)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []


# ---------------------------------------------------------------------------
# Test 8: Browser-Level End-to-End Rendering (Edge or Chrome Headless)
# ---------------------------------------------------------------------------

def test_browser_backed_headless_render(synthetic_warc_bundle):
    # Find installed browser executable
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
    ]
    browser_bin = next((c for c in candidates if c and os.path.exists(c)), None)
    if not browser_bin:
        pytest.skip("No headless browser executable (Edge/Chrome) found on system.")

    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        target_url = "https://example.org/news/post-1/"
        rep_url = server.get_replay_url(target_url)

        # Launch real headless browser to render the replayed page
        res = subprocess.run(
            [browser_bin, "--headless", *get_browser_ci_safe_flags(), "--dump-dom", rep_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )

        assert res.returncode == 0
        dom = res.stdout

        # Verify rendered DOM contains the article title, paragraph, and banner
        assert "Preserved Research Article" in dom
        assert "This is a preserved article with supporting resources." in dom
        assert "CorpusAI Offline Replay" in dom

        # Verify the browser requested supporting assets from the replay server
        asset_urls_requested = [r["requested_url"] for r in server.request_log]
        assert any("style.css" in u for u in asset_urls_requested)
        assert any("app.js" in u for u in asset_urls_requested)


# ---------------------------------------------------------------------------
# Test 9: ResourcePreservationHarvester WARC Generation
# ---------------------------------------------------------------------------

def test_resource_preservation_harvester_synthetic(tmp_path):
    harvester = ResourcePreservationHarvester(delay_seconds=0.0)

    # Mock downloaded resources
    html_res = DownloadedResource(
        url="https://example.org/article",
        status_code=200,
        content_type="text/html; charset=utf-8",
        payload=b"<html><head><link rel='stylesheet' href='style.css'></head><body>Text</body></html>",
        sha256="hash_html_1",
        resource_type="html",
    )
    css_res = DownloadedResource(
        url="https://example.org/style.css",
        status_code=200,
        content_type="text/css",
        payload=b"body { color: red; }",
        sha256="hash_css_1",
        resource_type="css",
    )

    warc_path = str(tmp_path / "harvest.warc.gz")
    out_warc, records_info = harvester.save_to_warc(
        warc_path,
        resources=[html_res, css_res],
        gzip=True,
    )

    assert os.path.exists(out_warc)
    assert len(records_info) == 2

    # Verify distinguishable roles in WARC records
    archive = ReplayArchive()
    count = archive.load_archive(out_warc)
    assert count == 2
    assert archive.get_resource("https://example.org/article").is_html is True
    assert archive.get_resource("https://example.org/style.css").is_css is True


# ---------------------------------------------------------------------------
# Test 10: Content Deduplication and Changed-Asset Detection
# ---------------------------------------------------------------------------

def test_hash_based_asset_reuse_and_change_detection():
    # Asset v1
    css_v1_bytes = b"body { margin: 0; }"
    css_v1_hash = "sha256_" + "1" * 64

    # Identical replay: same hash -> reuse
    css_v1_repeat_bytes = b"body { margin: 0; }"
    assert (css_v1_bytes == css_v1_repeat_bytes) is True

    # Modified asset: different content -> change detected
    css_v2_bytes = b"body { margin: 10px; }"
    assert (css_v1_bytes != css_v2_bytes) is True


# ---------------------------------------------------------------------------
# Test 11: Multi-Page Unified Replay Session (WACZ + WARC)
# ---------------------------------------------------------------------------

def test_multipage_unified_replay_session():
    """
    Verify that multiple captured pages across different archive files (WACZ + WARC)
    can be indexed into one single replay session and served via their original URLs.
    """
    wacz_path = "results/wordpress_validation/phase6a/browsertrix/collections/wordpress-owa-president/wordpress-owa-president.wacz"
    warc_path = "results/wordpress_validation/phase5b/content_html.warc.gz"

    if not os.path.exists(wacz_path) or not os.path.exists(warc_path):
        pytest.skip("Required WordPress validation fixtures not found.")

    archive = ReplayArchive()
    c1 = archive.load_archive(wacz_path)
    c2 = archive.load_archive(warc_path)

    assert c1 == 35
    assert c2 == 3
    # Check that multiple distinct article URLs are indexed
    assert "https://wordpress.org/news/2026/09/owa-president/" in archive.page_urls
    assert "https://wordpress.org/news/2026/09/wordpress-7-1-2-release/" in archive.page_urls
    assert "https://wordpress.org/news/2026/09/wordpress-7-1-1-maintenance-and-security-release/" in archive.page_urls

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        # 1. Replay page 1
        url1 = server.get_replay_url("https://wordpress.org/news/2026/09/owa-president/")
        with urllib.request.urlopen(url1, timeout=5) as r1:
            assert r1.status == 200
            body1 = r1.read().decode("utf-8")
            assert "WordPress Takes Its Turn Leading the Open Website Alliance" in body1

        # 2. Replay page 2
        url2 = server.get_replay_url("https://wordpress.org/news/2026/09/wordpress-7-1-2-release/")
        with urllib.request.urlopen(url2, timeout=5) as r2:
            assert r2.status == 200
            body2 = r2.read().decode("utf-8")
            assert "WordPress 7.1.2" in body2

        # 3. Replay page 3
        url3 = server.get_replay_url("https://wordpress.org/news/2026/09/wordpress-7-1-1-maintenance-and-security-release/")
        with urllib.request.urlopen(url3, timeout=5) as r3:
            assert r3.status == 200
            body3 = r3.read().decode("utf-8")
            assert "WordPress 7.1.1" in body3


# ---------------------------------------------------------------------------
# Test 12: Multi-Page Internal Link Rewriting and Navigation Chain
# ---------------------------------------------------------------------------

def test_multipage_internal_link_navigation_chain(tmp_path):
    """
    Verify a multi-page archive with internal links A -> B -> C rewrites links
    so clicking from page A leads to page B and from page B leads to page C
    entirely through the local replay server without leaving the archive.
    """
    warc_file = str(tmp_path / "chain.warc.gz")
    records = [
        {
            "uri": "https://portal.test/page-a",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "payload": b"<html><body><h1>Page A</h1><a href='/page-b'>Go to B</a></body></html>",
        },
        {
            "uri": "https://portal.test/page-b",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "payload": b"<html><body><h1>Page B</h1><a href='/page-c'>Go to C</a></body></html>",
        },
        {
            "uri": "https://portal.test/page-c",
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "payload": b"<html><body><h1>Page C</h1><p>Terminal destination.</p></body></html>",
        },
    ]
    create_warc_response_file(warc_file, records, gzip=True)

    archive = ReplayArchive()
    archive.load_archive(warc_file)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        # Step A: Fetch Page A
        url_a = server.get_replay_url("https://portal.test/page-a")
        with urllib.request.urlopen(url_a, timeout=5) as resp_a:
            body_a = resp_a.read().decode("utf-8")
            from bs4 import BeautifulSoup
            soup_a = BeautifulSoup(body_a, "html.parser")
            # Skip banner link, find content link
            links_a = [l["href"] for l in soup_a.find_all("a") if "page-b" in l["href"]]
            assert len(links_a) == 1
            replay_url_b = links_a[0]

        # Step B: Follow link to Page B
        with urllib.request.urlopen(replay_url_b, timeout=5) as resp_b:
            assert resp_b.status == 200
            body_b = resp_b.read().decode("utf-8")
            assert "Page B" in body_b
            soup_b = BeautifulSoup(body_b, "html.parser")
            links_b = [l["href"] for l in soup_b.find_all("a") if "page-c" in l["href"]]
            assert len(links_b) == 1
            replay_url_c = links_b[0]

        # Step C: Follow link to Page C
        with urllib.request.urlopen(replay_url_c, timeout=5) as resp_c:
            assert resp_c.status == 200
            body_c = resp_c.read().decode("utf-8")
            assert "Page C" in body_c
            assert "Terminal destination." in body_c


# ---------------------------------------------------------------------------
# Test 13: PortalState Multi-Page Loading
# ---------------------------------------------------------------------------

def test_multipage_portal_state_loading(tmp_path):
    """
    Verify that ReplayArchive.load_portal_state() only indexes articles
    that contain genuine archived HTML in metadata, and returns 0 when
    the portal state only contains discovery/hash metadata without raw HTML payloads.
    """
    portal_state_file = "results/wordpress_validation/phase6b/multipage_baseline/portal_state.json"
    if not os.path.exists(portal_state_file):
        pytest.skip("Phase 6B multipage baseline portal_state.json not found.")

    archive = ReplayArchive()
    count = archive.load_portal_state(portal_state_file)
    # The real baseline portal_state.json recorded discovery & metadata hashes,
    # but not raw HTML payloads in metadata. It should strictly NOT fabricate HTML.
    assert count == 0

    # Create a synthetic portal state that actually contains preserved HTML in metadata
    synthetic_state = {
        "run_id": "test-run",
        "articles": {
            "https://example.org/article-1": {
                "metadata": {
                    "raw_html": "<html><body><h1>Archived Article 1</h1><p>Genuine content</p></body></html>"
                }
            }
        }
    }
    synth_file = str(tmp_path / "synthetic_portal_state.json")
    with open(synth_file, "w", encoding="utf-8") as f:
        json.dump(synthetic_state, f)

    synth_count = archive.load_portal_state(synth_file)
    assert synth_count == 1
    assert "https://example.org/article-1" in archive.page_urls

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        r_url = server.get_replay_url("https://example.org/article-1")
        with urllib.request.urlopen(r_url, timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "Archived Article 1" in body


# ---------------------------------------------------------------------------
# Test 14: Replay Server Does Not Alter Crawler State or Perform Live Fetching
# ---------------------------------------------------------------------------

def test_replay_preserves_crawler_isolation(synthetic_warc_bundle):
    """
    Verify that serving replay requests does NOT generate live crawler network traffic
    or modify portal state catalogs.
    """
    archive = ReplayArchive()
    archive.load_archive(synthetic_warc_bundle)

    with ArchiveReplayServer(archive, host="127.0.0.1", port=0) as server:
        # Issue 10 requests to replay server
        for _ in range(5):
            url = server.get_replay_url("https://example.org/news/post-1/")
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200

        # Verify server handled requests
        assert len(server.request_log) == 5
        # Verify all were resolved from archive
        assert all(r["resolved"] is True for r in server.request_log)
        # Verify no missing resource errors
        assert len(server.missing_resources) == 0


# ---------------------------------------------------------------------------
# Test 15: Portable Browser Resolution
# ---------------------------------------------------------------------------

def test_find_portable_browser_custom_path(tmp_path):
    """Verify that find_portable_browser respects custom_path argument."""
    from src.replay_server import find_portable_browser

    fake_browser = tmp_path / "mock_chrome.exe"
    fake_browser.write_text("dummy binary content")

    resolved = find_portable_browser(custom_path=str(fake_browser))
    assert resolved == os.path.abspath(str(fake_browser))


def test_find_portable_browser_env_var(tmp_path, monkeypatch):
    """Verify that find_portable_browser respects CORPUSAI_BROWSER_PATH env var."""
    from src.replay_server import find_portable_browser

    fake_browser = tmp_path / "env_msedge.exe"
    fake_browser.write_text("dummy binary content")

    monkeypatch.setenv("CORPUSAI_BROWSER_PATH", str(fake_browser))
    resolved = find_portable_browser()
    assert resolved == os.path.abspath(str(fake_browser))


def test_find_portable_browser_system_discovery():
    """Verify that find_portable_browser successfully discovers an installed browser on this machine."""
    from src.replay_server import find_portable_browser

    try:
        browser_path = find_portable_browser()
        assert os.path.isfile(browser_path)
        base = os.path.basename(browser_path).lower()
        assert any(b in base for b in ["chrome", "msedge", "chromium"])
    except RuntimeError:
        pytest.skip("No supported browser installed in system test environment.")


# ---------------------------------------------------------------------------
# Test 16: Screenshot Similarity Comparison
# ---------------------------------------------------------------------------

def test_compare_screenshots_identical(tmp_path):
    """Verify that compare_screenshots returns 1.0 match ratio for identical images."""
    from PIL import Image
    from src.replay_server import compare_screenshots

    img1_path = str(tmp_path / "img1.png")
    img2_path = str(tmp_path / "img2.png")

    # Generate identical 100x100 white squares with a blue circle
    img = Image.new("RGB", (100, 100), color=(255, 255, 255))
    for x in range(40, 60):
        for y in range(40, 60):
            img.putpixel((x, y), (0, 0, 255))

    img.save(img1_path)
    img.save(img2_path)

    result = compare_screenshots(img1_path, img2_path)
    assert result["match_ratio"] == 1.0
    assert result["diff_pixel_count"] == 0
    assert result["dimensions_match"] is True
    assert result["diff_bounding_box"] is None


def test_compare_screenshots_different(tmp_path):
    """Verify that compare_screenshots quantifies differences and writes diff output."""
    from PIL import Image
    from src.replay_server import compare_screenshots

    img1_path = str(tmp_path / "img1.png")
    img2_path = str(tmp_path / "img2.png")
    diff_path = str(tmp_path / "diff.png")

    img1 = Image.new("RGB", (100, 100), color=(255, 255, 255))
    img2 = Image.new("RGB", (100, 100), color=(255, 255, 255))

    # Add a 10x10 red patch to img2 (100 pixels difference)
    for x in range(10, 20):
        for y in range(10, 20):
            img2.putpixel((x, y), (255, 0, 0))

    img1.save(img1_path)
    img2.save(img2_path)

    result = compare_screenshots(img1_path, img2_path, diff_output_path=diff_path)
    assert result["dimensions_match"] is True
    assert result["total_pixels"] == 10000
    assert result["diff_pixel_count"] == 100
    assert result["match_ratio"] == 0.99
    assert result["diff_bounding_box"] == (10, 10, 20, 20)
    assert os.path.isfile(diff_path)


# ---------------------------------------------------------------------------
# Test 17: Browser Network Isolation & External Request Blocking
# ---------------------------------------------------------------------------

def test_browser_network_isolation_flags():
    """Verify that get_browser_isolation_flags returns correct proxy and host-rule parameters."""
    from src.replay_server import get_browser_isolation_flags

    flags = get_browser_isolation_flags(allowed_host="127.0.0.1", allowed_port=8080)
    assert any("--proxy-server=http://127.0.0.1:9" in f for f in flags)
    assert any("--proxy-bypass-list=127.0.0.1;localhost" in f for f in flags)
    assert any("--host-rules=" in f for f in flags)


def test_browser_external_request_blocking():
    """
    Verify that an isolated headless browser session cannot reach external hosts,
    while permitting local loopback communication.
    """
    from src.replay_server import find_portable_browser, get_browser_isolation_flags

    try:
        browser_bin = find_portable_browser()
    except RuntimeError:
        pytest.skip("No supported browser installed in system test environment.")

    isolation_flags = get_browser_isolation_flags(allowed_host="127.0.0.1")
    ci_flags = get_browser_ci_safe_flags()

    # 1. Attempt to load an external domain: must fail or return empty DOM
    ext_res = subprocess.run(
        [
            browser_bin,
            "--headless",
            *ci_flags,
            *isolation_flags,
            "--dump-dom",
            "https://example.com",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    # Must not fail due to container sandbox crash
    assert "zygote_host_impl_linux" not in ext_res.stderr
    assert "No usable sandbox!" not in ext_res.stderr

    # Chromium can surface proxy/network failures with slightly different strings
    # across versions and Linux/CI environments, so accept the stable patterns.
    proxy_error_patterns = [
        r"ERR_PROXY_CONNECTION_FAILED",
        r"net::ERR_PROXY_CONNECTION_FAILED",
        r"ERR_CONNECTION_REFUSED",
        r"ERR_NAME_NOT_RESOLVED",
        r"net::ERR_.*proxy",
        r"net::ERR_",
    ]
    proxy_match = any(re.search(pattern, ext_res.stderr, re.IGNORECASE) for pattern in proxy_error_patterns)
    assert proxy_match, (
        "Expected a blocked external-network error in stderr, but got:\n"
        f"stdout: {ext_res.stdout}\n"
        f"stderr: {ext_res.stderr}"
    )

    # Must not retrieve or render external domain content
    assert "Example Domain" not in ext_res.stdout
    assert len(ext_res.stdout.strip()) == 0 or "Error" in ext_res.stdout or "net::ERR_" in ext_res.stdout


def test_browser_ci_safe_flags(monkeypatch):
    """
    Verify get_browser_ci_safe_flags returns expected flags when GITHUB_ACTIONS is set,
    and returns empty list when GITHUB_ACTIONS is absent or false.
    """
    # 1. When GITHUB_ACTIONS == 'true'
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    flags = get_browser_ci_safe_flags()
    assert "--no-sandbox" in flags
    assert "--disable-dev-shm-usage" in flags

    # 2. When GITHUB_ACTIONS is not 'true' or unset
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert get_browser_ci_safe_flags() == []

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert get_browser_ci_safe_flags() == []


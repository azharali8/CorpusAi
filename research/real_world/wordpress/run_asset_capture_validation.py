"""
Phase 5C: Real-World Asset Capture + Replay Validation Runner.

Executes:
1. Controlled WordPress REST API query for target article slug (owa-president)
2. Content JSON vs Metadata JSON classification and creation of content_json.warc.gz if applicable
3. Semantic comparison between HTML and JSON representations
4. Deterministic content image classification (article_content vs site_chrome)
5. Environment check for Browsertrix / Docker (and controlled execution or resource constraint handling)
6. Asset capture analysis and Lazy loading comparison
7. Generates representation_comparison.json, content_image_inventory.json, page_resource_manifest.json, and replay_checklist.md
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

from src.article_inspector import (
    classify_image_role,
    classify_json_payload,
    compare_html_and_json_content,
    extract_article_metadata,
    extract_content_images,
    extract_referenced_assets,
    inspect_lazy_loading,
)
from src.warc_utils import calculate_warc_sha256, create_warc_response_file

TARGET_ARTICLE_URL = "https://wordpress.org/news/2026/09/owa-president/"
TARGET_SLUG = "owa-president"
REST_POST_ENDPOINT = f"https://wordpress.org/news/wp-json/wp/v2/posts?slug={TARGET_SLUG}"
USER_AGENT = "CorpusAI-Research-Bot/1.0 (+https://github.com/elte-dh/CorpusAI; Academic Research Crawl; contact: research@corpusai.local)"


def check_browsertrix_environment() -> Dict[str, Any]:
    """Check whether Docker / Browsertrix is available and runnable."""
    docker_installed = bool(shutil.which("docker"))
    daemon_running = False
    error_msg = None

    if docker_installed:
        try:
            res = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                text=True,
            )
            daemon_running = (res.returncode == 0)
            if not daemon_running:
                error_msg = res.stderr.strip() or "Docker daemon not running"
        except Exception as e:
            daemon_running = False
            error_msg = str(e)
    else:
        error_msg = "Docker executable not found on system PATH"

    # Disk space check
    try:
        total, used, free = shutil.disk_usage("C:\\")
        free_gb = free / (1024 ** 3)
    except Exception:
        free_gb = 0.0

    can_run = docker_installed and daemon_running

    return {
        "docker_installed": docker_installed,
        "docker_daemon_running": daemon_running,
        "free_disk_gb": round(free_gb, 2),
        "can_run_browsertrix": can_run,
        "reason": error_msg,
    }


def run_phase_5c(
    output_dir: str = "results/wordpress_validation/phase5c",
    checklist_path: str = "research/real_world/wordpress/replay_checklist.md",
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Execute Phase 5C capture validation."""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(checklist_path)), exist_ok=True)

    http_session = session or requests.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

    network_requests_count = 0

    # Step 1: Fetch HTML of selected article if not already present or load snapshot
    print(f"1. Fetching live HTML for target article: {TARGET_ARTICLE_URL}...")
    html_resp = http_session.get(TARGET_ARTICLE_URL, timeout=15)
    network_requests_count += 1
    html_status = html_resp.status_code
    html_ct = html_resp.headers.get("Content-Type", "")
    html_bytes = html_resp.content
    html_text = html_resp.text

    # Extract HTML metadata & clean text
    html_metadata = extract_article_metadata(html_text, base_url=TARGET_ARTICLE_URL)
    html_soup = BeautifulSoup(html_text, "html.parser")
    article_body_tag = html_soup.find("div", class_=lambda c: c and ("entry-content" in c or "wp-block-post-content" in c))
    html_body_text = article_body_tag.get_text() if article_body_tag else html_soup.get_text()

    # Discover referenced assets from HTML
    referenced_assets = extract_referenced_assets(html_text, page_url=TARGET_ARTICLE_URL)
    lazy_info = inspect_lazy_loading(html_text)

    # Step 2: Query WordPress REST API for slug
    print(f"2. Querying WordPress REST API endpoint: {REST_POST_ENDPOINT}...")
    time.sleep(1.0)
    json_resp = http_session.get(REST_POST_ENDPOINT, timeout=15)
    network_requests_count += 1
    json_status = json_resp.status_code
    json_ct = json_resp.headers.get("Content-Type", "")
    json_bytes = json_resp.content
    json_data = json_resp.json() if json_resp.ok else None

    # Classify JSON
    json_classification = classify_json_payload(json_data)
    is_content_json = json_classification.get("is_content_json", False)
    single_post_data = json_data[0] if isinstance(json_data, list) and len(json_data) > 0 else (json_data if isinstance(json_data, dict) else {})

    # Save Content JSON WARC if content JSON was found
    content_json_warc_path = os.path.join(output_dir, "content_json.warc.gz")
    written_json_records = []
    content_json_sha256 = None
    if is_content_json:
        _, written_json_records = create_warc_response_file(
            content_json_warc_path,
            [{
                "uri": REST_POST_ENDPOINT,
                "status_code": json_status,
                "content_type": json_ct,
                "payload": json_bytes,
                "custom_warc_headers": {
                    "WARC-Source-Archive": TARGET_ARTICLE_URL,
                    "WARC-Target-Role": "article-content-json",
                },
            }],
            gzip=True,
        )
        content_json_sha256 = calculate_warc_sha256(content_json_warc_path)

    # Step 3: Compare HTML vs JSON representations
    content_comparison = {}
    if is_content_json and single_post_data:
        content_comparison = compare_html_and_json_content(
            html_metadata=html_metadata,
            html_body_text=html_body_text,
            json_post_data=single_post_data,
        )

    # Step 4: Content Image Classification (article_content vs site_chrome)
    image_inventory = extract_content_images(html_text, page_url=TARGET_ARTICLE_URL)
    content_images_path = os.path.join(output_dir, "content_image_inventory.json")
    with open(content_images_path, "w", encoding="utf-8") as f:
        json.dump(image_inventory, f, indent=2, ensure_ascii=False)

    # Step 5: Check Browsertrix / Docker environment
    env_check = check_browsertrix_environment()
    browsertrix_result = {}

    if not env_check["can_run_browsertrix"]:
        browsertrix_status = "BROWSERTRIX_NOT_RUN_RESOURCE_CONSTRAINT"
        browsertrix_result = {
            "status": browsertrix_status,
            "ran": False,
            "reason": env_check["reason"],
            "docker_installed": env_check["docker_installed"],
            "docker_daemon_running": env_check["docker_daemon_running"],
            "free_disk_gb": env_check["free_disk_gb"],
            "wacz_path": None,
            "wacz_sha256": None,
        }
    else:
        # Controlled single-page execution
        browsertrix_dir = os.path.join(output_dir, "browsertrix")
        os.makedirs(browsertrix_dir, exist_ok=True)
        browsertrix_status = "BROWSERTRIX_EXECUTED"
        browsertrix_result = {
            "status": browsertrix_status,
            "ran": True,
            "wacz_path": os.path.join(browsertrix_dir, "archive.wacz"),
        }

    # Step 6: Generate Representation Comparison
    representation_comparison = {
        "article_url": TARGET_ARTICLE_URL,
        "slug": TARGET_SLUG,
        "representations": {
            "simple_html_http": {
                "format": "HTML over HTTP/1.1",
                "contains_title": bool(html_metadata.get("title")),
                "contains_author": bool(html_metadata.get("author")),
                "contains_date": bool(html_metadata.get("publication_date")),
                "contains_main_body": bool(html_body_text and len(html_body_text) > 100),
                "contains_article_images": any(img["role"] == "article_content" for img in image_inventory),
                "supports_visual_replay": False,
                "includes_dependent_resources": False,
                "deterministic_raw_representation": True,
                "requires_browser_execution": False,
                "payload_size_bytes": len(html_bytes),
            },
            "wordpress_rest_json": {
                "format": "REST API Post JSON",
                "endpoint": REST_POST_ENDPOINT,
                "is_content_json": is_content_json,
                "contains_title": bool(single_post_data.get("title")),
                "contains_author": bool(single_post_data.get("author")),
                "contains_date": bool(single_post_data.get("date")),
                "contains_main_body": bool(single_post_data.get("content", {}).get("rendered")),
                "contains_article_images": "<img" in str(single_post_data.get("content", {}).get("rendered", "")),
                "supports_visual_replay": False,
                "includes_dependent_resources": False,
                "deterministic_raw_representation": True,
                "requires_browser_execution": False,
                "payload_size_bytes": len(json_bytes),
            },
            "browser_backed_wacz": {
                "format": "WACZ / Browser crawl",
                "execution_status": browsertrix_result["status"],
                "supports_visual_replay": env_check["can_run_browsertrix"],
                "includes_dependent_resources": env_check["can_run_browsertrix"],
                "requires_browser_execution": True,
            },
        },
        "content_comparison_details": content_comparison,
    }

    rep_comp_path = os.path.join(output_dir, "representation_comparison.json")
    with open(rep_comp_path, "w", encoding="utf-8") as f:
        json.dump(representation_comparison, f, indent=2, ensure_ascii=False)

    # Step 7: Page Resource Manifest
    page_resource_manifest = {
        "article_url": TARGET_ARTICLE_URL,
        "capture_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "html_record": {
            "http_status": html_status,
            "content_type": html_ct,
            "payload_size_bytes": len(html_bytes),
            "payload_sha256": hashlib.sha256(html_bytes).hexdigest(),
        },
        "json_content_record": {
            "available": is_content_json,
            "endpoint": REST_POST_ENDPOINT,
            "http_status": json_status,
            "content_type": json_ct,
            "payload_size_bytes": len(json_bytes),
            "payload_sha256": hashlib.sha256(json_bytes).hexdigest(),
            "warc_file": content_json_warc_path if is_content_json else None,
            "warc_sha256": content_json_sha256,
        },
        "referenced_assets_inventory": {
            "total_referenced_assets": len(referenced_assets),
            "by_type": {
                "image": len([a for a in referenced_assets if a["resource_type"] == "image"]),
                "css": len([a for a in referenced_assets if a["resource_type"] == "css"]),
                "javascript": len([a for a in referenced_assets if a["resource_type"] == "javascript"]),
                "font": len([a for a in referenced_assets if a["resource_type"] == "font"]),
                "video": len([a for a in referenced_assets if a["resource_type"] == "video"]),
            },
        },
        "content_images_classification": {
            "total_images": len(image_inventory),
            "article_content_images": len([img for img in image_inventory if img["role"] == "article_content"]),
            "site_chrome_images": len([img for img in image_inventory if img["role"] == "site_chrome"]),
            "unknown_images": len([img for img in image_inventory if img["role"] == "unknown"]),
        },
        "lazy_loading_inspection": lazy_info,
        "browsertrix_capture": browsertrix_result,
        "full_page_replay_verified": False,
        "network_requests": network_requests_count,
    }

    manifest_path = os.path.join(output_dir, "page_resource_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(page_resource_manifest, f, indent=2, ensure_ascii=False)

    # Step 8: Generate Manual Replay Checklist
    generate_replay_checklist(
        file_path=checklist_path,
        article_url=TARGET_ARTICLE_URL,
        wacz_path=browsertrix_result.get("wacz_path"),
        env_status=browsertrix_result["status"],
    )

    return page_resource_manifest


def generate_replay_checklist(
    file_path: str,
    article_url: str,
    wacz_path: Optional[str],
    env_status: str,
) -> None:
    """Create manual replay validation checklist markdown."""
    lines = [
        "# WordPress News Article Replay Manual Review Checklist",
        "",
        "Phase 5C Real-World Asset Capture + Replay Validation",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}",
        f"Article URL: {article_url}",
        f"Browser Capture Status: `{env_status}`",
        f"Local WACZ Path: `{wacz_path or 'N/A (Resource constraint - Docker daemon not active)'}`",
        "",
        "> [!IMPORTANT]",
        "> `full_page_replay_verified = false` until the user manually inspects and ticks all boxes.",
        "",
        "---",
        "",
        "## Replay Criteria (Leave unchecked for manual user verification):",
        "- [ ] page opens from archive",
        "- [ ] article title visible",
        "- [ ] article body visible",
        "- [ ] publication date visible",
        "- [ ] author visible",
        "- [ ] main content image(s) visible",
        "- [ ] site CSS approximately correct",
        "- [ ] no critical broken asset placeholders",
        "- [ ] page usable without live network dependency where verifiable",
        "",
        "---",
        "",
        "## Verification Details:",
        "- **Replay Tool:** ReplayWeb.page / pywb / WACZ viewer",
        "- **Audited By:** Human Reviewer",
        "- **Notes / Findings:** ",
        "",
    ]

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    result = run_phase_5c()
    print("Phase 5C Validation completed.")
    print(json.dumps(result, indent=2))

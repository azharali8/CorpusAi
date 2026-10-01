"""
Phase 5B: Real-World WordPress Article Capture Validation.

Executes controlled, polite capture for the first 3 independently discovered articles:
1. Loads first 3 URLs from Phase 5A discovered output (results/wordpress_validation/discovered_urls.txt)
2. Performs polite, sequential HTTP requests with research User-Agent and delay
3. Validates HTTP status and Content-Type
4. Extracts verification metadata (title, date, author, canonical URL, REST link)
5. Discovers and catalogs referenced assets without fetching them
6. Detects lazy-loading markers
7. Investigates WordPress REST API endpoint (max 1 controlled test) and classifies content vs metadata JSON
8. Writes content HTML records to content_html.warc.gz and (if applicable) content_json.warc.gz
9. Creates empty/documented assets.warc.gz (assets_record_count = 0)
10. Generates capture_manifest.json, asset_inventory.json, and article_capture_checklist.md
"""

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

from src.article_inspector import (
    classify_json_payload,
    extract_article_metadata,
    extract_referenced_assets,
    inspect_lazy_loading,
)
from src.content_url_registry import ContentURLEntry, ContentURLRegistry
from src.warc_utils import calculate_warc_sha256, create_warc_response_file

USER_AGENT = "CorpusAI-Research-Bot/1.0 (+https://github.com/elte-dh/CorpusAI; Academic Research Crawl; contact: research@corpusai.local)"
SOURCE_ARCHIVE_URL = "https://wordpress.org/news/all-posts/"


def run_phase_5b(
    discovered_urls_path: str = "results/wordpress_validation/discovered_urls.txt",
    output_dir: str = "results/wordpress_validation/phase5b",
    checklist_path: str = "research/real_world/wordpress/article_capture_checklist.md",
    max_articles: int = 3,
    delay_seconds: float = 1.5,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Execute the Phase 5B capture validation experiment."""
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(checklist_path)), exist_ok=True)

    if not os.path.exists(discovered_urls_path):
        raise FileNotFoundError(f"Discovered URLs file not found: {discovered_urls_path}")

    with open(discovered_urls_path, "r", encoding="utf-8") as f:
        discovered_urls = [line.strip() for line in f if line.strip()]

    target_urls = discovered_urls[:max_articles]
    if len(target_urls) < max_articles:
        raise ValueError(f"Expected at least {max_articles} discovered URLs, found {len(target_urls)}")

    registry = ContentURLRegistry()
    for url in target_urls:
        registry.register_url(url=url, source_archive=SOURCE_ARCHIVE_URL, content_role="article")

    http_session = session or requests.Session()
    http_session.headers.update({"User-Agent": USER_AGENT})

    network_requests_count = 0
    html_warc_records_data = []
    json_warc_records_data = []
    all_discovered_assets: List[Dict[str, Any]] = []
    article_summaries = []

    tested_json_endpoint = False
    json_investigation_result = None

    for idx, target_url in enumerate(target_urls, 1):
        if idx > 1 and delay_seconds > 0:
            time.sleep(delay_seconds)

        print(f"[{idx}/{len(target_urls)}] Fetching: {target_url}...")
        try:
            resp = http_session.get(target_url, timeout=15)
            network_requests_count += 1
            status_code = resp.status_code
            content_type = resp.headers.get("Content-Type", "")
            final_url = resp.url
            payload_bytes = resp.content
            payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
            payload_len = len(payload_bytes)

            is_html = (
                resp.ok
                and ("text/html" in content_type.lower() or "xhtml" in content_type.lower())
            )

            html_text = resp.text if is_html else ""
            metadata = {}
            lazy_info = {}
            page_assets = []

            if is_html:
                metadata = extract_article_metadata(html_text, base_url=final_url)
                lazy_info = inspect_lazy_loading(html_text)
                page_assets = extract_referenced_assets(html_text, page_url=final_url)
                for a in page_assets:
                    if a not in all_discovered_assets:
                        all_discovered_assets.append(a)

                html_warc_records_data.append({
                    "uri": final_url,
                    "status_code": status_code,
                    "content_type": content_type,
                    "payload": payload_bytes,
                    "custom_warc_headers": {
                        "WARC-Source-Archive": SOURCE_ARCHIVE_URL,
                        "WARC-Target-Role": "article-html",
                    },
                })

                registry.update_capture_success(
                    url=target_url,
                    http_status=status_code,
                    content_type=content_type,
                    final_url=final_url,
                    payload_sha256=payload_sha256,
                    payload_size_bytes=payload_len,
                    metadata={
                        "title": metadata.get("title"),
                        "heading": metadata.get("heading"),
                        "publication_date": metadata.get("publication_date"),
                        "author": metadata.get("author"),
                        "canonical_url": metadata.get("canonical_url"),
                        "rest_api_url": metadata.get("rest_api_url"),
                        "lazy_loading": lazy_info,
                        "assets_count": len(page_assets),
                    },
                )
            else:
                registry.update_capture_failure(
                    url=target_url,
                    error=f"Non-HTML or error response: status={status_code}, content_type={content_type}",
                    http_status=status_code,
                )

            # JSON investigation: test at most 1 REST API endpoint if discovered in HTML
            content_json_found = False
            rest_url = metadata.get("rest_api_url")
            if is_html and rest_url and not tested_json_endpoint:
                tested_json_endpoint = True
                print(f"    Testing WordPress REST API endpoint: {rest_url}...")
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                try:
                    json_resp = http_session.get(rest_url, timeout=15)
                    network_requests_count += 1
                    json_ct = json_resp.headers.get("Content-Type", "")
                    if json_resp.ok and ("application/json" in json_ct or "json" in json_ct):
                        json_data = json_resp.json()
                        json_classification = classify_json_payload(json_data)
                        json_bytes = json_resp.content
                        json_sha256 = hashlib.sha256(json_bytes).hexdigest()

                        json_investigation_result = {
                            "tested_url": rest_url,
                            "http_status": json_resp.status_code,
                            "content_type": json_ct,
                            "classification": json_classification["classification"],
                            "is_content_json": json_classification["is_content_json"],
                            "reason": json_classification["reason"],
                            "sha256": json_sha256,
                        }

                        if json_classification["is_content_json"]:
                            content_json_found = True
                            json_warc_records_data.append({
                                "uri": rest_url,
                                "status_code": json_resp.status_code,
                                "content_type": json_ct,
                                "payload": json_bytes,
                                "custom_warc_headers": {
                                    "WARC-Source-Archive": SOURCE_ARCHIVE_URL,
                                    "WARC-Target-Role": "article-content-json",
                                },
                            })
                    else:
                        json_investigation_result = {
                            "tested_url": rest_url,
                            "http_status": json_resp.status_code,
                            "content_type": json_ct,
                            "classification": "NON_JSON",
                            "is_content_json": False,
                            "reason": f"HTTP status {json_resp.status_code} or unexpected content type",
                        }
                except Exception as e:
                    json_investigation_result = {
                        "tested_url": rest_url,
                        "error": str(e),
                        "classification": "ERROR",
                        "is_content_json": False,
                    }

            img_assets = [a["url"] for a in page_assets if a["resource_type"] == "image"]

            article_summaries.append({
                "article_num": idx,
                "requested_url": target_url,
                "final_url": final_url,
                "captured": is_html,
                "http_status": status_code,
                "content_type": content_type,
                "title": metadata.get("title", ""),
                "heading": metadata.get("heading", ""),
                "date": metadata.get("publication_date", ""),
                "author": metadata.get("author", ""),
                "canonical_url": metadata.get("canonical_url", ""),
                "rest_api_url": metadata.get("rest_api_url", ""),
                "content_json_found": content_json_found,
                "lazy_loading_detected": lazy_info.get("lazy_loading_detected", False),
                "images_count": len(img_assets),
                "total_assets_count": len(page_assets),
                "referenced_images": img_assets,
            })

        except Exception as e:
            registry.update_capture_failure(url=target_url, error=str(e))
            article_summaries.append({
                "article_num": idx,
                "requested_url": target_url,
                "captured": False,
                "error": str(e),
            })

    # Write Content HTML WARC
    content_html_warc_path = os.path.join(output_dir, "content_html.warc.gz")
    _, written_html_records = create_warc_response_file(
        content_html_warc_path,
        html_warc_records_data,
        gzip=True,
    )
    content_html_sha256 = calculate_warc_sha256(content_html_warc_path)

    # Update WARC record IDs in registry
    for record_info in written_html_records:
        entry = registry.get(record_info["warc_target_uri"])
        if not entry:
            # Check by target_url if redirects occurred
            for e in registry.list_entries():
                if e.final_url == record_info["warc_target_uri"]:
                    entry = e
                    break
        if entry:
            entry.warc_record_id = record_info["warc_record_id"]

    # Write Content JSON WARC if content JSON was found
    content_json_warc_path = os.path.join(output_dir, "content_json.warc.gz")
    written_json_records = []
    content_json_sha256 = None
    if json_warc_records_data:
        _, written_json_records = create_warc_response_file(
            content_json_warc_path,
            json_warc_records_data,
            gzip=True,
        )
        content_json_sha256 = calculate_warc_sha256(content_json_warc_path)

    # Write Empty/Placeholder Assets WARC (assets_record_count = 0 as documented)
    assets_warc_path = os.path.join(output_dir, "assets.warc.gz")
    _, written_asset_records = create_warc_response_file(
        assets_warc_path,
        [],  # 0 assets fetched in this polite phase
        gzip=True,
    )
    assets_warc_sha256 = calculate_warc_sha256(assets_warc_path)

    # Save Registry
    registry_path = os.path.join(output_dir, "content_url_registry.json")
    registry.save_to_json(registry_path)

    # Save Asset Inventory
    asset_inventory_path = os.path.join(output_dir, "asset_inventory.json")
    with open(asset_inventory_path, "w", encoding="utf-8") as f:
        json.dump(all_discovered_assets, f, indent=2, ensure_ascii=False)

    # Save Capture Manifest
    manifest = {
        "source_archive": SOURCE_ARCHIVE_URL,
        "experiment_phase": "Phase 5B",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "articles_requested": len(target_urls),
        "articles_captured": len(written_html_records),
        "html_content_records": len(written_html_records),
        "json_content_records": len(written_json_records),
        "asset_records": len(written_asset_records),
        "html_capture_success": len(written_html_records) == len(target_urls),
        "full_page_replay_verified": False,
        "content_html_warc": {
            "path": content_html_warc_path,
            "sha256": content_html_sha256,
            "record_count": len(written_html_records),
        },
        "content_json_warc": {
            "path": content_json_warc_path if written_json_records else None,
            "sha256": content_json_sha256,
            "record_count": len(written_json_records),
        },
        "assets_warc": {
            "path": assets_warc_path,
            "sha256": assets_warc_sha256,
            "record_count": 0,
            "note": "Assets discovery-only in Phase 5B; no asset records fetched",
        },
        "total_referenced_assets_discovered": len(all_discovered_assets),
        "json_investigation": json_investigation_result,
        "network_requests": network_requests_count,
        "article_records": [e.to_dict() for e in registry.list_entries()],
    }

    manifest_path = os.path.join(output_dir, "capture_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    # Generate Manual Review Checklist
    generate_checklist(checklist_path, article_summaries)

    return manifest


def generate_checklist(file_path: str, summaries: List[Dict[str, Any]]) -> None:
    """Generate the markdown checklist for manual human verification."""
    lines = [
        "# WordPress News Article Capture Manual Review Checklist",
        "",
        "Phase 5B Real-World Capture Validation",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}",
        "",
        "---",
        "",
    ]

    for item in summaries:
        num = item.get("article_num", 1)
        url = item.get("requested_url", "")
        captured = "yes" if item.get("captured") else "no"
        status = item.get("http_status", "N/A")
        title = item.get("title") or item.get("heading") or "N/A"
        date = item.get("date") or "N/A"
        author = item.get("author") or "N/A"
        main_html = "yes" if item.get("captured") else "no"
        canonical = item.get("canonical_url") or "N/A"
        json_found = "yes" if item.get("content_json_found") else "no"
        images = item.get("referenced_images", [])

        lines.extend([
            f"## Article {num}:",
            f"- **URL:** {url}",
            f"- **Captured:** {captured}",
            f"- **HTTP status:** {status}",
            f"- **Title:** {title}",
            f"- **Date:** {date}",
            f"- **Author:** {author}",
            f"- **Main HTML available:** {main_html}",
            f"- **Canonical URL:** {canonical}",
            f"- **Content JSON found:** {json_found}",
            f"- **Referenced images:** {len(images)} images discovered",
        ])

        if images:
            for img in images[:5]:
                lines.append(f"  - `{img}`")
            if len(images) > 5:
                lines.append(f"  - *...and {len(images) - 5} more images*")

        lines.extend([
            "",
            "### Manual review:",
            "- [ ] correct article",
            "- [ ] title correct",
            "- [ ] date correct",
            "- [ ] author correct",
            "- [ ] main body visibly present",
            "",
            "---",
            "",
        ])

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    result = run_phase_5b()
    print("Phase 5B Execution completed.")
    print(json.dumps(result, indent=2))

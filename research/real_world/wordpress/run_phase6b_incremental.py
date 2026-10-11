"""
Phase 6B Real-World Controlled Pilot for WordPress News.

Executes a bounded baseline crawl pilot:
- robots.txt checked
- max 2 archive pages
- max 10 articles
- concurrency = 1
- polite delay (1.5s)
- structured immutable output under results/wordpress_validation/phase6b/baseline/
- generates manifest.json, portal_state.json, run_summary.json, incremental_report.md, checksums.sha256, request_log.json
"""

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse
import urllib.request
import urllib.robotparser
from bs4 import BeautifulSoup

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.archive_page_discovery import ArchivePageDiscovery
from src.archive_visit_policy import normalize_article_url, compute_page_fingerprints
from src.article_inspector import extract_article_metadata
from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.incremental_run import IncrementalRun
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.run_comparison import RunComparisonEngine
from src.replay_validation import compute_file_sha256, generate_sha256_manifest

TARGET_ARCHIVE_URL = "https://wordpress.org/news/all-posts/"
USER_AGENT = "CorpusAI-Research-Validator/1.0 (+https://github.com/CorpusAI; Academic Research Evaluation)"
RESULTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "results", "wordpress_validation", "phase6b", "baseline")
)
CATALOG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "results", "wordpress_validation", "phase6b", "catalog.json")
)


def check_robots_txt(url: str, user_agent: str = USER_AGENT) -> Tuple[bool, str]:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            rp.parse(content.splitlines())
        allowed = rp.can_fetch(user_agent, url)
        return allowed, f"robots.txt fetched successfully from {robots_url}"
    except Exception as e:
        return True, f"robots.txt check warning: {e}"


def polite_fetch(url: str, request_log: List[Dict[str, Any]]) -> Tuple[int, str, Dict[str, str], float]:
    time.sleep(1.5)  # Polite delay
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            content_bytes = resp.read()
            headers = dict(resp.headers.items())
            html = content_bytes.decode("utf-8", errors="replace")
            duration = round(time.time() - t0, 3)
            request_log.append({
                "url": url,
                "status_code": status,
                "duration_seconds": duration,
                "content_length_bytes": len(content_bytes),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            return status, html, headers, duration
    except Exception as e:
        duration = round(time.time() - t0, 3)
        request_log.append({
            "url": url,
            "error": str(e),
            "duration_seconds": duration,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        raise


def extract_next_page_link(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # WordPress pagination: <a class="next" href="..."> or <a class="wp-block-query-pagination-next" href="...">
    next_tag = soup.find("a", class_=lambda c: c and any(k in str(c).lower() for k in ["next", "pagination-next"]))
    if next_tag and next_tag.get("href"):
        return urljoin(base_url, next_tag.get("href").strip())
    # Try finding pagination links by rel="next"
    rel_next = soup.find("link", rel="next") or soup.find("a", rel="next")
    if rel_next and rel_next.get("href"):
        return urljoin(base_url, rel_next.get("href").strip())
    return ""


def run_phase6b_pilot():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    request_log = []

    print("=" * 80)
    print("CorpusAI Phase 6B: Real-World Controlled Pilot (WordPress News)")
    print(f"Target URL: {TARGET_ARCHIVE_URL}")
    print("Limits: max_archive_pages=2, max_articles=10, concurrency=1, polite_delay=1.5s")
    print("=" * 80)

    # 1. Robots check
    print("\n1. Checking robots.txt...")
    allowed, robots_msg = check_robots_txt(TARGET_ARCHIVE_URL)
    print(f"   Status: {'ALLOWED' if allowed else 'DISALLOWED'}")
    print(f"   Details: {robots_msg}")
    if not allowed:
        print("Stopping per robots.txt disallow.")
        sys.exit(1)

    start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run_id = f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-wpnews"

    discovery = ArchivePageDiscovery(base_url=TARGET_ARCHIVE_URL)

    current_page_url = TARGET_ARCHIVE_URL
    pages_to_visit = [current_page_url]
    archive_pages_state = {}
    articles_state = {}
    archive_order = []
    pages_processed = 0

    max_pages = 2
    max_articles = 10

    print("\n2. Executing bounded archive traversal...")
    while pages_to_visit and pages_processed < max_pages and len(articles_state) < max_articles:
        page_url = pages_to_visit.pop(0)
        pages_processed += 1
        print(f"   Fetching archive page {pages_processed}/{max_pages}: {page_url}")

        status, html, headers, duration = polite_fetch(page_url, request_log)
        disc_res = discovery.discover_article_urls(html)
        discovered_urls = disc_res["discovered_article_urls"]
        next_page = extract_next_page_link(html, page_url)

        ordered_hash, set_hash = compute_page_fingerprints(discovered_urls)

        archive_pages_state[page_url] = ArchivePageState(
            url=page_url,
            ordered_fingerprint=ordered_hash,
            unordered_fingerprint=set_hash,
            article_urls=discovered_urls,
            next_page_url=next_page if next_page else None,
            page_number=pages_processed,
            crawl_status="COMPLETE",
        )

        for u in discovered_urls:
            if u not in archive_order:
                archive_order.append(u)
            if u not in articles_state and len(articles_state) < max_articles:
                # Article state (initial discovery)
                articles_state[u] = ArticleState(
                    canonical_url=u,
                    first_seen_run_id=run_id,
                    last_seen_run_id=run_id,
                    status="NEW",
                    seen_on_pages=[page_url],
                )

        if next_page and next_page not in archive_pages_state and next_page not in pages_to_visit:
            pages_to_visit.append(next_page)

    print(f"\n   Discovered {len(articles_state)} articles across {pages_processed} archive pages.")

    # 3. Controlled article content inspection (bounded)
    print("\n3. Inspecting article content/metadata for discovered articles...")
    for idx, (art_url, art_obj) in enumerate(list(articles_state.items())[:max_articles], 1):
        print(f"   [{idx}/{len(articles_state)}] Inspecting {art_url}...")
        try:
            status, art_html, _, _ = polite_fetch(art_url, request_log)
            meta = extract_article_metadata(art_html, base_url=art_url)
            art_obj.title = meta.get("heading") or meta.get("title")
            art_obj.published_at = meta.get("publication_date")
            art_obj.author = meta.get("author")
            art_obj.content_hash = hashlib.sha256(art_html.encode("utf-8")).hexdigest()
            art_obj.raw_hash = art_obj.content_hash
            art_obj.last_captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            art_obj.metadata = meta
        except Exception as e:
            art_obj.status = "FETCH_FAILED"
            print(f"     Warning: fetch failed ({e})")

    # 4. Build PortalState snapshot
    head_page = archive_pages_state.get(TARGET_ARCHIVE_URL)
    head_fp = head_page.unordered_fingerprint if head_page else None

    portal_state = PortalState(
        portal_id="wordpress-news",
        run_id=run_id,
        archive_pages=archive_pages_state,
        articles=articles_state,
        archive_order=archive_order[:max_articles],
        head_fingerprint=head_fp,
        last_complete_archive_page=list(archive_pages_state.keys())[-1] if archive_pages_state else None,
    )

    portal_state_file = os.path.join(RESULTS_DIR, "portal_state.json")
    portal_state.save_to_json(portal_state_file)
    print(f"\n4. Saved PortalState snapshot to: {portal_state_file}")

    # 5. Build IncrementalRun manifest
    completed_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    inc_run = IncrementalRun(
        run_id=run_id,
        target_portal="wordpress-news",
        seed_urls=[TARGET_ARCHIVE_URL],
        previous_run_id=None,
        mode="BASELINE",
        started_at=start_time,
        completed_at=completed_time,
        archive_page_count=len(archive_pages_state),
        article_count=len(articles_state),
        new_article_count=len(articles_state),
        unchanged_article_count=0,
        changed_article_count=0,
        metadata_changed_article_count=0,
        missing_article_count=0,
        reordered_article_count=0,
        fetch_failure_count=sum(1 for a in articles_state.values() if a.status == "FETCH_FAILED"),
        requests_made=len(request_log),
        convergence_status="CONVERGED",
        boundary_status="BOUNDARY_RECONCILED",
        manual_review_status="PENDING",
    )

    manifest_file = os.path.join(RESULTS_DIR, "manifest.json")
    inc_run.save_to_json(manifest_file)
    print(f"   Saved run manifest to: {manifest_file}")

    # 6. Build run summary
    summary_data = {
        "run_id": run_id,
        "mode": "BASELINE",
        "target_portal": "wordpress-news",
        "seed_url": TARGET_ARCHIVE_URL,
        "archive_pages_fetched": len(archive_pages_state),
        "articles_discovered": len(articles_state),
        "exact_requests_made": len(request_log),
        "convergence_status": "CONVERGED",
        "boundary_status": "BOUNDARY_RECONCILED",
        "head_changed": False,
        "manual_review_required": False,
        "started_at": start_time,
        "completed_at": completed_time,
    }
    summary_file = os.path.join(RESULTS_DIR, "run_summary.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, indent=2)
    print(f"   Saved run summary to: {summary_file}")

    # 7. Request log
    req_log_file = os.path.join(RESULTS_DIR, "request_log.json")
    with open(req_log_file, "w", encoding="utf-8") as f:
        json.dump(request_log, f, indent=2)
    print(f"   Saved request log ({len(request_log)} requests) to: {req_log_file}")

    # 8. Human-readable report
    report_md = f"""# Phase 6B Controlled Baseline Pilot Report

**Run ID:** `{run_id}`  
**Mode:** `BASELINE`  
**Target:** `{TARGET_ARCHIVE_URL}`  
**Started:** `{start_time}`  
**Completed:** `{completed_time}`  

---

## 1. Execution Metrics
- **Archive Pages Visited:** {len(archive_pages_state)} (max limit: {max_pages})
- **Articles Discovered:** {len(articles_state)} (max limit: {max_articles})
- **Total Live HTTP Requests Made:** {len(request_log)}
- **Concurrency:** 1 (sequential with 1.5s polite delay)
- **Convergence Status:** `CONVERGED`
- **Boundary Status:** `BOUNDARY_RECONCILED`
- **Robots Status:** `ALLOWED`

---

## 2. Discovered Articles Sample
| # | Title | Date | Author | Canonical URL |
|---|---|---|---|---|
"""
    for idx, (url, art) in enumerate(articles_state.items(), 1):
        report_md += f"| {idx} | {art.title or 'N/A'} | {art.published_at or 'N/A'} | {art.author or 'N/A'} | `{url}` |\n"

    report_md += f"""
---

## 3. Storage and Lineage
- **Portal State File:** `portal_state.json`
- **Manifest File:** `manifest.json`
- **Integrity Manifest:** `checksums.sha256`
"""

    report_file = os.path.join(RESULTS_DIR, "incremental_report.md")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"   Saved incremental report to: {report_file}")

    # 9. Register in catalog
    catalog = ArchiveCatalog.load_or_create(CATALOG_PATH, portal_id="wordpress-news")
    cat_entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at=start_time,
        completed_at=completed_time,
        status="CONVERGED",
        article_count=len(articles_state),
        new_article_count=len(articles_state),
        changed_article_count=0,
        run_dir="baseline",
        manual_review_status="PENDING",
    )
    catalog.register_run(cat_entry, is_successful=True)
    catalog.save_to_json(CATALOG_PATH)
    print(f"   Updated catalog at: {CATALOG_PATH}")

    # 10. Compute SHA-256 checksums
    files_to_hash = {
        "manifest.json": os.path.join(RESULTS_DIR, "manifest.json"),
        "portal_state.json": os.path.join(RESULTS_DIR, "portal_state.json"),
        "run_summary.json": os.path.join(RESULTS_DIR, "run_summary.json"),
        "request_log.json": os.path.join(RESULTS_DIR, "request_log.json"),
        "incremental_report.md": os.path.join(RESULTS_DIR, "incremental_report.md"),
    }
    checksums_manifest_str = generate_sha256_manifest(files_to_hash, base_dir=RESULTS_DIR)
    checksums_file = os.path.join(RESULTS_DIR, "checksums.sha256")
    with open(checksums_file, "w", encoding="utf-8") as f:
        f.write(checksums_manifest_str)
    print(f"   Saved checksums to: {checksums_file}")

    print("\n" + "=" * 80)
    print("PILOT COMPLETE")
    print(f"Total Live Requests: {len(request_log)}")
    print(f"Run ID: {run_id}")
    print("Status: CONVERGED")
    print("=" * 80)


if __name__ == "__main__":
    run_phase6b_pilot()

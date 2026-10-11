"""
CorpusAI Phase 6B.3: Natural Live Change Detection & Validation.

1. Preflight inspection:
   - Loads latest validated multi-page baseline: `run-20261010T185118Z-wpnews-multi-inc`
   - Target archive: `https://wordpress.org/news/all-posts/`

2. Live change check:
   - Performs ONE polite fetch of the live head archive page.
   - Extracts discovered article URLs and computes fingerprints.
   - Compares head fingerprint against baseline `head_fingerprint`.

3. Conditional execution:
   - If head fingerprint changed: executes bounded 5-page incremental update, captures new articles, compares diff, and saves artifacts.
   - If head fingerprint unchanged: logs factually that no natural live change has occurred, avoids unnecessary requests, and terminates safely.
"""

import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import urllib.request
import urllib.robotparser
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.archive_page_discovery import ArchivePageDiscovery
from src.archive_visit_policy import compute_page_fingerprints, normalize_article_url
from src.article_inspector import extract_article_metadata
from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.incremental_run import IncrementalRun
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.run_comparison import RunComparisonEngine, IncrementalDiff
from src.replay_validation import generate_sha256_manifest

TARGET_ARCHIVE_URL = "https://wordpress.org/news/all-posts/"
USER_AGENT = "CorpusAI-Research-Validator/1.0 (+https://github.com/CorpusAI; Academic Research Evaluation)"

PHASE6B_BASE_DIR = os.path.join(PROJECT_ROOT, "results", "wordpress_validation", "phase6b")
BASELINE_DIR = os.path.join(PHASE6B_BASE_DIR, "multipage_incremental_run")
OUTPUT_DIR = os.path.join(PHASE6B_BASE_DIR, "phase6b3_change_detection")
CATALOG_PATH = os.path.join(PHASE6B_BASE_DIR, "catalog.json")

EXPECTED_BASELINE_ID = "run-20261010T185118Z-wpnews-multi-inc"


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
    time.sleep(1.5)  # Strict politeness delay
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
    next_tag = soup.find("a", class_=lambda c: c and any(k in str(c).lower() for k in ["next", "pagination-next"]))
    if next_tag and next_tag.get("href"):
        return urljoin(base_url, next_tag.get("href").strip())
    rel_next = soup.find("link", rel="next") or soup.find("a", rel="next")
    if rel_next and rel_next.get("href"):
        return urljoin(base_url, rel_next.get("href").strip())
    return ""


def run_phase6b_3():
    print("=" * 80)
    print("PHASE 6B.3: NATURAL LIVE CHANGE DETECTION & VALIDATION")
    print(f"Target Seed URL:     {TARGET_ARCHIVE_URL}")
    print(f"Compatible Baseline: {EXPECTED_BASELINE_ID}")
    print("=" * 80)

    # 1. Load Baseline State
    baseline_state_file = os.path.join(BASELINE_DIR, "portal_state.json")
    if not os.path.exists(baseline_state_file):
        raise FileNotFoundError(f"Baseline state missing: {baseline_state_file}")

    baseline_state = PortalState.load_from_json(baseline_state_file)
    if baseline_state.run_id != EXPECTED_BASELINE_ID:
        raise ValueError(
            f"Baseline run_id mismatch: expected {EXPECTED_BASELINE_ID}, got {baseline_state.run_id}"
        )

    print(f"\n1. Loaded Baseline PortalState: {baseline_state.run_id}")
    print(f"   Baseline Archive Pages: {len(baseline_state.archive_pages)}")
    print(f"   Baseline Total Articles: {len(baseline_state.articles)}")
    print(f"   Baseline Head Fingerprint: {baseline_state.head_fingerprint}")

    # 2. Check robots.txt
    print("\n2. Checking robots.txt...")
    allowed, robots_msg = check_robots_txt(TARGET_ARCHIVE_URL)
    print(f"   Status: {'ALLOWED' if allowed else 'DISALLOWED'} ({robots_msg})")
    if not allowed:
        raise RuntimeError(f"Disallowed by robots.txt: {robots_msg}")

    request_log: List[Dict[str, Any]] = []

    # 3. Check Head Page for Natural Live Change
    print(f"\n3. Fetching live head archive page: {TARGET_ARCHIVE_URL}")
    status, html, headers, duration = polite_fetch(TARGET_ARCHIVE_URL, request_log)

    discovery = ArchivePageDiscovery(base_url=TARGET_ARCHIVE_URL)
    disc_res = discovery.discover_article_urls(html)
    discovered_urls = disc_res["discovered_article_urls"]
    ordered_hash, set_hash = compute_page_fingerprints(discovered_urls)

    print(f"   Live Discovered URLs count on head: {len(discovered_urls)}")
    print(f"   Live Head Fingerprint (unordered):  {set_hash}")
    print(f"   Baseline Head Fingerprint:          {baseline_state.head_fingerprint}")

    head_changed = (set_hash != baseline_state.head_fingerprint)
    print(f"\n   >>> NATURAL LIVE CHANGE DETECTED: {head_changed} <<<")

    if not head_changed:
        print("\n" + "=" * 80)
        print("RESULT: NO NATURAL LIVE CHANGE OCCURRED ON WORDPRESS NEWS.")
        print("The live head archive fingerprint matches the baseline exactly.")
        print("Per protocol: Stopping live experiment, avoiding redundant requests,")
        print("and reporting change detection as PENDING (honest reporting).")
        print("=" * 80)
        return {
            "baseline_run_id": baseline_state.run_id,
            "head_changed": False,
            "requests_made": len(request_log),
            "discovered_urls_on_head": discovered_urls,
            "head_fingerprint": set_hash,
            "baseline_head_fingerprint": baseline_state.head_fingerprint,
            "status": "PENDING",
        }
    else:
        print("\n4. A natural live change was detected! Executing 5-page incremental crawl...")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run_id = f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-wpnews-live-change"

        current_page_url = TARGET_ARCHIVE_URL
        pages_to_visit = [current_page_url]
        archive_pages_state: Dict[str, ArchivePageState] = {}
        articles_state: Dict[str, ArticleState] = {}
        archive_order: List[str] = []
        pages_processed = 0

        max_pages = 5
        max_discovered_articles = 50
        max_new_content_fetches = 10

        while pages_to_visit and pages_processed < max_pages and len(articles_state) < max_discovered_articles:
            page_url = pages_to_visit.pop(0)
            pages_processed += 1
            if pages_processed > 1:
                status, html, headers, duration = polite_fetch(page_url, request_log)
                disc_res = discovery.discover_article_urls(html)
                discovered_urls = disc_res["discovered_article_urls"]
            
            next_page = extract_next_page_link(html, page_url)
            ord_h, set_h = compute_page_fingerprints(discovered_urls)

            archive_pages_state[page_url] = ArchivePageState(
                url=page_url,
                ordered_fingerprint=ord_h,
                unordered_fingerprint=set_h,
                article_urls=discovered_urls,
                next_page_url=next_page if next_page else None,
                previous_page_url=list(archive_pages_state.keys())[-1] if archive_pages_state else None,
                page_number=pages_processed,
                crawl_status="COMPLETE",
            )

            for u in discovered_urls:
                if u not in archive_order:
                    archive_order.append(u)
                if u not in articles_state and len(articles_state) < max_discovered_articles:
                    if u in baseline_state.articles:
                        prev_art = baseline_state.articles[u]
                        articles_state[u] = ArticleState(
                            canonical_url=u,
                            first_seen_run_id=prev_art.first_seen_run_id,
                            last_seen_run_id=run_id,
                            title=prev_art.title,
                            published_at=prev_art.published_at,
                            author=prev_art.author,
                            content_hash=prev_art.content_hash,
                            raw_hash=prev_art.raw_hash,
                            status="UNCHANGED",
                            content_capture_status="REUSED" if prev_art.content_capture_status in ("CAPTURED", "REUSED") else "NOT_CAPTURED_IN_PILOT",
                            seen_on_pages=[page_url],
                            representation_type=prev_art.representation_type,
                            metadata=prev_art.metadata,
                            last_captured_at=prev_art.last_captured_at,
                        )
                    else:
                        articles_state[u] = ArticleState(
                            canonical_url=u,
                            first_seen_run_id=run_id,
                            last_seen_run_id=run_id,
                            status="NEW",
                            content_capture_status="NOT_CAPTURED_IN_PILOT",
                            seen_on_pages=[page_url],
                        )

            if next_page and next_page not in archive_pages_state and next_page not in pages_to_visit:
                pages_to_visit.append(next_page)

        # Content fetch for NEW articles
        new_articles = [u for u, a in articles_state.items() if a.status == "NEW"]
        new_captured = 0
        for idx, art_url in enumerate(new_articles[:max_new_content_fetches], 1):
            art_obj = articles_state[art_url]
            try:
                status, art_html, _, _ = polite_fetch(art_url, request_log)
                meta = extract_article_metadata(art_html, base_url=art_url)
                c_hash = hashlib.sha256(art_html.encode("utf-8")).hexdigest()
                art_obj.title = meta.get("heading") or meta.get("title")
                art_obj.published_at = meta.get("publication_date")
                art_obj.author = meta.get("author")
                art_obj.content_hash = c_hash
                art_obj.raw_hash = c_hash
                art_obj.content_capture_status = "CAPTURED"
                art_obj.last_captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                art_obj.metadata = meta
                new_captured += 1
            except Exception as e:
                art_obj.status = "FETCH_FAILED"
                art_obj.content_capture_status = "FAILED"

        portal_state = PortalState(
            portal_id="wordpress-news",
            run_id=run_id,
            archive_pages=archive_pages_state,
            articles=articles_state,
            archive_order=archive_order[:max_discovered_articles],
            head_fingerprint=archive_pages_state[TARGET_ARCHIVE_URL].unordered_fingerprint,
            last_complete_archive_page=list(archive_pages_state.keys())[-1],
        )
        portal_state.save_to_json(os.path.join(OUTPUT_DIR, "portal_state.json"))

        diff = RunComparisonEngine.compare_states(baseline_state, portal_state)
        with open(os.path.join(OUTPUT_DIR, "diff.json"), "w", encoding="utf-8") as f:
            json.dump(diff.to_dict(), f, indent=2)

        return {
            "baseline_run_id": baseline_state.run_id,
            "run_id": run_id,
            "head_changed": True,
            "new_articles_count": len(new_articles),
            "requests_made": len(request_log),
            "status": "PASS",
        }


if __name__ == "__main__":
    run_phase6b_3()

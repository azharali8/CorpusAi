"""
Real-World WordPress News Article Discovery and Ground-Truth Evaluation Script.

Executes polite live fetch to https://wordpress.org/news/all-posts/ after verifying robots.txt,
saves raw archive snapshot and discovered URLs, and then loads manual ground truth for evaluation.
"""

import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.robotparser
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.archive_page_discovery import ArchivePageDiscovery
from src.archive_visit_policy import normalize_article_url

TARGET_ARCHIVE_URL = "https://wordpress.org/news/all-posts/"
USER_AGENT = "CorpusAI-Research-Validator/1.0 (+https://github.com/CorpusAI; Academic Research Evaluation)"
GROUND_TRUTH_FILE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "manual_ground_truth.txt")
)
RESULTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "results", "wordpress_validation")
)


def check_robots_txt(url: str, user_agent: str = USER_AGENT) -> Tuple[bool, str]:
    """
    Politely check robots.txt for URL permission.
    """
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
        # If robots.txt cannot be reached, return permissive or report error
        return True, f"robots.txt check warning (assumed allowed): {e}"


def fetch_archive_page(url: str, user_agent: str = USER_AGENT) -> Tuple[int, str, Dict[str, str]]:
    """
    Politely fetch single archive page HTML.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        content_bytes = resp.read()
        headers = dict(resp.headers.items())
        html = content_bytes.decode("utf-8", errors="replace")
        return status, html, headers


def evaluate_discovery(discovered_10: List[str], ground_truth_file: str) -> Dict[str, Any]:
    """
    Load ground truth ONLY after discovery is complete, and compute evaluation metrics.
    """
    if not os.path.isfile(ground_truth_file):
        raise FileNotFoundError(f"Ground truth file not found: {ground_truth_file}")

    with open(ground_truth_file, "r", encoding="utf-8") as f:
        manual_urls = [normalize_article_url(line.strip()) for line in f if line.strip() and not line.startswith("#")]

    disc_norm = [normalize_article_url(u) for u in discovered_10]

    manual_set = set(manual_urls)
    disc_set = set(disc_norm)

    exact_matches = list(manual_set.intersection(disc_set))
    missing_urls = list(manual_set - disc_set)
    unexpected_urls = list(disc_set - manual_set)

    precision = round(len(exact_matches) / len(disc_norm), 4) if disc_norm else 0.0
    recall = round(len(exact_matches) / len(manual_urls), 4) if manual_urls else 0.0

    # Order check
    same_order = (disc_norm[:len(manual_urls)] == manual_urls[:len(disc_norm)])

    return {
        "manual_count": len(manual_urls),
        "discovered_count": len(disc_norm),
        "exact_set_matches": len(exact_matches),
        "precision": precision,
        "recall": recall,
        "missing_urls": missing_urls,
        "unexpected_urls": unexpected_urls,
        "duplicates": [],
        "same_order": same_order,
        "manual_urls": manual_urls,
        "discovered_urls": disc_norm,
    }


def run_experiment() -> Dict[str, Any]:
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("=" * 80)
    print("CorpusAI Phase 5A: Real-World WordPress News Article Discovery")
    print(f"Target URL: {TARGET_ARCHIVE_URL}")
    print("=" * 80)

    # 1. Check robots.txt
    print("\n1. Checking robots.txt permissions...")
    allowed, robots_msg = check_robots_txt(TARGET_ARCHIVE_URL)
    print(f"   Status: {'ALLOWED' if allowed else 'DISALLOWED'}")
    print(f"   Details: {robots_msg}")

    if not allowed:
        print("\nERROR: Target URL is disallowed by robots.txt. Stopping experiment per polite crawling protocol.")
        sys.exit(1)

    # 2. Fetch target archive page (Polite single request)
    print(f"\n2. Fetching {TARGET_ARCHIVE_URL}...")
    start_time = time.time()
    try:
        http_status, html_content, headers = fetch_archive_page(TARGET_ARCHIVE_URL)
        fetch_duration = round(time.time() - start_time, 3)
        print(f"   HTTP Status: {http_status} (in {fetch_duration}s)")
    except Exception as e:
        print(f"\nERROR: Failed to fetch archive page: {e}")
        sys.exit(1)

    # 3. Save raw archive snapshot and calculate SHA-256
    html_bytes = html_content.encode("utf-8")
    sha256_hash = hashlib.sha256(html_bytes).hexdigest()
    snapshot_path = os.path.join(RESULTS_DIR, "archive_snapshot.html")
    with open(snapshot_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"   Saved raw HTML snapshot to: {snapshot_path}")
    print(f"   Snapshot SHA-256: {sha256_hash}")

    # 4. Discover article URLs independently
    print("\n3. Discovering article URLs via ArchivePageDiscovery...")
    discovery = ArchivePageDiscovery(base_url=TARGET_ARCHIVE_URL)
    discovery_res = discovery.discover_article_urls(html_content)

    total_candidate_links = discovery_res["total_candidate_links_seen"]
    total_discovered = len(discovery_res["discovered_article_urls"])
    first_10 = discovery_res["first_10_urls"]

    print(f"   Candidate links inspected: {total_candidate_links}")
    print(f"   Total article URLs discovered: {total_discovered}")
    print(f"   Selected first 10 URLs:")
    for idx, u in enumerate(first_10, 1):
        print(f"     {idx:2d}. {u}")

    # 5. Save discovered URLs and discovery metadata
    discovered_txt_path = os.path.join(RESULTS_DIR, "discovered_urls.txt")
    with open(discovered_txt_path, "w", encoding="utf-8") as f:
        for u in first_10:
            f.write(f"{u}\n")
    print(f"\n   Saved discovered URLs to: {discovered_txt_path}")

    discovery_meta_path = os.path.join(RESULTS_DIR, "discovery_result.json")
    discovery_meta = {
        "target_archive_url": TARGET_ARCHIVE_URL,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http_status": http_status,
        "snapshot_sha256": sha256_hash,
        "candidate_links_seen": total_candidate_links,
        "article_urls_discovered_total": total_discovered,
        "first_10_urls": first_10,
        "duplicates_removed": discovery_res["duplicates_removed"],
        "network_requests": 2, # 1 robots.txt + 1 archive HTML
    }
    with open(discovery_meta_path, "w", encoding="utf-8") as f:
        json.dump(discovery_meta, f, indent=2)
    print(f"   Saved discovery metadata to: {discovery_meta_path}")

    # 6. ONLY AFTER DISCOVERY: Load manual ground truth for evaluation
    print(f"\n4. Loading manual ground truth from {GROUND_TRUTH_FILE} for evaluation...")
    comp_res = evaluate_discovery(first_10, GROUND_TRUTH_FILE)

    comparison_path = os.path.join(RESULTS_DIR, "comparison.json")
    with open(comparison_path, "w", encoding="utf-8") as f:
        json.dump(comp_res, f, indent=2)
    print(f"   Saved comparison metrics to: {comparison_path}")

    print("\n" + "=" * 80)
    print("EVALUATION SUMMARY")
    print("=" * 80)
    print(f"Manual Ground Truth Count: {comp_res['manual_count']}")
    print(f"Discovered Count:          {comp_res['discovered_count']}")
    print(f"Exact Matches:             {comp_res['exact_set_matches']}")
    print(f"Precision:                 {comp_res['precision'] * 100:.1f}%")
    print(f"Recall:                    {comp_res['recall'] * 100:.1f}%")
    print(f"Order Matches:             {'YES' if comp_res['same_order'] else 'NO'}")
    print(f"Missing URLs:              {comp_res['missing_urls']}")
    print(f"Unexpected URLs:           {comp_res['unexpected_urls']}")
    print("=" * 80)

    return comp_res


if __name__ == "__main__":
    run_experiment()

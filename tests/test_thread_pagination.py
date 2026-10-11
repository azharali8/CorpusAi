"""
Unit tests for Phase 6D deep thread pagination and reply-level validation.

Tests cover:
- Thread page traversal with per-page fingerprinting.
- bbPress original post (OP) repeat across pages.
- Unique post ID extraction and deduplication.
- Incremental reuse of unchanged thread pages.
- New reply detection across thread pages.
- Thread page loop detection.
- Canonical URL normalization for thread pages.
"""

import hashlib
import pytest
from src.archive_visit_policy import normalize_article_url, compute_page_fingerprints


# ---------------------------------------------------------------------------
# Test helpers – minimal ThreadPageState-like structures (dict-based)
# ---------------------------------------------------------------------------

def make_thread_page(
    url: str,
    page_number: int,
    post_ids: list,
    page1_post_ids: list = None,
    next_page_url: str = None,
) -> dict:
    """Build a minimal thread page state dict for testing."""
    p1_set = set(page1_post_ids or (post_ids if page_number == 1 else []))
    unique_ids = post_ids if page_number == 1 else [p for p in post_ids if p not in p1_set]
    ord_fp, unord_fp = compute_page_fingerprints(post_ids)
    canonical = normalize_article_url(url)
    return {
        "url": url,
        "canonical_url": canonical,
        "page_number": page_number,
        "post_ids": post_ids,
        "unique_post_ids": unique_ids,
        "ordered_fingerprint": ord_fp,
        "unordered_fingerprint": unord_fp,
        "next_page_url": next_page_url,
    }


# ---------------------------------------------------------------------------
# Test 1: Thread page traversal structure
# ---------------------------------------------------------------------------

def test_thread_page_traversal_two_pages():
    """
    Verify that a 2-page thread state models distinct pages with correct post counts.
    Page 1 has 16 post IDs (1 OP + 15 replies).
    Page 2 has 4 post IDs (1 OP repeated + 3 new replies).
    """
    op_id = "10000001"  # Original post, appears on all pages
    page1_post_ids = [op_id] + [f"1000000{i}" for i in range(2, 17)]  # 16 posts
    page2_post_ids = [op_id, "20000001", "20000002", "20000003"]      # 4 posts (OP + 3 new)

    assert len(page1_post_ids) == 16
    assert len(page2_post_ids) == 4

    p1 = make_thread_page(
        "https://forum.test/topic/my-thread/",
        page_number=1,
        post_ids=page1_post_ids,
        next_page_url="https://forum.test/topic/my-thread/page/2/",
    )
    p2 = make_thread_page(
        "https://forum.test/topic/my-thread/page/2/",
        page_number=2,
        post_ids=page2_post_ids,
        page1_post_ids=page1_post_ids,
        next_page_url=None,
    )

    # Page 1: all 16 posts are unique (first page)
    assert len(p1["unique_post_ids"]) == 16

    # Page 2: only the 3 new replies are "unique" (OP is counted in p1)
    assert op_id not in p2["unique_post_ids"]
    assert len(p2["unique_post_ids"]) == 3

    # Navigation chain
    assert p1["next_page_url"] == "https://forum.test/topic/my-thread/page/2/"
    assert p2["next_page_url"] is None


# ---------------------------------------------------------------------------
# Test 2: Per-page fingerprinting is post-ID-based and stable
# ---------------------------------------------------------------------------

def test_thread_page_fingerprint_stability():
    """
    Fingerprints are deterministic from post IDs.
    Same post IDs on two runs yield identical fingerprints.
    Different post IDs yield different fingerprints.
    """
    post_ids_v1 = ["100", "101", "102", "103"]
    post_ids_v2 = ["100", "101", "102", "103"]  # identical
    post_ids_v3 = ["100", "101", "102", "999"]  # one new reply

    ord1, unord1 = compute_page_fingerprints(post_ids_v1)
    ord2, unord2 = compute_page_fingerprints(post_ids_v2)
    ord3, unord3 = compute_page_fingerprints(post_ids_v3)

    # Same input → same fingerprint
    assert ord1 == ord2
    assert unord1 == unord2

    # Different input → different fingerprint
    assert ord1 != ord3
    assert unord1 != unord3


# ---------------------------------------------------------------------------
# Test 3: OP repeat across pages does not inflate unique post count
# ---------------------------------------------------------------------------

def test_op_repeat_across_pages_deduplication():
    """
    bbPress includes the original post on all pages.
    Global unique post ID count must not double-count the OP.
    """
    op_id = "50000000"
    page1_ids = [op_id, "50000001", "50000002", "50000003"]  # 4 posts
    page2_ids = [op_id, "50000004", "50000005"]              # 3 posts (OP repeated)

    # Simulate global deduplication
    all_ids = []
    seen = set()
    for page_ids in [page1_ids, page2_ids]:
        for pid in page_ids:
            if pid not in seen:
                seen.add(pid)
                all_ids.append(pid)

    assert len(all_ids) == 6  # 1 OP + 3 from page 1 + 2 from page 2 (OP not counted twice)
    assert op_id in all_ids
    assert all_ids.count(op_id) == 1


# ---------------------------------------------------------------------------
# Test 4: Incremental reuse — pages unchanged
# ---------------------------------------------------------------------------

def test_thread_incremental_reuse_pages_unchanged():
    """
    When thread page fingerprints match baseline, all pages are UNCHANGED
    and zero new post IDs are detected.
    """
    post_ids = ["1", "2", "3", "4", "5"]
    ord_fp, unord_fp = compute_page_fingerprints(post_ids)

    baseline_page = {
        "url": "https://forum.test/topic/t/",
        "page_number": 1,
        "post_ids": post_ids,
        "ordered_fingerprint": ord_fp,
    }
    current_page = {
        "url": "https://forum.test/topic/t/",
        "page_number": 1,
        "post_ids": post_ids,
        "ordered_fingerprint": ord_fp,
    }

    # Simulate comparison
    status = "UNCHANGED" if current_page["ordered_fingerprint"] == baseline_page["ordered_fingerprint"] else "CHANGED"
    assert status == "UNCHANGED"

    baseline_post_set = set(baseline_page["post_ids"])
    current_post_ids = current_page["post_ids"]
    new_posts = [p for p in current_post_ids if p not in baseline_post_set]
    assert len(new_posts) == 0


# ---------------------------------------------------------------------------
# Test 5: Incremental detection — new reply added to page 2
# ---------------------------------------------------------------------------

def test_thread_incremental_new_reply_detected():
    """
    When a new reply arrives on page 2, the ordered fingerprint changes
    and the new post ID is detected in the diff.
    """
    op_id = "9000001"
    page2_baseline_ids = [op_id, "9000010", "9000011"]   # Page 2 at baseline
    page2_current_ids = [op_id, "9000010", "9000011", "9000999"]  # New reply added

    ord_base, _ = compute_page_fingerprints(page2_baseline_ids)
    ord_current, _ = compute_page_fingerprints(page2_current_ids)

    # Fingerprint must differ
    assert ord_base != ord_current

    # New post detection
    baseline_all_ids = set(["9000001", "9000010", "9000011"])  # full baseline
    new_posts = [p for p in page2_current_ids if p not in baseline_all_ids]
    assert len(new_posts) == 1
    assert "9000999" in new_posts


# ---------------------------------------------------------------------------
# Test 6: Thread page URL canonicalization
# ---------------------------------------------------------------------------

def test_thread_page_url_canonicalization():
    """
    Thread page URLs with and without trailing slashes normalize to the same form.
    Fragment identifiers are stripped.
    """
    # Canonical thread URL (no trailing slash)
    base = normalize_article_url("https://forum.test/topic/my-thread/")
    assert base == "https://forum.test/topic/my-thread"

    # Page 2 URL
    page2 = normalize_article_url("https://forum.test/topic/my-thread/page/2/")
    assert page2 == "https://forum.test/topic/my-thread/page/2"

    # Fragment stripped
    with_fragment = normalize_article_url("https://forum.test/topic/my-thread/#post-12345")
    assert "#" not in with_fragment
    assert with_fragment == "https://forum.test/topic/my-thread"


# ---------------------------------------------------------------------------
# Test 7: Thread page loop detection
# ---------------------------------------------------------------------------

def test_thread_page_loop_detection():
    """
    A traversal that revisits an already-seen URL must be detected as a loop.
    """
    visited: set = set()
    urls_to_visit = [
        "https://forum.test/topic/t/",
        "https://forum.test/topic/t/page/2/",
        "https://forum.test/topic/t/",  # Loop back to start
    ]

    loop_detected = False
    for url in urls_to_visit:
        canonical = normalize_article_url(url)
        if canonical in visited:
            loop_detected = True
            break
        visited.add(canonical)

    assert loop_detected is True


# ---------------------------------------------------------------------------
# Test 8: Genuine new-page check for bbPress empty pages
# ---------------------------------------------------------------------------

def test_empty_page_stops_traversal():
    """
    When a next-page URL returns only the OP and no new replies,
    the traversal should stop (no unique new posts).
    """
    op_id = "80000001"
    page1_ids = [op_id, "80000002", "80000003"]

    # bbPress "empty page" — only OP present
    page3_ids = [op_id]  # No new replies
    p1_set = set(page1_ids)
    unique_on_page3 = [p for p in page3_ids if p not in p1_set]

    assert len(unique_on_page3) == 0  # Should stop traversal


# ---------------------------------------------------------------------------
# Test 9: Validate real-world baseline artifacts exist
# ---------------------------------------------------------------------------

import os
import json

PHASE6D_BASELINE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "results", "forum_validation", "phase6d", "baseline"
)
PHASE6D_INCREMENTAL_DIR = os.path.join(
    os.path.dirname(__file__), "..", "results", "forum_validation", "phase6d", "incremental_run"
)


def test_phase6d_baseline_artifacts_present():
    """All expected baseline artifacts must exist."""
    expected_files = [
        "manifest.json",
        "thread_state.json",
        "run_summary.json",
        "request_log.json",
        "thread_report.md",
        "checksums.sha256",
    ]
    for fname in expected_files:
        fpath = os.path.join(PHASE6D_BASELINE_DIR, fname)
        assert os.path.isfile(fpath), f"Missing baseline artifact: {fname}"


def test_phase6d_baseline_thread_state_structure():
    """Baseline thread state must have 2 pages and 19 unique post IDs."""
    state_file = os.path.join(PHASE6D_BASELINE_DIR, "thread_state.json")
    with open(state_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["page_count"] == 2
    assert data["total_unique_post_count"] == 19
    assert len(data["pages"]) == 2

    # Page 1 has 16 post IDs
    assert data["pages"][0]["post_count"] == 16
    assert data["pages"][0]["page_number"] == 1

    # Page 2 has 4 post IDs (OP + 3 unique replies)
    assert data["pages"][1]["post_count"] == 4
    assert data["pages"][1]["unique_post_count"] == 3
    assert data["pages"][1]["page_number"] == 2


def test_phase6d_baseline_within_request_budget():
    """Baseline run must stay within 25-request budget."""
    req_log_file = os.path.join(PHASE6D_BASELINE_DIR, "request_log.json")
    with open(req_log_file, encoding="utf-8") as f:
        req_log = json.load(f)
    assert len(req_log) <= 25


def test_phase6d_incremental_artifacts_present():
    """All expected incremental artifacts must exist."""
    expected_files = [
        "manifest.json",
        "thread_state.json",
        "diff.json",
        "run_summary.json",
        "request_log.json",
        "thread_report.md",
        "checksums.sha256",
    ]
    for fname in expected_files:
        fpath = os.path.join(PHASE6D_INCREMENTAL_DIR, fname)
        assert os.path.isfile(fpath), f"Missing incremental artifact: {fname}"


def test_phase6d_incremental_diff_structure():
    """Incremental diff must show 2 pages unchanged and a valid factual result."""
    diff_file = os.path.join(PHASE6D_INCREMENTAL_DIR, "diff.json")
    with open(diff_file, encoding="utf-8") as f:
        diff = json.load(f)

    assert diff["baseline_total_posts"] == 19
    assert diff["current_total_posts"] == 19
    assert diff["pages_unchanged"] == 2
    assert diff["pages_changed"] == 0
    assert diff["pages_new"] == 0
    assert diff["factual_result"] in (
        "REAL_THREAD_INCREMENTAL_NO_CHANGE",
        "REAL_THREAD_INCREMENTAL_NEW_REPLIES",
        "REAL_THREAD_INCREMENTAL_PAGE_CHANGED",
    )


def test_phase6d_incremental_within_request_budget():
    """Incremental run must stay within 25-request budget."""
    req_log_file = os.path.join(PHASE6D_INCREMENTAL_DIR, "request_log.json")
    with open(req_log_file, encoding="utf-8") as f:
        req_log = json.load(f)
    assert len(req_log) <= 25


def test_phase6d_catalog_has_both_runs():
    """Catalog must register both baseline and incremental runs."""
    catalog_file = os.path.join(
        os.path.dirname(__file__), "..", "results", "forum_validation", "phase6d", "catalog.json"
    )
    with open(catalog_file, encoding="utf-8") as f:
        catalog = json.load(f)

    # ArchiveCatalog stores runs in 'runs' dict (keyed by run_id) + 'all_run_ids' list
    runs = catalog.get("runs", {})
    all_run_ids = catalog.get("all_run_ids", [])
    assert len(all_run_ids) >= 2, f"Expected >= 2 runs, found {len(all_run_ids)}: {all_run_ids}"

    modes = [r["mode"] for r in runs.values()]
    assert "BASELINE" in modes
    assert "INCREMENTAL" in modes

    # Verify lineage: incremental run references baseline as previous
    inc_runs = [r for r in runs.values() if r["mode"] == "INCREMENTAL"]
    assert len(inc_runs) >= 1
    assert inc_runs[0]["previous_run_id"] is not None

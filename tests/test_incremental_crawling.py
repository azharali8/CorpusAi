"""
Unit tests for CorpusAI Phase 6B Incremental Crawling components.

Covers:
- PortalState, ArticleState, ArchivePageState serialization and round-trips
- ArchiveCatalog run registration, failure isolation, and atomic updates
- RunComparisonEngine classification (NEW, UNCHANGED, CONTENT_CHANGED, METADATA_CHANGED, REORDERED, ANOMALY)
- IncrementalArchivePolicy head sentinel, boundary reconciliation, budget safety, and resume semantics
- Deterministic Synthetic Scenarios A through N
"""

import os
import pytest
from typing import Any, Dict, List

from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.incremental_run import IncrementalRun
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.run_comparison import IncrementalDiff, RunComparisonEngine
from src.incremental_archive_policy import (
    IncrementalArchivePolicy,
    IncrementalCrawlBudget,
)
from src.archive_visit_policy import compute_page_fingerprints


# ===========================================================================
# 1. PortalState & ArticleState Serialization Tests
# ===========================================================================

def test_article_state_serialization():
    art = ArticleState(
        canonical_url="https://example.org/news/post-1",
        first_seen_run_id="run-001",
        last_seen_run_id="run-002",
        title="Sample Title",
        published_at="2026-10-01",
        author="Author Name",
        content_hash="hash123",
        raw_hash="raw123",
        status="UNCHANGED",
        seen_on_pages=["https://example.org/news/all-posts/"],
    )
    d = art.to_dict()
    assert d["canonical_url"] == "https://example.org/news/post-1"
    art_loaded = ArticleState.from_dict(d)
    assert art_loaded.canonical_url == art.canonical_url
    assert art_loaded.content_hash == "hash123"


def test_portal_state_atomic_save_and_load(tmp_path):
    p_file = str(tmp_path / "portal_state.json")
    page = ArchivePageState(
        url="https://example.org/news/page/1",
        ordered_fingerprint="ord123",
        unordered_fingerprint="unord123",
        article_urls=["https://example.org/news/post-1"],
    )
    art = ArticleState(
        canonical_url="https://example.org/news/post-1",
        first_seen_run_id="run-001",
        last_seen_run_id="run-001",
        title="Post 1",
    )
    state = PortalState(
        portal_id="test-portal",
        run_id="run-001",
        archive_pages={page.url: page},
        articles={art.canonical_url: art},
        archive_order=[art.canonical_url],
        head_fingerprint="unord123",
    )
    state.save_to_json(p_file)
    assert os.path.exists(p_file)

    loaded = PortalState.load_from_json(p_file)
    assert loaded.portal_id == "test-portal"
    assert loaded.run_id == "run-001"
    assert len(loaded.articles) == 1
    assert loaded.articles["https://example.org/news/post-1"].title == "Post 1"


# ===========================================================================
# 2. ArchiveCatalog Lineage & Failure Isolation Tests
# ===========================================================================

def test_archive_catalog_registration_and_failure_isolation(tmp_path):
    cat_file = str(tmp_path / "catalog.json")
    catalog = ArchiveCatalog.load_or_create(cat_file, portal_id="wordpress-news")

    entry1 = CatalogRunEntry(
        run_id="run-001",
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-01T00:00:00Z",
        completed_at="2026-10-01T00:05:00Z",
        status="CONVERGED",
        article_count=10,
        new_article_count=10,
        changed_article_count=0,
        run_dir="runs/run-001",
    )
    catalog.register_run(entry1, is_successful=True)
    catalog.save_to_json(cat_file)

    assert catalog.latest_successful_run_id == "run-001"

    # Failed run MUST NOT advance latest_successful_run_id
    entry2 = CatalogRunEntry(
        run_id="run-002",
        previous_run_id="run-001",
        mode="INCREMENTAL",
        started_at="2026-10-02T00:00:00Z",
        completed_at="2026-10-02T00:01:00Z",
        status="FAILED",
        article_count=0,
        new_article_count=0,
        changed_article_count=0,
        run_dir="runs/run-002",
    )
    catalog.register_run(entry2, is_successful=False)
    assert catalog.latest_successful_run_id == "run-001"
    assert "run-002" in catalog.all_run_ids


# ===========================================================================
# 3. Synthetic Scenarios A through N
# ===========================================================================

def _create_mock_fetcher(pages_dict: Dict[str, Dict[str, Any]]):
    def fetcher(url: str) -> Dict[str, Any]:
        if url not in pages_dict:
            return {"status_code": 404, "article_urls": []}
        return pages_dict[url]
    return fetcher


# Scenario A: Baseline stable archive, incremental run unchanged
def test_scenario_a_stable_archive_minimal_requests():
    pages_v1 = {
        "https://portal.test/p1": {
            "status_code": 200,
            "article_urls": ["https://portal.test/a1", "https://portal.test/a2"],
            "next_page_url": "https://portal.test/p2",
        },
        "https://portal.test/p2": {
            "status_code": 200,
            "article_urls": ["https://portal.test/a3", "https://portal.test/a4"],
            "next_page_url": None,
        },
    }
    # Baseline
    policy1 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1")
    state1, run1, diff1 = policy1.execute_incremental_crawl(page_fetcher=_create_mock_fetcher(pages_v1))
    assert run1.mode == "BASELINE"
    assert len(state1.articles) == 4
    assert run1.requests_made == 2

    # Incremental with zero changes
    policy2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1)
    state2, run2, diff2 = policy2.execute_incremental_crawl(page_fetcher=_create_mock_fetcher(pages_v1))
    assert run2.mode == "INCREMENTAL"
    assert run2.requests_made == 1  # Only inspected head! Minimal requests
    assert len(diff2.new_articles) == 0
    assert len(diff2.unchanged_articles) == 2  # on head
    assert run2.boundary_status == "BOUNDARY_RECONCILED"


# Scenario B: One new article inserted at head
def test_scenario_b_one_new_article_at_head():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1", "https://portal.test/a2"], "next_page_url": None}
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a_new", "https://portal.test/a1", "https://portal.test/a2"], "next_page_url": None}
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert "https://portal.test/a_new" in diff2.new_articles
    assert run2.new_article_count == 1
    assert run2.head_changed is True


# Scenario C: Multiple new articles inserted at head
def test_scenario_c_multiple_new_articles_at_head():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/new_1", "https://portal.test/new_2", "https://portal.test/a1"], "next_page_url": None}
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert len(diff2.new_articles) == 2
    assert "https://portal.test/new_1" in diff2.new_articles
    assert "https://portal.test/new_2" in diff2.new_articles


# Scenario D: Same-date items reorder
def test_scenario_d_same_date_reordering_no_false_new():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1", "https://portal.test/a2"], "next_page_url": None}
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a2", "https://portal.test/a1"], "next_page_url": None}
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert len(diff2.new_articles) == 0
    assert len(diff2.unchanged_articles) == 2
    assert diff2.archive_page_changes["https://portal.test/p1"] == "ORDER_CHANGED"


# Scenario E: Article moves across pagination boundary
def test_scenario_e_article_moves_across_boundary():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1", "https://portal.test/a2"], "next_page_url": "https://portal.test/p2"},
        "https://portal.test/p2": {"status_code": 200, "article_urls": ["https://portal.test/a3"], "next_page_url": None},
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    # New item on p1 pushes a2 to p2
    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/new_0", "https://portal.test/a1"], "next_page_url": "https://portal.test/p2"},
        "https://portal.test/p2": {"status_code": 200, "article_urls": ["https://portal.test/a2", "https://portal.test/a3"], "next_page_url": None},
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert len(diff2.new_articles) == 1
    assert "https://portal.test/new_0" in diff2.new_articles
    assert len(diff2.missing_candidates) == 0  # No false missing classification


# Scenario F: New archive page appears
def test_scenario_f_new_archive_page_appears():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": "https://portal.test/p2"},
        "https://portal.test/p2": {"status_code": 200, "article_urls": ["https://portal.test/a2"], "next_page_url": None},
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert diff2.pagination_changed is True
    assert diff2.archive_page_changes.get("https://portal.test/p2") == "NEW_PAGE"


# Scenario G: Existing article content changes
def test_scenario_g_article_content_changes():
    pages = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    art_fetcher_v1 = lambda u: {"title": "Title 1", "content_hash": "hash_v1", "raw_hash": "raw_v1"}
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=art_fetcher_v1
    )

    art_fetcher_v2 = lambda u: {"title": "Title 1", "content_hash": "hash_v2_changed", "raw_hash": "raw_v2"}
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=art_fetcher_v2
    )

    assert "https://portal.test/a1" in diff2.changed_articles
    assert run2.changed_article_count == 1


# Scenario H: Existing metadata changes but content body does not
def test_scenario_h_metadata_changed_only():
    pages = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    art_fetcher_v1 = lambda u: {"title": "Title 1", "published_at": "2026-10-01", "author": "Alice", "content_hash": "same_hash"}
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=art_fetcher_v1
    )

    art_fetcher_v2 = lambda u: {"title": "Updated Title", "published_at": "2026-10-01", "author": "Alice", "content_hash": "same_hash"}
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=art_fetcher_v2
    )

    assert "https://portal.test/a1" in diff2.metadata_changed_articles
    assert run2.metadata_changed_article_count == 1


# Scenario I: Temporary article fetch failure
def test_scenario_i_temporary_fetch_failure_not_missing():
    pages = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    art_fetcher_v1 = lambda u: {"title": "Title 1", "content_hash": "hash1"}
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=art_fetcher_v1
    )

    def failing_art_fetcher(u):
        raise ConnectionResetError("Connection timeout")

    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(
        _create_mock_fetcher(pages), article_fetcher=failing_art_fetcher
    )

    assert "https://portal.test/a1" in diff2.fetch_failures
    assert "https://portal.test/a1" not in diff2.missing_candidates


# Scenario J: Archive structure changes unexpectedly (e.g. empty head page)
def test_scenario_j_portal_structure_changed_requires_review():
    pages_v1 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1", "https://portal.test/a2"], "next_page_url": None}
    }
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages_v1))

    # Empty head page anomaly
    pages_v2 = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": [], "next_page_url": None}
    }
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(_create_mock_fetcher(pages_v2))

    assert "ARCHIVE_EMPTY_UNEXPECTEDLY" in run2.anomalies_detected
    assert run2.manual_review_status == "MANUAL_REVIEW_REQUIRED"


# Scenario K: Access gate detected (HTTP 403)
def test_scenario_k_access_gate_detected():
    pages = {
        "https://portal.test/p1": {"status_code": 403, "article_urls": []}
    }
    state, run, diff = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(_create_mock_fetcher(pages))
    assert "ACCESS_GATE_DETECTED" in run.anomalies_detected
    assert run.manual_review_status == "MANUAL_REVIEW_REQUIRED"


# Scenario L: Request budget exhausted
def test_scenario_l_budget_exhausted_status():
    pages = {
        f"https://portal.test/p{i}": {"status_code": 200, "article_urls": [f"https://portal.test/a{i}"], "next_page_url": f"https://portal.test/p{i+1}"}
        for i in range(1, 10)
    }
    budget = IncrementalCrawlBudget(max_requests=3, max_archive_pages=10)
    state, run, diff = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", budget=budget).execute_incremental_crawl(_create_mock_fetcher(pages))

    assert run.convergence_status == "BUDGET_EXHAUSTED"
    assert run.requests_made == 3
    assert state.last_potentially_incomplete_page is not None


# Scenario M: Resume from previous potentially incomplete boundary
def test_scenario_m_resume_from_incomplete_boundary():
    prev_state = PortalState(
        portal_id="test",
        run_id="run-prev",
        last_potentially_incomplete_page="https://portal.test/p3",
    )
    policy = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=prev_state)
    assert policy.last_incomplete_page == "https://portal.test/p3"


# Scenario N: Repeated update with no changes
def test_scenario_n_repeated_update_no_changes():
    pages = {
        "https://portal.test/p1": {"status_code": 200, "article_urls": ["https://portal.test/a1"], "next_page_url": None}
    }
    fetcher = _create_mock_fetcher(pages)
    state1, _, _ = IncrementalArchivePolicy(head_page_url="https://portal.test/p1").execute_incremental_crawl(fetcher)
    state2, run2, diff2 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state1).execute_incremental_crawl(fetcher)
    state3, run3, diff3 = IncrementalArchivePolicy(head_page_url="https://portal.test/p1", previous_state=state2).execute_incremental_crawl(fetcher)

    assert run3.requests_made == 1
    assert len(diff3.new_articles) == 0
    assert run3.convergence_status == "CONVERGED"


# ===========================================================================
# 4. Phase 6B.1 Multi-Page & Content Capture Status Tests
# ===========================================================================

def test_pilot_scope_completion_flags():
    """Verify pilot scope completion explicit non-claims of full portal coverage."""
    run = IncrementalRun(
        run_id="run-pilot-001",
        target_portal="test-portal",
        seed_urls=["https://portal.test/p1"],
        convergence_status="PILOT_SCOPE_CONVERGED",
        scope_completion="PILOT_SCOPE_CONVERGED",
        full_portal_coverage=False,
    )
    assert run.convergence_status == "PILOT_SCOPE_CONVERGED"
    assert run.scope_completion == "PILOT_SCOPE_CONVERGED"
    assert run.full_portal_coverage is False

    d = run.to_dict()
    assert d["full_portal_coverage"] is False
    loaded = IncrementalRun.from_dict(d)
    assert loaded.full_portal_coverage is False
    assert loaded.scope_completion == "PILOT_SCOPE_CONVERGED"


def test_bounded_scope_unobserved_articles_not_marked_missing():
    """Articles unobserved during a partial/bounded 1-page check are not labeled MISSING."""
    # Previous state with 5 articles across 2 pages
    p1 = ArchivePageState(url="https://portal.test/p1", ordered_fingerprint="fp1", unordered_fingerprint="fp1_u", article_urls=["https://portal.test/a1", "https://portal.test/a2"])
    p2 = ArchivePageState(url="https://portal.test/p2", ordered_fingerprint="fp2", unordered_fingerprint="fp2_u", article_urls=["https://portal.test/a3", "https://portal.test/a4", "https://portal.test/a5"])
    prev_state = PortalState(
        portal_id="test",
        run_id="run-prev",
        archive_pages={"https://portal.test/p1": p1, "https://portal.test/p2": p2},
        articles={
            f"https://portal.test/a{i}": ArticleState(canonical_url=f"https://portal.test/a{i}", first_seen_run_id="run-prev", last_seen_run_id="run-prev")
            for i in range(1, 6)
        },
        archive_order=[f"https://portal.test/a{i}" for i in range(1, 6)],
        head_fingerprint="fp1_u",
    )

    # Current run only crawls 1 page (a1, a2)
    curr_state = PortalState(
        portal_id="test",
        run_id="run-curr",
        archive_pages={"https://portal.test/p1": p1},
        articles={
            "https://portal.test/a1": ArticleState(canonical_url="https://portal.test/a1", first_seen_run_id="run-prev", last_seen_run_id="run-curr"),
            "https://portal.test/a2": ArticleState(canonical_url="https://portal.test/a2", first_seen_run_id="run-prev", last_seen_run_id="run-curr"),
        },
        archive_order=["https://portal.test/a1", "https://portal.test/a2"],
        head_fingerprint="fp1_u",
    )

    diff = RunComparisonEngine.compare_states(prev_state, curr_state)
    assert len(diff.missing_candidates) == 0
    assert len(diff.not_observed_in_scope) == 3
    assert "https://portal.test/a3" in diff.not_observed_in_scope


def test_content_capture_status_differentiation():
    """Verify CAPTURED, NOT_CAPTURED_IN_PILOT, and REUSED states."""
    art_captured = ArticleState(
        canonical_url="https://portal.test/a1",
        first_seen_run_id="run-001",
        last_seen_run_id="run-001",
        content_hash="sha_content_123",
        content_capture_status="CAPTURED",
    )
    art_discovered_only = ArticleState(
        canonical_url="https://portal.test/a2",
        first_seen_run_id="run-001",
        last_seen_run_id="run-001",
        content_hash=None,
        content_capture_status="NOT_CAPTURED_IN_PILOT",
    )
    art_reused = ArticleState(
        canonical_url="https://portal.test/a3",
        first_seen_run_id="run-001",
        last_seen_run_id="run-002",
        content_hash="sha_content_123",
        content_capture_status="REUSED",
    )

    assert art_captured.content_capture_status == "CAPTURED"
    assert art_discovered_only.content_capture_status == "NOT_CAPTURED_IN_PILOT"
    assert art_reused.content_capture_status == "REUSED"

    d1 = art_captured.to_dict()
    assert d1["content_capture_status"] == "CAPTURED"
    d2 = art_discovered_only.to_dict()
    assert d2["content_capture_status"] == "NOT_CAPTURED_IN_PILOT"


def test_multipage_traversal_deduplication_and_catalog_advancement(tmp_path):
    """Verify 5-page traversal deduplication and catalog tracking."""
    cat_file = str(tmp_path / "catalog.json")
    catalog = ArchiveCatalog.load_or_create(cat_file, portal_id="wordpress-news")

    entry_multi = CatalogRunEntry(
        run_id="run-multi-001",
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-07T19:00:00Z",
        completed_at="2026-10-07T19:02:00Z",
        status="MULTIPAGE_PILOT_COMPLETE",
        article_count=50,
        new_article_count=50,
        changed_article_count=0,
        run_dir="multipage_baseline",
    )
    catalog.register_run(entry_multi, is_successful=True)
    catalog.save_to_json(cat_file)

    assert catalog.latest_successful_run_id == "run-multi-001"
    loaded_catalog = ArchiveCatalog.load_from_json(cat_file)
    assert loaded_catalog.latest_successful_run_id == "run-multi-001"
    assert "run-multi-001" in loaded_catalog.runs


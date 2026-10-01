"""
Unit tests for ArchiveVisitPolicy, ArticleLedger, fingerprinting, and crawl convergence.

Verifies all 15 required test criteria:
1. identical set + different order classified correctly (ORDER_CHANGED_ONLY);
2. changed article set detected (ARTICLE_SET_CHANGED);
3. duplicate article across pages detected;
4. global article ledger deduplicates correctly;
5. new article insertion detected;
6. adjacent pages scheduled after set change;
7. stable observations lead to page stability;
8. ordering-only change does not unnecessarily invalidate set stability;
9. visit limit enforced;
10. total request budget enforced;
11. stale cache scenario handled deterministically;
12. crawler convergence reported correctly;
13. incomplete run is not reported as complete;
14. naive single-pass scenario demonstrates measurable coverage result;
15. robust policy coverage measured correctly.
"""

import pytest
from src.archive_visit_policy import (
    ArchiveVisitPolicy,
    ArticleLedger,
    compute_page_fingerprints,
    create_policy,
    normalize_article_url,
)
from research.crawling.experiments.portal_simulator import UnstablePortalSimulator
from research.crawling.experiments.run_stability_experiment import execute_crawl


# ---------------------------------------------------------------------------
# 1. Identical set + different order classified correctly
# ---------------------------------------------------------------------------
def test_identical_set_different_order_classified():
    policy = ArchiveVisitPolicy(required_stable_observations=2)
    urls_v1 = ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    urls_v2 = ["https://example.com/c", "https://example.com/a", "https://example.com/b"]

    r1 = policy.register_observation(1, urls_v1)
    assert r1["change_type"] == "FIRST_VISIT"

    r2 = policy.register_observation(1, urls_v2)
    assert r2["change_type"] == "ORDER_CHANGED_ONLY"
    assert policy.order_only_changes == 1


# ---------------------------------------------------------------------------
# 2. Changed article set detected
# ---------------------------------------------------------------------------
def test_changed_article_set_detected():
    policy = ArchiveVisitPolicy()
    urls_v1 = ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    urls_v2 = ["https://example.com/a", "https://example.com/b", "https://example.com/d"]

    policy.register_observation(1, urls_v1)
    r2 = policy.register_observation(1, urls_v2)
    assert r2["change_type"] == "ARTICLE_SET_CHANGED"
    assert policy.set_changes == 1


# ---------------------------------------------------------------------------
# 3. Duplicate article across pages detected
# ---------------------------------------------------------------------------
def test_duplicate_article_across_pages_detected():
    policy = ArchiveVisitPolicy()
    policy.register_observation(1, ["https://example.com/item_01", "https://example.com/item_02"])
    policy.register_observation(2, ["https://example.com/item_02", "https://example.com/item_03"])

    assert policy.ledger.duplicate_observation_count == 1
    assert policy.ledger.get_unique_count() == 3
    assert policy.ledger.articles["https://example.com/item_02"]["seen_on_pages"] == [1, 2]


# ---------------------------------------------------------------------------
# 4. Global article ledger deduplicates correctly
# ---------------------------------------------------------------------------
def test_global_article_ledger_deduplication():
    ledger = ArticleLedger()
    is_new1 = ledger.record_article("https://example.com/article?id=10#part1", source_page=1, visit_number=1)
    is_new2 = ledger.record_article("https://example.com/article?id=10#part2", source_page=2, visit_number=2)

    assert is_new1 is True
    assert is_new2 is False
    assert ledger.get_unique_count() == 1
    assert ledger.duplicate_observation_count == 1


# ---------------------------------------------------------------------------
# 5. New article insertion detected
# ---------------------------------------------------------------------------
def test_new_article_insertion_detected():
    policy = ArchiveVisitPolicy()
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    assert policy.new_articles_count == 2

    policy.register_observation(1, ["https://example.com/new_breaking", "https://example.com/a", "https://example.com/b"])
    assert policy.new_articles_count == 3


# ---------------------------------------------------------------------------
# 6. Adjacent pages scheduled after set change
# ---------------------------------------------------------------------------
def test_adjacent_pages_scheduled_after_set_change():
    policy = ArchiveVisitPolicy(profile="ROBUST", neighbor_revisit_on_set_change=True)
    policy.register_observation(2, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(2, ["https://example.com/a", "https://example.com/c"])  # Set changed

    # Queue should contain page 2, page 1, page 3
    assert 1 in policy.revisit_queue
    assert 2 in policy.revisit_queue
    assert 3 in policy.revisit_queue


# ---------------------------------------------------------------------------
# 7. Stable observations lead to page stability
# ---------------------------------------------------------------------------
def test_stable_observations_lead_to_page_stability():
    policy = ArchiveVisitPolicy(required_stable_observations=2)
    urls = ["https://example.com/a", "https://example.com/b"]

    r1 = policy.register_observation(1, urls)
    assert r1["is_stable"] is False

    r2 = policy.register_observation(1, urls)
    assert r2["is_stable"] is True


# ---------------------------------------------------------------------------
# 8. Ordering-only change does not unnecessarily invalidate set stability
# ---------------------------------------------------------------------------
def test_ordering_change_preserves_set_stability():
    policy = ArchiveVisitPolicy(required_stable_observations=2)
    urls_v1 = ["https://example.com/a", "https://example.com/b"]
    urls_v2 = ["https://example.com/b", "https://example.com/a"]

    r1 = policy.register_observation(1, urls_v1)
    assert r1["consecutive_set_stable_count"] == 1

    r2 = policy.register_observation(1, urls_v2)
    assert r2["change_type"] == "ORDER_CHANGED_ONLY"
    assert r2["consecutive_set_stable_count"] == 2
    assert r2["is_stable"] is True


# ---------------------------------------------------------------------------
# 9. Visit limit enforced
# ---------------------------------------------------------------------------
def test_visit_limit_enforced():
    policy = ArchiveVisitPolicy(max_visits_per_page=2)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/a"])
    # Schedule should reject third visit
    can_schedule = policy.schedule_revisit(1)
    assert can_schedule is False


# ---------------------------------------------------------------------------
# 10. Total request budget enforced
# ---------------------------------------------------------------------------
def test_total_request_budget_enforced():
    policy = ArchiveVisitPolicy(max_total_requests=3)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(2, ["https://example.com/b"])
    policy.register_observation(3, ["https://example.com/c"])
    r4 = policy.register_observation(4, ["https://example.com/d"])

    assert r4["status"] == "BUDGET_EXHAUSTED"
    assert policy.budget_exhausted is True


# ---------------------------------------------------------------------------
# 11. Stale cache scenario handled deterministically
# ---------------------------------------------------------------------------
def test_stale_cache_scenario_handled_deterministically():
    sim = UnstablePortalSimulator(scenario="E_CACHED")
    v1 = sim.fetch_page(2)
    v2 = sim.fetch_page(2)
    v3 = sim.fetch_page(2)

    assert v1 == v2
    assert v2 != v3  # Updates on 3rd visit
    assert "https://portal.test/articles/item_05" in v3


# ---------------------------------------------------------------------------
# 12. Crawler convergence reported correctly
# ---------------------------------------------------------------------------
def test_crawler_convergence_reported_correctly():
    policy = ArchiveVisitPolicy(required_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(2, ["https://example.com/b"])
    policy.register_observation(2, ["https://example.com/b"])

    assert policy.is_archive_converged({1, 2}) is True


# ---------------------------------------------------------------------------
# 13. Incomplete run is not reported as complete
# ---------------------------------------------------------------------------
def test_incomplete_run_not_converged():
    policy = ArchiveVisitPolicy(required_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a"])  # Only 1 visit (not stable)

    assert policy.is_archive_converged({1}) is False


# ---------------------------------------------------------------------------
# 14. Naive single-pass scenario demonstrates measurable coverage result
# ---------------------------------------------------------------------------
def test_naive_single_pass_coverage_measurement():
    res_naive = execute_crawl(scenario_name="D_INSERTION", policy_profile="NAIVE")
    # Naive encounters shift and achieves < 100% recall
    assert res_naive["coverage_recall"] < 1.0
    assert len(res_naive["missing_articles"]) > 0
    assert res_naive["revisits"] == 0


# ---------------------------------------------------------------------------
# 15. Robust policy coverage measured correctly
# ---------------------------------------------------------------------------
def test_robust_policy_coverage_measurement():
    res_robust = execute_crawl(scenario_name="D_INSERTION", policy_profile="ROBUST")
    assert res_robust["coverage_recall"] >= 0.95
    assert res_robust["revisits"] > 0
    assert res_robust["archive_requests"] > 5

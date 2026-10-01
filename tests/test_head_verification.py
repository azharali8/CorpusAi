"""
Unit tests for Task 2B: Head-Page Sentinel, Boundary Propagation Reconciliation, and Strong Crawl Convergence.

Verifies all 17 required test criteria:
1. crawl cannot converge before head verification;
2. unchanged head allows progression toward convergence;
3. changed head invalidates previous convergence;
4. new head article is added to ledger;
5. page 2 scheduled after page 1 set change;
6. propagation reaches page N+1 when boundary shift continues;
7. propagation stops when next page is unchanged;
8. item_99 scenario is reproduced;
9. new policy attempts to recover item_99;
10. repeated head changes prevent convergence;
11. continuous mutation causes budget exhaustion or non-convergence;
12. no false `converged=true` after budget exhaustion;
13. newly created last page can be discovered;
14. order-only head change does not count as set mutation;
15. head stability counter resets after set change;
16. cached-response scenario behaves deterministically;
17. stable archive overhead is measured correctly.
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
from research.crawling.experiments.run_head_verification_experiment import execute_head_verified_crawl


# 1. Crawl cannot converge before head verification
def test_cannot_converge_before_head_verification():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    # Visit pages 1 and 2 twice (page-level stable)
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(2, ["https://example.com/c", "https://example.com/d"])
    policy.register_observation(2, ["https://example.com/c", "https://example.com/d"])

    # Individual pages are stable, but head has not been verified after initial traversal
    assert policy.is_archive_converged({1, 2}) is False


# 2. Unchanged head allows progression toward convergence
def test_unchanged_head_allows_convergence():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(2, ["https://example.com/c", "https://example.com/d"])
    policy.register_observation(2, ["https://example.com/c", "https://example.com/d"])

    policy.initial_traversal_complete = True
    # Head verification pass
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])

    assert policy.is_archive_converged({1, 2}) is True


# 3. Changed head invalidates previous convergence
def test_changed_head_invalidates_convergence():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.initial_traversal_complete = True

    # Head changed during verification
    policy.register_observation(1, ["https://example.com/new_x", "https://example.com/a"])

    assert policy.head_verified_after_traversal is False
    assert policy.is_archive_converged({1}) is False


# 4. New head article is added to ledger
def test_new_head_article_added_to_ledger():
    policy = ArchiveVisitPolicy(head_verification_enabled=True)
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.initial_traversal_complete = True
    policy.current_phase = "HEAD_VERIFICATION"

    policy.register_observation(1, ["https://example.com/breaking_news", "https://example.com/a"])

    assert "https://example.com/breaking_news" in policy.ledger.get_all_urls()
    assert policy.new_articles_during_verification == 1


# 5. Page 2 scheduled after page 1 set change
def test_page_2_scheduled_after_page_1_change():
    policy = ArchiveVisitPolicy(boundary_propagation_enabled=True)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/x", "https://example.com/a"])  # Changed

    assert 2 in policy.revisit_queue


# 6. Propagation reaches page N+1 when boundary shift continues
def test_propagation_reaches_next_page():
    policy = ArchiveVisitPolicy(boundary_propagation_enabled=True)
    policy.trigger_boundary_propagation(2)
    assert policy.propagation_depth == 3
    assert 3 in policy.revisit_queue


# 7. Propagation stops when next page is unchanged
def test_propagation_stops_when_unchanged():
    policy = ArchiveVisitPolicy()
    policy.register_observation(2, ["https://example.com/item1"])
    # Same observation again -> no boundary propagation triggered
    r2 = policy.register_observation(2, ["https://example.com/item1"])
    assert r2["change_type"] == "NO_CHANGE"
    assert 3 not in policy.revisit_queue


# 8. Item_99 scenario is reproduced (Naive misses items under mid-crawl shift)
def test_item_99_reproduced_by_naive():
    res = execute_head_verified_crawl(scenario_name="D_INSERTION", policy_profile="NAIVE")
    assert res["coverage_recall"] < 1.0


# 9. New policy attempts to recover item_99
def test_new_policy_recovers_item_99():
    res = execute_head_verified_crawl(scenario_name="D_INSERTION", policy_profile="NEW_ROBUST_HEAD_VERIFIED")
    assert res["coverage_recall"] == 1.0
    assert "https://portal.test/articles/item_99_breaking_news" not in res["missing_articles"]


# 10. Repeated head changes prevent convergence
def test_repeated_head_changes_prevent_convergence():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    policy.initial_traversal_complete = True
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/b"])
    policy.register_observation(1, ["https://example.com/c"])

    assert policy.is_archive_converged({1}) is False


# 11. Continuous mutation causes non-convergence
def test_continuous_mutation_causes_non_convergence():
    res = execute_head_verified_crawl(scenario_name="H_CONTINUOUS_MUTATION", policy_profile="NEW_ROBUST_HEAD_VERIFIED")
    assert res["converged"] is False
    assert res["termination_reason"] in ("UNSTABLE_AT_TERMINATION", "BUDGET_EXHAUSTED")


# 12. No false converged=true after budget exhaustion
def test_no_false_converged_after_budget_exhaustion():
    policy = ArchiveVisitPolicy(max_total_requests=2)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/a"])  # Over budget

    assert policy.budget_exhausted is True
    assert policy.is_archive_converged({1}) is False


# 13. Newly created last page can be discovered
def test_newly_created_last_page_discovered():
    res = execute_head_verified_crawl(scenario_name="I_EXPANDING_PAGE_COUNT", policy_profile="NEW_ROBUST_HEAD_VERIFIED")
    assert res["coverage_recall"] == 1.0
    assert res["discovered_articles"] == 21


# 14. Order-only head change does not count as set mutation
def test_order_only_head_change_preserves_head_set():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a", "https://example.com/b"])
    policy.initial_traversal_complete = True
    r = policy.register_observation(1, ["https://example.com/b", "https://example.com/a"])

    assert r["change_type"] == "ORDER_CHANGED_ONLY"
    assert policy.consecutive_head_stable_count == 2
    assert policy.head_verified_after_traversal is True


# 15. Head stability counter resets after set change
def test_head_stability_counter_resets_after_set_change():
    policy = ArchiveVisitPolicy(head_verification_enabled=True, required_head_stable_observations=2)
    policy.register_observation(1, ["https://example.com/a"])
    policy.register_observation(1, ["https://example.com/a"])
    assert policy.consecutive_head_stable_count == 2

    # Mutate set
    policy.register_observation(1, ["https://example.com/a", "https://example.com/new"])
    assert policy.consecutive_head_stable_count == 1
    assert policy.head_verified_after_traversal is False


# 16. Cached-response scenario behaves deterministically
def test_cached_response_behaves_deterministically():
    res = execute_head_verified_crawl(scenario_name="E_CACHED", policy_profile="NEW_ROBUST_HEAD_VERIFIED")
    assert res["coverage_recall"] == 1.0
    assert res["converged"] is True


# 17. Stable archive overhead is measured correctly
def test_stable_archive_overhead_measured():
    res_naive = execute_head_verified_crawl(scenario_name="A_STABLE", policy_profile="NAIVE")
    res_verified = execute_head_verified_crawl(scenario_name="A_STABLE", policy_profile="NEW_ROBUST_HEAD_VERIFIED")

    assert res_naive["total_requests"] == 5
    assert res_verified["total_requests"] == 11
    assert res_verified["head_verification_requests"] == 1

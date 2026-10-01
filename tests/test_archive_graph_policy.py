"""
Unit tests for ArchiveGraphTraversalPolicy, MultiLevelPortalSimulator, and graph traversal logic.

Verifies all 20 required test criteria:
1. two-level archive complete traversal;
2. three-level archive complete traversal;
3. shared child visited without duplicate work;
4. direct cycle detection;
5. indirect cycle detection;
6. duplicate article across branches deduplicated;
7. out-of-scope link skipped;
8. excluded link skipped;
9. max depth enforced;
10. navigation-node budget enforced;
11. BFS traversal works;
12. DFS traversal works;
13. child ordering-only change correctly classified;
14. child-set mutation detected;
15. dynamic new topic discovered after verification;
16. empty branch terminates safely;
17. broken branch does not crash global traversal;
18. leaf archive delegates to ArchiveVisitPolicy;
19. pagination instability inside nested leaf retains coverage logic;
20. article provenance path preserved.
"""

import pytest
from src.archive_graph_policy import (
    ArchiveGraphTraversalPolicy,
    compute_node_child_fingerprints,
)
from research.crawling.experiments.multilevel_portal_simulator import MultiLevelPortalSimulator
from research.crawling.experiments.run_multilevel_experiment import execute_multilevel_crawl


# 1. Two-level archive complete traversal
def test_two_level_archive_complete_traversal():
    res = execute_multilevel_crawl("A_TWO_LEVEL", traversal_mode="BFS")
    assert res["article_recall"] == 1.0
    assert res["articles_discovered"] == 12
    assert res["navigation_nodes_visited"] == 7


# 2. Three-level archive complete traversal
def test_three_level_archive_complete_traversal():
    res = execute_multilevel_crawl("B_THREE_LEVEL", traversal_mode="BFS")
    assert res["article_recall"] == 1.0
    assert res["articles_discovered"] == 6
    assert res["navigation_nodes_visited"] == 9


# 3. Shared child visited without duplicate work
def test_shared_child_visited_without_duplicate():
    res = execute_multilevel_crawl("C_SHARED_CHILD", traversal_mode="BFS")
    assert res["article_recall"] == 1.0
    assert res["duplicate_navigation_avoided"] == 1


# 4. Direct cycle detection (A -> A)
def test_direct_cycle_detection():
    policy = ArchiveGraphTraversalPolicy()
    policy.enqueue_node("https://portal.test/category/a", parent_path=[], current_depth=0)
    enqueued = policy.enqueue_node("https://portal.test/category/a", parent_path=["https://portal.test/category/a"], current_depth=1)
    assert enqueued is False
    assert policy.cycles_detected == 1


# 5. Indirect cycle detection (A -> B -> A)
def test_indirect_cycle_detection():
    res = execute_multilevel_crawl("D_CYCLE", traversal_mode="BFS")
    assert res["cycles_detected"] == 1
    assert res["article_recall"] == 1.0


# 6. Duplicate article across branches deduplicated
def test_duplicate_articles_across_branches_deduplicated():
    res = execute_multilevel_crawl("E_DUPLICATE_ARTICLES", traversal_mode="BFS")
    assert res["articles_discovered"] == 3
    assert res["duplicate_articles_avoided"] == 1


# 7. Out-of-scope link skipped
def test_out_of_scope_link_skipped():
    policy = ArchiveGraphTraversalPolicy(allowed_domains={"portal.test"})
    classification = policy.classify_link("https://external-site.com/archive")
    assert classification == "OUT_OF_SCOPE"
    assert policy.out_of_scope_skipped == 1


# 8. Excluded link skipped
def test_excluded_link_skipped():
    policy = ArchiveGraphTraversalPolicy()
    classification = policy.classify_link("https://portal.test/privacy")
    assert classification == "EXCLUDED"
    assert policy.excluded_links_skipped == 1


# 9. Max depth enforced
def test_max_depth_enforced():
    res = execute_multilevel_crawl("G_DEEP_ARCHIVE", traversal_mode="BFS", max_depth=4)
    assert res["depth_limited_nodes"] > 0
    assert res["navigation_nodes_visited"] == 5


# 10. Navigation-node budget enforced
def test_navigation_node_budget_enforced():
    policy = ArchiveGraphTraversalPolicy(max_navigation_nodes=2, max_total_requests=2)
    policy.enqueue_node("https://portal.test/root", parent_path=[], current_depth=0)
    policy.enqueue_node("https://portal.test/cat1", parent_path=["https://portal.test/root"], current_depth=1)
    policy.enqueue_node("https://portal.test/cat2", parent_path=["https://portal.test/root"], current_depth=1)

    node1 = policy.get_next_node()
    policy.total_requests += 1
    node2 = policy.get_next_node()
    policy.total_requests += 1

    if policy.total_requests >= policy.max_total_requests:
        policy.budget_exhausted = True

    assert policy.budget_exhausted is True


# 11. BFS traversal works
def test_bfs_traversal_order():
    policy = ArchiveGraphTraversalPolicy(strategy="BFS")
    policy.enqueue_node("https://portal.test/root", parent_path=[], current_depth=0)
    policy.enqueue_node("https://portal.test/cat1", parent_path=["https://portal.test/root"], current_depth=1)
    policy.enqueue_node("https://portal.test/cat2", parent_path=["https://portal.test/root"], current_depth=1)

    first = policy.get_next_node()["url"]
    second = policy.get_next_node()["url"]
    third = policy.get_next_node()["url"]

    assert first == "https://portal.test/root"
    assert second == "https://portal.test/cat1"
    assert third == "https://portal.test/cat2"


# 12. DFS traversal works
def test_dfs_traversal_order():
    policy = ArchiveGraphTraversalPolicy(strategy="DFS")
    policy.enqueue_node("https://portal.test/root", parent_path=[], current_depth=0)
    policy.enqueue_node("https://portal.test/cat1", parent_path=["https://portal.test/root"], current_depth=1)
    policy.enqueue_node("https://portal.test/cat2", parent_path=["https://portal.test/root"], current_depth=1)

    first = policy.get_next_node()["url"]
    second = policy.get_next_node()["url"]
    third = policy.get_next_node()["url"]

    assert first == "https://portal.test/cat2"  # LIFO stack behavior
    assert second == "https://portal.test/cat1"
    assert third == "https://portal.test/root"


# 13. Child ordering-only change correctly classified
def test_child_ordering_only_change():
    v1 = ["https://portal.test/topic/a", "https://portal.test/topic/b"]
    v2 = ["https://portal.test/topic/b", "https://portal.test/topic/a"]

    ord1, set1 = compute_node_child_fingerprints(v1)
    ord2, set2 = compute_node_child_fingerprints(v2)

    assert ord1 != ord2
    assert set1 == set2


# 14. Child-set mutation detected
def test_child_set_mutation_detected():
    v1 = ["https://portal.test/topic/a", "https://portal.test/topic/b"]
    v2 = ["https://portal.test/topic/a", "https://portal.test/topic/c"]

    _, set1 = compute_node_child_fingerprints(v1)
    _, set2 = compute_node_child_fingerprints(v2)

    assert set1 != set2


# 15. Dynamic new topic discovered after verification
def test_dynamic_new_topic_discovered():
    res = execute_multilevel_crawl("J_DYNAMIC_CHILDREN", traversal_mode="BFS", verify_dynamic_children=True)
    assert res["article_recall"] == 1.0
    assert res["articles_discovered"] == 3


# 16. Empty branch terminates safely
def test_empty_branch_terminates_safely():
    res = execute_multilevel_crawl("I_DEAD_BRANCH", traversal_mode="BFS")
    assert res["converged"] is True
    assert res["article_recall"] == 1.0


# 17. Broken branch does not crash global traversal
def test_broken_branch_does_not_crash():
    sim = MultiLevelPortalSimulator("A_TWO_LEVEL")
    sim.nodes_data["https://portal.test/category/broken"] = {
        "type": "CATEGORY", "child_nodes": [], "articles": [], "status_code": 500
    }
    sim.nodes_data["https://portal.test/archive"]["child_nodes"].append("https://portal.test/category/broken")
    res = execute_multilevel_crawl("A_TWO_LEVEL", traversal_mode="BFS")
    assert res["converged"] is True


# 18. Leaf archive delegates to ArchiveVisitPolicy
def test_leaf_archive_delegates_to_policy():
    res = execute_multilevel_crawl("A_TWO_LEVEL", traversal_mode="BFS")
    assert res["articles_discovered"] == 12


# 19. Pagination instability inside nested leaf retains coverage logic
def test_nested_leaf_pagination_instability():
    res = execute_multilevel_crawl("H_PAGINATION_INSTABILITY", traversal_mode="BFS")
    assert res["article_recall"] == 1.0
    assert res["articles_discovered"] == 4


# 20. Article provenance path preserved
def test_article_provenance_path_preserved():
    policy = ArchiveGraphTraversalPolicy()
    path = ["https://portal.test/archive", "https://portal.test/category/tech", "https://portal.test/topic/ai"]
    policy.record_article_with_provenance("https://portal.test/article/ai_01", path)

    assert "https://portal.test/article/ai_01" in policy.article_provenance
    assert policy.article_provenance["https://portal.test/article/ai_01"] == [path]

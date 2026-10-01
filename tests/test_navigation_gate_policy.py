"""
Unit tests for NavigationGatePolicy, MultiPageArticlePolicy, and GatedPortalSimulator.

Verifies all 20 required test criteria:
1. no-gate article passes normally;
2. age gate detected;
3. allowed gate resolves;
4. denied gate remains blocked;
5. session state reused;
6. session state scoped by origin;
7. redirect chain resolves;
8. redirect limit enforced;
9. gate loop detected;
10. unsupported gate terminates safely;
11. out-of-scope target rejected;
12. three-page logical article complete;
13. article-page loop detected;
14. missing page marks article incomplete;
15. duplicate page link deduplicated;
16. arbitrary URL-shaped components handled;
17. gate on second component resolved;
18. expired session handled;
19. page budget enforced;
20. logical article provenance preserved.
"""

import pytest
from src.navigation_gate_policy import NavigationGatePolicy
from src.multipage_article_policy import MultiPageArticlePolicy, LogicalArticle
from research.crawling.experiments.gated_portal_simulator import GatedPortalSimulator
from research.crawling.experiments.run_gated_navigation_experiment import execute_gated_crawl


# 1. No-gate article passes normally
def test_no_gate_article_passes_normally():
    sim = GatedPortalSimulator(scenario="A_NO_GATE")
    policy = NavigationGatePolicy()
    res = policy.resolve_url("https://portal.test/article/1", sim.fetch_url)
    assert res["status"] == "RESOLVED"
    assert res["gate_encountered"] is False


# 2. Age gate detected
def test_age_gate_detected():
    sim = GatedPortalSimulator(scenario="B_AGE_GATE")
    policy = NavigationGatePolicy()
    res = policy.resolve_url("https://portal.test/article/adult", sim.fetch_url)
    assert res["gate_encountered"] is True
    assert res["gate_type"] == "AGE_CONFIRMATION"


# 3. Allowed gate resolves
def test_allowed_gate_resolves():
    sim = GatedPortalSimulator(scenario="B_AGE_GATE")
    policy = NavigationGatePolicy(allowed_gate_actions={"AGE_CONFIRMATION": True})
    res = policy.resolve_url("https://portal.test/article/adult", sim.fetch_url)
    assert res["status"] == "RESOLVED"
    assert res["resolved_url"] == "https://portal.test/article/adult"


# 4. Denied gate remains blocked
def test_denied_gate_remains_blocked():
    sim = GatedPortalSimulator(scenario="B_AGE_GATE")
    policy = NavigationGatePolicy(allowed_gate_actions={"AGE_CONFIRMATION": False})
    res = policy.resolve_url("https://portal.test/article/adult", sim.fetch_url)
    assert res["status"] == "GATE_BLOCKED"
    assert res["resolved_url"] is None


# 5. Session state reused
def test_session_state_reused():
    sim = GatedPortalSimulator(scenario="C_SITEWIDE_AGE_SESSION")
    policy = NavigationGatePolicy()
    res1 = policy.resolve_url("https://portal.test/article/adult_01", sim.fetch_url)
    res2 = policy.resolve_url("https://portal.test/article/adult_02", sim.fetch_url)

    assert res1["status"] == "RESOLVED"
    assert res2["status"] == "RESOLVED"
    assert res2["session_reused"] is True


# 6. Session state scoped by origin
def test_session_state_scoped_by_origin():
    policy = NavigationGatePolicy()
    state1 = policy.get_session_state("site-a.test")
    state1["age_verified"] = True

    state2 = policy.get_session_state("site-b.test")
    assert state2.get("age_verified") is None


# 7. Redirect chain resolves
def test_redirect_chain_resolves():
    sim = GatedPortalSimulator(scenario="E_REDIRECT_CHAIN")
    policy = NavigationGatePolicy(allowed_gate_actions={"AGE_CONFIRMATION": True})
    res = policy.resolve_url("https://portal.test/entry", sim.fetch_url)
    assert res["status"] == "RESOLVED"
    assert res["resolved_url"] == "https://portal.test/article/destination"
    assert len(res["redirect_chain"]) >= 3


# 8. Redirect limit enforced
def test_redirect_limit_enforced():
    sim = GatedPortalSimulator(scenario="E_REDIRECT_CHAIN")
    policy = NavigationGatePolicy(max_redirect_hops=0)
    res = policy.resolve_url("https://portal.test/entry", sim.fetch_url)
    assert res["status"] == "REDIRECT_LIMIT_EXCEEDED"


# 9. Gate loop detected
def test_gate_loop_detected():
    sim = GatedPortalSimulator(scenario="F_GATE_LOOP")
    policy = NavigationGatePolicy(max_gate_hops=3)
    res = policy.resolve_url("https://portal.test/loop", sim.fetch_url)
    assert res["status"] == "GATE_LOOP_DETECTED"


# 10. Unsupported gate terminates safely
def test_unsupported_gate_terminates_safely():
    sim = GatedPortalSimulator(scenario="G_UNRESOLVABLE_GATE")
    policy = NavigationGatePolicy(allowed_gate_actions={"UNKNOWN_GATE": True})
    res = policy.resolve_url("https://portal.test/secure", sim.fetch_url)
    assert res["status"] == "UNSUPPORTED_GATE"


# 11. Out-of-scope target rejected
def test_out_of_scope_target_rejected():
    sim = GatedPortalSimulator(scenario="H_EXTERNAL_TARGET")
    policy = NavigationGatePolicy(allowed_domains={"portal.test"})
    res = policy.resolve_url("https://portal.test/ad", sim.fetch_url)
    assert res["status"] == "OUT_OF_SCOPE_GATE_TARGET"


# 12. Three-page logical article complete
def test_three_page_article_complete():
    sim = GatedPortalSimulator(scenario="I_THREE_PAGE_ARTICLE")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/article/multipage_01", sim.fetch_url)

    assert art.complete is True
    assert len(art.component_pages) == 3
    assert art.termination_reason == "LAST_PAGE_REACHED"


# 13. Article-page loop detected
def test_article_page_loop_detected():
    sim = GatedPortalSimulator(scenario="J_MULTIPAGE_LOOP")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/article/loop_01", sim.fetch_url)

    assert art.complete is False
    assert art.termination_reason == "ARTICLE_PAGE_LOOP_DETECTED"
    assert len(art.component_pages) == 2


# 14. Missing page marks article incomplete
def test_missing_page_marks_incomplete():
    sim = GatedPortalSimulator(scenario="K_MISSING_PAGE")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/article/broken_01", sim.fetch_url)

    assert art.complete is False
    assert "FETCH_FAILED" in art.termination_reason


# 15. Duplicate page link deduplicated
def test_duplicate_component_link():
    sim = GatedPortalSimulator(scenario="L_DUPLICATE_COMPONENT_LINK")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/article/dup_01", sim.fetch_url)

    assert art.complete is True
    assert len(art.component_pages) == 2


# 16. Arbitrary URL-shaped components handled
def test_arbitrary_url_shapes():
    sim = GatedPortalSimulator(scenario="M_ARBITRARY_SHAPES")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/story?id=123", sim.fetch_url)

    assert art.complete is True
    assert len(art.component_pages) == 3
    assert "https://portal.test/read/abc987" in art.component_pages


# 17. Gate on second component resolved
def test_gate_on_second_component():
    sim = GatedPortalSimulator(scenario="N_GATE_ON_PAGE_2")
    gate_policy = NavigationGatePolicy(allowed_gate_actions={"AGE_CONFIRMATION": True})
    policy = MultiPageArticlePolicy(gate_policy=gate_policy)
    art = policy.fetch_logical_article("https://portal.test/article/gated_p2_1", sim.fetch_url)

    assert art.complete is True
    assert len(art.component_pages) == 3


# 18. Expired session handled
def test_expired_session_handled():
    sim = GatedPortalSimulator(scenario="O_SESSION_EXPIRES")
    gate_policy = NavigationGatePolicy(allowed_gate_actions={"AGE_CONFIRMATION": True})
    policy = MultiPageArticlePolicy(gate_policy=gate_policy)
    art = policy.fetch_logical_article("https://portal.test/article/exp_01", sim.fetch_url)

    assert art.complete is True
    assert len(art.component_pages) == 3
    assert gate_policy.total_gate_interactions == 2  # Resolved twice!


# 19. Page budget enforced
def test_page_budget_enforced():
    sim = GatedPortalSimulator(scenario="I_THREE_PAGE_ARTICLE")
    policy = MultiPageArticlePolicy(max_component_pages_per_article=2)
    art = policy.fetch_logical_article("https://portal.test/article/multipage_01", sim.fetch_url)

    assert art.complete is False
    assert art.termination_reason == "PAGE_BUDGET_EXHAUSTED"
    assert len(art.component_pages) == 2


# 20. Logical article provenance preserved
def test_logical_article_provenance():
    sim = GatedPortalSimulator(scenario="I_THREE_PAGE_ARTICLE")
    policy = MultiPageArticlePolicy()
    art = policy.fetch_logical_article("https://portal.test/article/multipage_01", sim.fetch_url)
    art_dict = art.to_dict()

    assert art_dict["canonical_url"] == "https://portal.test/article/multipage_01"
    assert len(art_dict["gate_provenance"]) == 3

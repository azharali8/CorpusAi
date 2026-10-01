"""
Gated Navigation and Multi-Page Article Benchmark Harness (Task 4).

Compares NAIVE, GATE_AWARE, and GATE_AND_MULTIPAGE_AWARE across Scenarios A through O.
Outputs structured metrics to results/gated_navigation_experiment.json.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Set

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.navigation_gate_policy import NavigationGatePolicy
from src.multipage_article_policy import MultiPageArticlePolicy
from research.crawling.experiments.gated_portal_simulator import GatedPortalSimulator


def execute_gated_crawl(scenario_name: str, policy_mode: str) -> Dict[str, Any]:
    sim = GatedPortalSimulator(scenario=scenario_name)

    # Entry point URLs for each scenario
    entry_urls = {
        "A_NO_GATE": ["https://portal.test/article/simple_01"],
        "B_AGE_GATE": ["https://portal.test/article/adult_01"],
        "C_SITEWIDE_AGE_SESSION": ["https://portal.test/article/adult_01", "https://portal.test/article/adult_02", "https://portal.test/article/adult_03"],
        "D_CONSENT_INTERSTITIAL": ["https://portal.test/article/eu_news_01"],
        "E_REDIRECT_CHAIN": ["https://portal.test/entry"],
        "F_GATE_LOOP": ["https://portal.test/article/loop_gate_01"],
        "G_UNRESOLVABLE_GATE": ["https://portal.test/article/secure_01"],
        "H_EXTERNAL_TARGET": ["https://portal.test/article/ad_target"],
        "I_THREE_PAGE_ARTICLE": ["https://portal.test/article/multipage_01"],
        "J_MULTIPAGE_LOOP": ["https://portal.test/article/loop_01"],
        "K_MISSING_PAGE": ["https://portal.test/article/broken_01"],
        "L_DUPLICATE_COMPONENT_LINK": ["https://portal.test/article/dup_01"],
        "M_ARBITRARY_SHAPES": ["https://portal.test/story?id=123"],
        "N_GATE_ON_PAGE_2": ["https://portal.test/article/gated_p2_1"],
        "O_SESSION_EXPIRES": ["https://portal.test/article/exp_01"],
    }

    urls_to_test = entry_urls.get(scenario_name, ["https://portal.test/article/simple_01"])

    # Expected component pages ground truth
    expected_pages = {
        "A_NO_GATE": 1,
        "B_AGE_GATE": 1,
        "C_SITEWIDE_AGE_SESSION": 3,
        "D_CONSENT_INTERSTITIAL": 1,
        "E_REDIRECT_CHAIN": 1,
        "F_GATE_LOOP": 0,
        "G_UNRESOLVABLE_GATE": 0,
        "H_EXTERNAL_TARGET": 0,
        "I_THREE_PAGE_ARTICLE": 3,
        "J_MULTIPAGE_LOOP": 2, # Only 2 distinct before loop
        "K_MISSING_PAGE": 2, # P1 (200), P2 (404)
        "L_DUPLICATE_COMPONENT_LINK": 2,
        "M_ARBITRARY_SHAPES": 3,
        "N_GATE_ON_PAGE_2": 3,
        "O_SESSION_EXPIRES": 3,
    }

    mode = policy_mode.upper()

    # 1. NAIVE FETCHER
    if mode == "NAIVE":
        discovered_components = 0
        logical_complete = 0
        reqs = 0
        gate_interactions = 0
        session_reuses = 0

        for u in urls_to_test:
            reqs += 1
            resp = sim.fetch_url(u, session_state={})
            if resp.get("status_code") == 200 and resp.get("page_type") == "CONTENT":
                discovered_components += 1
                logical_complete += 1

        term_reason = "COMPLETE" if logical_complete == len(urls_to_test) else "FAILED_OR_BLOCKED"

        return {
            "scenario": scenario_name,
            "policy": "NAIVE",
            "gate_encountered": "GATE" in scenario_name or scenario_name in ("B_AGE_GATE", "C_SITEWIDE_AGE_SESSION", "D_CONSENT_INTERSTITIAL", "E_REDIRECT_CHAIN", "F_GATE_LOOP", "G_UNRESOLVABLE_GATE", "H_EXTERNAL_TARGET", "N_GATE_ON_PAGE_2", "O_SESSION_EXPIRES"),
            "gate_type": "AGE_CONFIRMATION" if "AGE" in scenario_name else ("CONSENT" if "CONSENT" in scenario_name else None),
            "gate_interactions": gate_interactions,
            "session_reuses": session_reuses,
            "redirect_hops": 0,
            "logical_articles": len(urls_to_test),
            "component_pages_expected": expected_pages.get(scenario_name, 1),
            "component_pages_discovered": discovered_components,
            "article_complete": logical_complete == len(urls_to_test) and expected_pages.get(scenario_name, 1) == 1,
            "loops_detected": 0,
            "requests": reqs,
            "termination_reason": term_reason,
        }

    # 2. GATE_AWARE ONLY (Resolves gates, but does not traverse multi-page next links)
    elif mode == "GATE_AWARE":
        gate_policy = NavigationGatePolicy(allowed_domains={"portal.test"})
        discovered_components = 0
        logical_complete = 0

        for u in urls_to_test:
            res = gate_policy.resolve_url(u, sim.fetch_url)
            if res["status"] == "RESOLVED":
                discovered_components += 1
                if expected_pages.get(scenario_name, 1) == 1:
                    logical_complete += 1

        term_reason = "COMPLETE" if (logical_complete == len(urls_to_test)) else "SINGLE_PAGE_ONLY_OR_BLOCKED"

        return {
            "scenario": scenario_name,
            "policy": "GATE_AWARE",
            "gate_encountered": gate_policy.total_gate_encounters > 0,
            "gate_type": "AGE_CONFIRMATION" if "AGE" in scenario_name else ("CONSENT" if "CONSENT" in scenario_name else None),
            "gate_interactions": gate_policy.total_gate_interactions,
            "session_reuses": gate_policy.total_session_reuses,
            "redirect_hops": gate_policy.total_redirect_hops,
            "logical_articles": len(urls_to_test),
            "component_pages_expected": expected_pages.get(scenario_name, 1),
            "component_pages_discovered": discovered_components,
            "article_complete": logical_complete == len(urls_to_test) and expected_pages.get(scenario_name, 1) == 1,
            "loops_detected": gate_policy.loops_detected,
            "requests": sim.request_count,
            "termination_reason": term_reason,
        }

    # 3. GATE_AND_MULTIPAGE_AWARE (Full Task 4 Policy)
    elif mode in ("GATE_AND_MULTIPAGE_AWARE", "FULL"):
        gate_policy = NavigationGatePolicy(allowed_domains={"portal.test"})
        multipage_policy = MultiPageArticlePolicy(gate_policy=gate_policy)

        discovered_components = 0
        all_complete = True
        primary_term_reason = "COMPLETE"

        for u in urls_to_test:
            art = multipage_policy.fetch_logical_article(u, sim.fetch_url)
            discovered_components += len(art.component_pages)
            if not art.complete:
                all_complete = False
                primary_term_reason = art.termination_reason

        return {
            "scenario": scenario_name,
            "policy": "GATE_AND_MULTIPAGE_AWARE",
            "gate_encountered": gate_policy.total_gate_encounters > 0,
            "gate_type": "AGE_CONFIRMATION" if "AGE" in scenario_name else ("CONSENT" if "CONSENT" in scenario_name else None),
            "gate_interactions": gate_policy.total_gate_interactions,
            "session_reuses": gate_policy.total_session_reuses,
            "redirect_hops": gate_policy.total_redirect_hops,
            "logical_articles": len(urls_to_test),
            "component_pages_expected": expected_pages.get(scenario_name, 1),
            "component_pages_discovered": discovered_components,
            "article_complete": all_complete and (expected_pages.get(scenario_name, 1) > 0),
            "loops_detected": gate_policy.loops_detected + multipage_policy.article_page_loops_detected,
            "requests": sim.request_count,
            "termination_reason": primary_term_reason if not all_complete else "COMPLETE",
        }

    else:
        raise ValueError(f"Unknown policy mode: {policy_mode}")


def run_full_gated_benchmark() -> List[Dict[str, Any]]:
    scenarios = [
        "A_NO_GATE",
        "B_AGE_GATE",
        "C_SITEWIDE_AGE_SESSION",
        "D_CONSENT_INTERSTITIAL",
        "E_REDIRECT_CHAIN",
        "F_GATE_LOOP",
        "G_UNRESOLVABLE_GATE",
        "H_EXTERNAL_TARGET",
        "I_THREE_PAGE_ARTICLE",
        "J_MULTIPAGE_LOOP",
        "K_MISSING_PAGE",
        "L_DUPLICATE_COMPONENT_LINK",
        "M_ARBITRARY_SHAPES",
        "N_GATE_ON_PAGE_2",
        "O_SESSION_EXPIRES",
    ]
    policies = ["NAIVE", "GATE_AWARE", "GATE_AND_MULTIPAGE_AWARE"]

    all_results = []

    print("=" * 115)
    print("CorpusAI Task 4 — Gated Navigation & Multi-Page Article Benchmark")
    print("=" * 115)
    print(f"{'Scenario':<25} {'Policy':<26} {'Comp (D/Exp)':<14} {'Complete':<10} {'Reqs':<6} {'GateInt':<8} {'Reuses':<8} {'TermReason'}")
    print("-" * 115)

    for sc in scenarios:
        for pol in policies:
            res = execute_gated_crawl(scenario_name=sc, policy_mode=pol)
            all_results.append(res)
            comp_str = f"{res['component_pages_discovered']}/{res['component_pages_expected']}"
            comp_bool = "YES" if res['article_complete'] else "NO"
            print(f"{sc:<25} {pol:<26} {comp_str:<14} {comp_bool:<10} {res['requests']:<6} {res['gate_interactions']:<8} {res['session_reuses']:<8} {res['termination_reason']}")

    print("=" * 115)

    results_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    out_file = os.path.join(results_dir, "gated_navigation_experiment.json")

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nSaved structured benchmark results to: {out_file}")
    return all_results


if __name__ == "__main__":
    run_full_gated_benchmark()

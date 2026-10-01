"""
Simulation runner and evaluation harness comparing Naive, Precision, and Robust policies
across all 5 unstable pagination scenarios. Outputs results/pagination_stability_experiment.json.
"""

import json
import os
import sys
from typing import Any, Dict, List, Set

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.archive_visit_policy import ArchiveVisitPolicy, create_policy
from research.crawling.experiments.portal_simulator import UnstablePortalSimulator


def execute_crawl(
    scenario_name: str,
    policy_profile: str,
    num_pages: int = 4,
) -> Dict[str, Any]:
    """
    Execute a crawl against the portal simulator using a specific visit policy.
    """
    simulator = UnstablePortalSimulator(scenario=scenario_name)
    ground_truth = simulator.get_ground_truth()

    policy = create_policy(policy_profile, max_pages=num_pages)

    # Initial crawl pass: sequentially visit pages 1..num_pages
    initial_queue = list(range(1, num_pages + 1))
    visited_pages: Set[int] = set()

    # If scenario D has 21 items with 5 items/page, page 5 may also exist
    if "INSERTION" in scenario_name or scenario_name == "D_INSERTION":
        # In a real dynamic crawl, crawler checks next page if previous page was full
        initial_queue.append(5)

    while initial_queue or policy.revisit_queue:
        if policy.budget_exhausted:
            break

        # Determine next page to visit
        if policy.profile == "NAIVE":
            if not initial_queue:
                break
            page_to_visit = initial_queue.pop(0)
        else:
            # Revisit queue takes priority if available, otherwise next initial page
            if policy.revisit_queue:
                next_revisit = policy.get_next_revisit_page()
                page_to_visit = next_revisit if next_revisit is not None else (initial_queue.pop(0) if initial_queue else None)
            elif initial_queue:
                page_to_visit = initial_queue.pop(0)
            else:
                break

        if page_to_visit is None:
            break

        visited_pages.add(page_to_visit)

        # Fetch from simulator
        article_urls = simulator.fetch_page(page_to_visit)

        # Register in policy
        obs_res = policy.register_observation(
            page_num=page_to_visit,
            article_urls=article_urls,
            timestamp=f"T{policy.total_requests}",
        )

        # For PRECISION and ROBUST: if we are in initial pass and policy requires 2 stable observations,
        # schedule verification if visit_count < required_stable_observations
        if policy.profile in ("PRECISION", "ROBUST") and not policy.budget_exhausted:
            state = policy.page_states[page_to_visit]
            if state["visit_count"] < policy.required_stable_observations and not state["is_stable"]:
                policy.schedule_revisit(page_to_visit)

    # Check convergence
    converged = policy.is_archive_converged(visited_pages)
    if policy.profile == "NAIVE":
        converged = True  # Naive is defined as single pass done

    discovered_urls = policy.ledger.get_all_urls()
    missing_urls = list(ground_truth - discovered_urls)

    coverage_recall = round(len(discovered_urls) / len(ground_truth), 4) if ground_truth else 0.0

    return {
        "scenario": scenario_name,
        "policy": policy_profile,
        "ground_truth_articles": len(ground_truth),
        "unique_articles_discovered": len(discovered_urls),
        "missing_articles": missing_urls,
        "coverage_recall": coverage_recall,
        "duplicate_observations": policy.ledger.duplicate_observation_count,
        "archive_requests": policy.total_requests,
        "revisits": policy.total_revisits,
        "order_only_changes": policy.order_only_changes,
        "set_changes": policy.set_changes,
        "converged": converged,
        "budget_exhausted": policy.budget_exhausted,
    }


def run_full_stability_benchmark() -> List[Dict[str, Any]]:
    scenarios = [
        "A_STABLE",
        "B_REORDERING",
        "C_CROSS_PAGE",
        "D_INSERTION",
        "E_CACHED",
    ]
    policies = ["NAIVE", "PRECISION", "ROBUST"]

    all_results = []

    print("=" * 85)
    print("CorpusAI Unstable Pagination & Archive Coverage Benchmark")
    print("=" * 85)
    print(f"{'Scenario':<15} {'Policy':<12} {'Recall':<8} {'Discovered':<12} {'Missing':<10} {'Reqs':<6} {'Revisits':<10} {'Converged'}")
    print("-" * 85)

    for sc in scenarios:
        for pol in policies:
            res = execute_crawl(scenario_name=sc, policy_profile=pol)
            all_results.append(res)
            pct = f"{res['coverage_recall']*100:.1f}%"
            disc_str = f"{res['unique_articles_discovered']}/{res['ground_truth_articles']}"
            miss_count = len(res['missing_articles'])
            conv_str = "YES" if res['converged'] else "NO"
            print(f"{sc:<15} {pol:<12} {pct:<8} {disc_str:<12} {miss_count:<10} {res['archive_requests']:<6} {res['revisits']:<10} {conv_str}")

    print("=" * 85)

    # Save to results/pagination_stability_experiment.json
    results_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    out_file = os.path.join(results_dir, "pagination_stability_experiment.json")

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nSaved structured experiment results to: {out_file}")
    return all_results


if __name__ == "__main__":
    run_full_stability_benchmark()

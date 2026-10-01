"""
Head Verification & Strong Crawl Convergence Experiment Harness (Task 2B).

Evaluates NAIVE, OLD_ROBUST, and NEW_ROBUST_HEAD_VERIFIED across Scenarios A through I.
Outputs results to results/head_verification_experiment.json.
"""

import json
import os
import sys
from typing import Any, Dict, List, Set

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.archive_visit_policy import ArchiveVisitPolicy, create_policy
from research.crawling.experiments.portal_simulator import UnstablePortalSimulator


def execute_head_verified_crawl(
    scenario_name: str,
    policy_profile: str,
    base_pages: int = 4,
) -> Dict[str, Any]:
    """
    Execute a full multi-phase crawl:
    Phase 1: INITIAL_TRAVERSAL
    Phase 2: HEAD_VERIFICATION / RECONCILIATION / FINAL_VERIFICATION
    """
    simulator = UnstablePortalSimulator(scenario=scenario_name)
    ground_truth = simulator.get_ground_truth()

    # Dynamic expansion support
    max_known_pages = base_pages
    policy = create_policy(policy_profile, max_pages=base_pages + 2)

    # Initial crawl frontier
    frontier: List[int] = list(range(1, base_pages + 1))
    visited_pages: Set[int] = set()

    # Phase 1: INITIAL_TRAVERSAL
    policy.current_phase = "INITIAL_TRAVERSAL"

    while frontier or policy.revisit_queue:
        if policy.budget_exhausted:
            break

        if policy.revisit_queue:
            page = policy.get_next_revisit_page()
            if page is None and frontier:
                page = frontier.pop(0)
        elif frontier:
            page = frontier.pop(0)
        else:
            break

        if page is None:
            break

        visited_pages.add(page)
        articles = simulator.fetch_page(page)

        # Detect new page creation: if page has full articles, check if page+1 exists
        if len(articles) == simulator.page_size and page >= max_known_pages and page < 10:
            next_p = page + 1
            if next_p not in frontier and next_p not in visited_pages:
                frontier.append(next_p)
                max_known_pages = next_p
                policy.log_event("NEW_ARCHIVE_PAGE_DISCOVERED", next_p)

        policy.register_observation(page_num=page, article_urls=articles, timestamp=f"T{policy.total_requests}")

        # In Precision/Robust initial pass, schedule 2nd observation if not stable
        if policy.profile in ("PRECISION", "ROBUST", "ROBUST_HEAD_VERIFIED") and not policy.budget_exhausted:
            st = policy.page_states[page]
            if st["visit_count"] < policy.required_stable_observations and not st["is_stable"]:
                policy.schedule_revisit(page)

    policy.initial_traversal_complete = True

    # Phase 2: HEAD_VERIFICATION & RECONCILIATION (if policy enabled and not naive)
    if policy.head_verification_enabled and not policy.budget_exhausted:
        for v_round in range(1, policy.max_verification_rounds + 1):
            policy.verification_round_count = v_round
            policy.current_phase = "HEAD_VERIFICATION"
            policy.log_event("HEAD_VERIFICATION_STARTED", policy.head_page_number, {"round": v_round})

            # Check head page
            head_articles = simulator.fetch_page(policy.head_page_number)
            head_obs = policy.register_observation(
                page_num=policy.head_page_number,
                article_urls=head_articles,
                timestamp=f"T{policy.total_requests}_V{v_round}",
            )

            # If head changed, enter RECONCILIATION phase
            if head_obs["change_type"] == "ARTICLE_SET_CHANGED":
                policy.current_phase = "RECONCILIATION"
                policy.log_event("RECONCILIATION_STARTED", policy.head_page_number)

                # Process boundary propagation until revisit queue clears
                while policy.revisit_queue and not policy.budget_exhausted:
                    p_revisit = policy.get_next_revisit_page()
                    if p_revisit is None:
                        break
                    p_articles = simulator.fetch_page(p_revisit)
                    policy.register_observation(
                        page_num=p_revisit,
                        article_urls=p_articles,
                        timestamp=f"T{policy.total_requests}_REC",
                    )
                    # Check if downstream page expanded
                    if len(p_articles) == simulator.page_size and p_revisit >= max_known_pages and p_revisit < 10:
                        next_p = p_revisit + 1
                        if next_p not in visited_pages:
                            visited_pages.add(next_p)
                            policy.schedule_revisit(next_p)
                            max_known_pages = next_p

            elif head_obs.get("head_verified"):
                # Head verified stable!
                policy.current_phase = "FINAL_VERIFICATION"
                policy.log_event("FINAL_VERIFICATION_PASSED", policy.head_page_number)
                break

    converged = policy.is_archive_converged(visited_pages)
    if policy.profile == "NAIVE":
        converged = True

    termination_reason = "CONVERGED" if converged else ("BUDGET_EXHAUSTED" if policy.budget_exhausted else "UNSTABLE_AT_TERMINATION")

    discovered_urls = policy.ledger.get_all_urls()
    missing_urls = list(ground_truth - discovered_urls)
    coverage_recall = round(len(discovered_urls) / len(ground_truth), 4) if ground_truth else 0.0

    return {
        "scenario": scenario_name,
        "policy": policy_profile,
        "ground_truth_articles": len(ground_truth),
        "discovered_articles": len(discovered_urls),
        "missing_articles": missing_urls,
        "coverage_recall": coverage_recall,
        "initial_requests": policy.initial_requests_count,
        "head_verification_requests": policy.head_verification_requests_count,
        "reconciliation_requests": policy.reconciliation_requests_count,
        "total_requests": policy.total_requests,
        "head_changes_detected": policy.head_changes_detected,
        "new_articles_detected_during_verification": policy.new_articles_during_verification,
        "propagation_depth": policy.propagation_depth,
        "converged": converged,
        "termination_reason": termination_reason,
    }


def run_head_verification_benchmark() -> List[Dict[str, Any]]:
    scenarios = [
        "A_STABLE",
        "B_REORDERING",
        "C_CROSS_PAGE",
        "D_INSERTION",
        "E_CACHED",
        "F_HEAD_INSERTION",
        "G_MULTIPLE_INSERTIONS",
        "H_CONTINUOUS_MUTATION",
        "I_EXPANDING_PAGE_COUNT",
    ]
    policies = ["NAIVE", "OLD_ROBUST", "NEW_ROBUST_HEAD_VERIFIED"]

    all_results = []

    print("=" * 105)
    print("CorpusAI Task 2B — Head-Page Verification & Strong Crawl Convergence Benchmark")
    print("=" * 105)
    print(f"{'Scenario':<24} {'Policy':<25} {'Recall':<8} {'Disc/GT':<10} {'Reqs (Init/Head/Rec)':<22} {'HeadChg':<8} {'Converged'}")
    print("-" * 105)

    for sc in scenarios:
        for pol in policies:
            res = execute_head_verified_crawl(scenario_name=sc, policy_profile=pol)
            all_results.append(res)
            pct = f"{res['coverage_recall']*100:.1f}%"
            disc_str = f"{res['discovered_articles']}/{res['ground_truth_articles']}"
            req_breakdown = f"{res['total_requests']} ({res['initial_requests']}/{res['head_verification_requests']}/{res['reconciliation_requests']})"
            conv_str = "YES" if res['converged'] else f"NO ({res['termination_reason']})"
            print(f"{sc:<24} {pol:<25} {pct:<8} {disc_str:<10} {req_breakdown:<22} {res['head_changes_detected']:<8} {conv_str}")

    print("=" * 105)

    # Save to results/head_verification_experiment.json
    results_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "results")
    os.makedirs(results_dir, exist_ok=True)
    out_file = os.path.join(results_dir, "head_verification_experiment.json")

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\nSaved structured benchmark results to: {out_file}")
    return all_results


if __name__ == "__main__":
    run_head_verification_benchmark()

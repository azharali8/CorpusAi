"""
Local Unstable Paginated Portal Simulator for CorpusAI Research Task 2 and 2B.

Supports 9 distinct deterministic scenarios:
- SCENARIO A: Stable Archive
- SCENARIO B: Same-Date Random Reordering (Order changes, set identical)
- SCENARIO C: Cross-Page Instability (Items swap/shift across pages N and N+1)
- SCENARIO D: Mid-Crawl New Article Insertion (Items shift downward after page 1 is visited)
- SCENARIO E: Cached/Frozen Response (Returns stale state before updating)
- SCENARIO F: Head Insertion After Page 1 (Reproduces item_99 insertion cleanly)
- SCENARIO G: Multiple Insertions During Crawl (X inserted after p1, Y inserted after p3)
- SCENARIO H: Continuously Changing Archive (New content appears continuously, exhausts budget)
- SCENARIO I: Insertion Creates New Last Page (Expands total page count from 4 to 5)
"""

import copy
from typing import Any, Dict, List, Optional, Set


class UnstablePortalSimulator:
    """
    Simulates a paginated portal with ground-truth articles under various stability scenarios.
    """

    def __init__(self, scenario: str = "A_STABLE", page_size: int = 5):
        self.scenario = scenario.upper()
        self.page_size = page_size
        self.request_count: int = 0
        self.page_visit_counts: Dict[int, int] = {}

        # Base 20 Canonical Ground-Truth Article URLs
        self.ground_truth_articles: List[str] = [
            f"https://portal.test/articles/item_{i:02d}" for i in range(1, 21)
        ]

        self._init_scenario_state()

    def _init_scenario_state(self):
        self.base_pages: Dict[int, List[str]] = {
            1: self.ground_truth_articles[0:5],
            2: self.ground_truth_articles[5:10],
            3: self.ground_truth_articles[10:15],
            4: self.ground_truth_articles[15:20],
        }

        # Track mutations
        self.inserted_article_url: Optional[str] = None
        self.extra_inserted_articles: List[str] = []

        if self.scenario in ("D_INSERTION", "D", "F_HEAD_INSERTION", "F"):
            self.inserted_article_url = "https://portal.test/articles/item_99_breaking_news"

        elif self.scenario in ("G_MULTIPLE_INSERTIONS", "G"):
            self.inserted_article_url = "https://portal.test/articles/item_98_mid_crawl_alpha"
            self.extra_inserted_articles = ["https://portal.test/articles/item_99_mid_crawl_beta"]

        elif self.scenario in ("H_CONTINUOUS_MUTATION", "H"):
            # Generates endless new articles
            self.inserted_article_url = "https://portal.test/articles/item_mutation_01"

        elif self.scenario in ("I_EXPANDING_PAGE_COUNT", "I"):
            self.inserted_article_url = "https://portal.test/articles/item_99_extra_expansion"

    def get_ground_truth(self) -> Set[str]:
        """
        Return the complete set of ground truth article URLs active in this scenario.
        """
        gt = set(self.ground_truth_articles)
        if self.inserted_article_url:
            gt.add(self.inserted_article_url)
        for extra in self.extra_inserted_articles:
            gt.add(extra)
        if self.scenario in ("H_CONTINUOUS_MUTATION", "H"):
            # Ground truth is dynamic
            for i in range(1, 20):
                gt.add(f"https://portal.test/articles/item_continuous_{i:02d}")
        return gt

    def fetch_page(self, page_num: int) -> List[str]:
        """
        Fetch article URLs for a specific page number, simulating the active scenario dynamics.
        """
        self.request_count += 1
        self.page_visit_counts[page_num] = self.page_visit_counts.get(page_num, 0) + 1
        visit_num = self.page_visit_counts[page_num]

        # -------------------------------------------------------------
        # SCENARIO A: Perfectly Stable
        # -------------------------------------------------------------
        if self.scenario in ("A_STABLE", "A"):
            return copy.deepcopy(self.base_pages.get(page_num, []))

        # -------------------------------------------------------------
        # SCENARIO B: Same-Date Random Reordering
        # -------------------------------------------------------------
        elif self.scenario in ("B_REORDERING", "B"):
            items = copy.deepcopy(self.base_pages.get(page_num, []))
            if not items:
                return []
            if visit_num % 2 == 0:
                return [items[2], items[0], items[4], items[1], items[3]]
            elif visit_num % 3 == 0:
                return list(reversed(items))
            else:
                return items

        # -------------------------------------------------------------
        # SCENARIO C: Cross-Page Instability
        # -------------------------------------------------------------
        elif self.scenario in ("C_CROSS_PAGE", "C"):
            if visit_num == 1:
                return copy.deepcopy(self.base_pages.get(page_num, []))
            else:
                if page_num == 1:
                    return [
                        "https://portal.test/articles/item_01",
                        "https://portal.test/articles/item_02",
                        "https://portal.test/articles/item_03",
                        "https://portal.test/articles/item_04",
                        "https://portal.test/articles/item_06",
                    ]
                elif page_num == 2:
                    return [
                        "https://portal.test/articles/item_05",
                        "https://portal.test/articles/item_07",
                        "https://portal.test/articles/item_08",
                        "https://portal.test/articles/item_09",
                        "https://portal.test/articles/item_10",
                    ]
                else:
                    return copy.deepcopy(self.base_pages.get(page_num, []))

        # -------------------------------------------------------------
        # SCENARIO D & F: Mid-Crawl Insertion (item_99 published after 1st total request)
        # -------------------------------------------------------------
        elif self.scenario in ("D_INSERTION", "D", "F_HEAD_INSERTION", "F"):
            if self.request_count == 1 and page_num == 1:
                return copy.deepcopy(self.base_pages[1])
            else:
                mutated_all = [self.inserted_article_url] + self.ground_truth_articles
                start_idx = (page_num - 1) * self.page_size
                end_idx = start_idx + self.page_size
                if start_idx >= len(mutated_all):
                    return []
                return mutated_all[start_idx:end_idx]

        # -------------------------------------------------------------
        # SCENARIO E: Cached / Frozen Stale Response
        # -------------------------------------------------------------
        elif self.scenario in ("E_CACHED", "E"):
            if page_num == 2:
                if visit_num < 3:
                    return [
                        "https://portal.test/articles/item_06",
                        "https://portal.test/articles/item_07",
                        "https://portal.test/articles/item_08",
                        "https://portal.test/articles/item_09",
                        "https://portal.test/articles/item_10",
                    ]
                else:
                    return [
                        "https://portal.test/articles/item_05",
                        "https://portal.test/articles/item_06",
                        "https://portal.test/articles/item_07",
                        "https://portal.test/articles/item_08",
                        "https://portal.test/articles/item_09",
                    ]
            else:
                return copy.deepcopy(self.base_pages.get(page_num, []))

        # -------------------------------------------------------------
        # SCENARIO G: Multiple Insertions During Crawl
        # Insertion 1 after request 1, Insertion 2 after request 4
        # -------------------------------------------------------------
        elif self.scenario in ("G_MULTIPLE_INSERTIONS", "G"):
            if self.request_count == 1:
                return copy.deepcopy(self.base_pages.get(page_num, []))
            elif self.request_count <= 4:
                # 1 insertion
                mutated_all = [self.inserted_article_url] + self.ground_truth_articles
            else:
                # 2 insertions
                mutated_all = self.extra_inserted_articles + [self.inserted_article_url] + self.ground_truth_articles

            start_idx = (page_num - 1) * self.page_size
            end_idx = start_idx + self.page_size
            if start_idx >= len(mutated_all):
                return []
            return mutated_all[start_idx:end_idx]

        # -------------------------------------------------------------
        # SCENARIO H: Continuously Mutating Archive
        # Every visit shifts articles by inserting a new unique article
        # -------------------------------------------------------------
        elif self.scenario in ("H_CONTINUOUS_MUTATION", "H"):
            active_items = [
                f"https://portal.test/articles/item_continuous_{self.request_count:02d}"
            ] + self.ground_truth_articles
            start_idx = (page_num - 1) * self.page_size
            end_idx = start_idx + self.page_size
            if start_idx >= len(active_items):
                return []
            return active_items[start_idx:end_idx]

        # -------------------------------------------------------------
        # SCENARIO I: Insertion Creates New Last Page (Page 5)
        # 20 items base -> 21 items total -> 5 pages needed
        # -------------------------------------------------------------
        elif self.scenario in ("I_EXPANDING_PAGE_COUNT", "I"):
            mutated_all = [self.inserted_article_url] + self.ground_truth_articles
            start_idx = (page_num - 1) * self.page_size
            end_idx = start_idx + self.page_size
            if start_idx >= len(mutated_all):
                return []
            return mutated_all[start_idx:end_idx]

        else:
            return copy.deepcopy(self.base_pages.get(page_num, []))

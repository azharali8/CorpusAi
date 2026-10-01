"""
Multi-Level Archive Simulator for CorpusAI Research Task 3.

Models complex hierarchical and directed graph archive structures across 10 deterministic scenarios:
- SCENARIO A: Simple Two-Level Archive (Root -> Category -> Pages)
- SCENARIO B: Three-Level Archive (Root -> Group -> Topic -> Pages)
- SCENARIO C: Shared Child / Multiple Parents (Category A & B -> Topic X)
- SCENARIO D: Navigation Cycle (A -> B -> A)
- SCENARIO E: Duplicate Articles Across Branches
- SCENARIO F: Irrelevant & External Links (login, ads, privacy, external domain)
- SCENARIO G: Deep Archive (6+ nested navigation levels)
- SCENARIO H: Branch with Pagination Instability (Integrates ArchiveVisitPolicy)
- SCENARIO I: Empty / Dead Branch
- SCENARIO J: Dynamic Child Discovery (New topic appears on reverification)
"""

import copy
from typing import Any, Dict, List, Optional, Set, Tuple


class MultiLevelPortalSimulator:
    """
    Simulates a multi-level web portal returning structured navigation and article links.
    """

    def __init__(self, scenario: str = "A_TWO_LEVEL"):
        self.scenario = scenario.upper()
        self.request_count: int = 0
        self.visit_counts: Dict[str, int] = {}

        # Ground truth stores: navigation nodes and unique articles
        self.ground_truth_nodes: Set[str] = set()
        self.ground_truth_articles: Set[str] = set()

        self._build_scenario_graph()

    def _build_scenario_graph(self):
        self.root_url = "https://portal.test/archive"
        self.nodes_data: Dict[str, Dict[str, Any]] = {}

        # -------------------------------------------------------------
        # SCENARIO A: Simple Two-Level Archive
        # -------------------------------------------------------------
        if self.scenario in ("A_TWO_LEVEL", "A"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/tech",
                        "https://portal.test/category/science",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/tech": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/category/tech?page=1",
                        "https://portal.test/category/tech?page=2",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/tech?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [f"https://portal.test/article/tech_{i:02d}" for i in range(1, 4)],
                    "is_leaf": True,
                },
                "https://portal.test/category/tech?page=2": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [f"https://portal.test/article/tech_{i:02d}" for i in range(4, 7)],
                    "is_leaf": True,
                },
                "https://portal.test/category/science": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/category/science?page=1",
                        "https://portal.test/category/science?page=2",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/science?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [f"https://portal.test/article/sci_{i:02d}" for i in range(1, 4)],
                    "is_leaf": True,
                },
                "https://portal.test/category/science?page=2": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [f"https://portal.test/article/sci_{i:02d}" for i in range(4, 7)],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO B: Three-Level Archive (Root -> Group -> Topic -> Pages)
        # -------------------------------------------------------------
        elif self.scenario in ("B_THREE_LEVEL", "B"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/group/science",
                        "https://portal.test/group/humanities",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/group/science": {
                    "type": "GROUP",
                    "child_nodes": [
                        "https://portal.test/topic/ai",
                        "https://portal.test/topic/robotics",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/ai": {
                    "type": "TOPIC",
                    "child_nodes": [
                        "https://portal.test/topic/ai?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/ai?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/ai_01", "https://portal.test/article/ai_02"],
                    "is_leaf": True,
                },
                "https://portal.test/topic/robotics": {
                    "type": "TOPIC",
                    "child_nodes": [
                        "https://portal.test/topic/robotics?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/robotics?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/rob_01", "https://portal.test/article/rob_02"],
                    "is_leaf": True,
                },
                "https://portal.test/group/humanities": {
                    "type": "GROUP",
                    "child_nodes": [
                        "https://portal.test/topic/linguistics",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/linguistics": {
                    "type": "TOPIC",
                    "child_nodes": [
                        "https://portal.test/topic/linguistics?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/linguistics?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/ling_01", "https://portal.test/article/ling_02"],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO C: Shared Child / Multiple Parents
        # -------------------------------------------------------------
        elif self.scenario in ("C_SHARED_CHILD", "C"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/engineering",
                        "https://portal.test/category/computer_science",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/engineering": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/topic/shared_robotics",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/computer_science": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/topic/shared_robotics",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/shared_robotics": {
                    "type": "TOPIC",
                    "child_nodes": [
                        "https://portal.test/topic/shared_robotics?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/shared_robotics?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/shared_rob_01", "https://portal.test/article/shared_rob_02"],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO D: Navigation Cycle (A -> B -> A)
        # -------------------------------------------------------------
        elif self.scenario in ("D_CYCLE", "D"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/alpha",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/alpha": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/topic/beta",
                        "https://portal.test/category/alpha?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/beta": {
                    "type": "TOPIC",
                    "child_nodes": [
                        "https://portal.test/category/alpha", # Cyclic back-link!
                        "https://portal.test/topic/beta?page=1",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/alpha?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/alpha_01"],
                    "is_leaf": True,
                },
                "https://portal.test/topic/beta?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/beta_01"],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO E: Duplicate Articles Across Branches
        # -------------------------------------------------------------
        elif self.scenario in ("E_DUPLICATE_ARTICLES", "E"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/branch1",
                        "https://portal.test/category/branch2",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/branch1": {
                    "type": "CATEGORY",
                    "child_nodes": ["https://portal.test/category/branch1?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/branch1?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [
                        "https://portal.test/article/cross_posted_item",
                        "https://portal.test/article/item_unique_1",
                    ],
                    "is_leaf": True,
                },
                "https://portal.test/category/branch2": {
                    "type": "CATEGORY",
                    "child_nodes": ["https://portal.test/category/branch2?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/branch2?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": [
                        "https://portal.test/article/cross_posted_item", # Duplicate
                        "https://portal.test/article/item_unique_2",
                    ],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO F: Irrelevant & External Links
        # -------------------------------------------------------------
        elif self.scenario in ("F_IRRELEVANT_LINKS", "F"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/news",
                        "https://portal.test/login",
                        "https://portal.test/privacy",
                        "https://portal.test/contact",
                        "https://external-ads.com/promo",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/news": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/category/news?page=1",
                        "https://portal.test/author/john-doe",
                        "https://portal.test/tag/politics",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/news?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/news_01", "https://portal.test/article/news_02"],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO G: Deep Archive (6 nested levels)
        # -------------------------------------------------------------
        elif self.scenario in ("G_DEEP_ARCHIVE", "G"):
            self.nodes_data = {
                "https://portal.test/archive": {"type": "ARCHIVE_ROOT", "child_nodes": ["https://portal.test/category/l1"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l1": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l2"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l2": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l3"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l3": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l4"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l4": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l5"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l5": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l6"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l6": {"type": "CATEGORY", "child_nodes": ["https://portal.test/category/l6?page=1"], "articles": [], "is_leaf": False},
                "https://portal.test/category/l6?page=1": {"type": "ARCHIVE_PAGE", "child_nodes": [], "articles": ["https://portal.test/article/deep_item_01"], "is_leaf": True},
            }

        # -------------------------------------------------------------
        # SCENARIO H: Branch with Pagination Instability
        # -------------------------------------------------------------
        elif self.scenario in ("H_PAGINATION_INSTABILITY", "H"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": ["https://portal.test/category/dynamic_branch"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/dynamic_branch": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/category/dynamic_branch?page=1",
                        "https://portal.test/category/dynamic_branch?page=2",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/dynamic_branch?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/dyn_01", "https://portal.test/article/dyn_02"],
                    "is_leaf": True,
                },
                "https://portal.test/category/dynamic_branch?page=2": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/dyn_03", "https://portal.test/article/dyn_04"],
                    "is_leaf": True,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO I: Empty / Dead Branch
        # -------------------------------------------------------------
        elif self.scenario in ("I_DEAD_BRANCH", "I"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/active",
                        "https://portal.test/category/empty_dead_end",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/active": {
                    "type": "CATEGORY",
                    "child_nodes": ["https://portal.test/category/active?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/active?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/active_01"],
                    "is_leaf": True,
                },
                "https://portal.test/category/empty_dead_end": {
                    "type": "CATEGORY",
                    "child_nodes": [],
                    "articles": [],
                    "is_leaf": False,
                },
            }

        # -------------------------------------------------------------
        # SCENARIO J: Dynamic Child Discovery (Topic C added on reverification)
        # -------------------------------------------------------------
        elif self.scenario in ("J_DYNAMIC_CHILDREN", "J"):
            self.nodes_data = {
                "https://portal.test/archive": {
                    "type": "ARCHIVE_ROOT",
                    "child_nodes": [
                        "https://portal.test/category/forum",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/category/forum": {
                    "type": "CATEGORY",
                    "child_nodes": [
                        "https://portal.test/topic/topic_a",
                        "https://portal.test/topic/topic_b",
                    ],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/topic_a": {
                    "type": "TOPIC",
                    "child_nodes": ["https://portal.test/topic/topic_a?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/topic_a?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/top_a_01"],
                    "is_leaf": True,
                },
                "https://portal.test/topic/topic_b": {
                    "type": "TOPIC",
                    "child_nodes": ["https://portal.test/topic/topic_b?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/topic_b?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/top_b_01"],
                    "is_leaf": True,
                },
                "https://portal.test/topic/topic_c": {
                    "type": "TOPIC",
                    "child_nodes": ["https://portal.test/topic/topic_c?page=1"],
                    "articles": [],
                    "is_leaf": False,
                },
                "https://portal.test/topic/topic_c?page=1": {
                    "type": "ARCHIVE_PAGE",
                    "child_nodes": [],
                    "articles": ["https://portal.test/article/top_c_breaking"],
                    "is_leaf": True,
                },
            }

        # Calculate ground truths
        for url, data in self.nodes_data.items():
            self.ground_truth_nodes.add(url)
            for art in data.get("articles", []):
                self.ground_truth_articles.add(art)

    def fetch_node(self, node_url: str) -> Dict[str, Any]:
        """
        Fetch node contents (child navigation links and article URLs).
        """
        self.request_count += 1
        self.visit_counts[node_url] = self.visit_counts.get(node_url, 0) + 1
        visit_num = self.visit_counts[node_url]

        data = copy.deepcopy(self.nodes_data.get(node_url))
        if not data:
            return {"type": "UNKNOWN", "child_nodes": [], "articles": [], "status_code": 404}

        # Dynamic child injection for Scenario J on visit_num > 1
        if self.scenario in ("J_DYNAMIC_CHILDREN", "J") and node_url == "https://portal.test/category/forum":
            if visit_num > 1:
                data["child_nodes"].append("https://portal.test/topic/topic_c")

        # Dynamic mid-crawl mutation for Scenario H
        if self.scenario in ("H_PAGINATION_INSTABILITY", "H") and node_url == "https://portal.test/category/dynamic_branch?page=1":
            if visit_num > 1:
                data["articles"] = ["https://portal.test/article/dyn_99_new", "https://portal.test/article/dyn_01"]

        data["status_code"] = 200
        return data

"""
Graph-based Multi-Level Archive Traversal Policy Engine for CorpusAI.

Models hierarchical and DAG archive structures (Category -> Subcategory -> Topic -> Paginated Leaves),
implements BFS and DFS navigation strategies, cycle detection, scope controls,
dynamic child verification, and integrates leaf archives directly with ArchiveVisitPolicy.
"""

import hashlib
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from src.archive_visit_policy import ArchiveVisitPolicy, ArticleLedger, normalize_article_url


def compute_node_child_fingerprints(child_urls: List[str]) -> Tuple[str, str]:
    """
    Compute ordered and unordered (set) fingerprints for child navigation links.
    """
    cleaned = [u.strip() for u in child_urls if u and isinstance(u, str)]
    ordered_str = "\n".join(cleaned)
    ordered_hash = hashlib.sha256(ordered_str.encode("utf-8")).hexdigest()

    set_str = "\n".join(sorted(list(set(cleaned))))
    set_hash = hashlib.sha256(set_str.encode("utf-8")).hexdigest()

    return ordered_hash, set_hash


class ArchiveGraphTraversalPolicy:
    """
    Manages navigation frontiers, graph traversal (BFS / DFS), cycle detection,
    path provenance, scope filtering, depth enforcement, and leaf archive handoff.
    """

    def __init__(
        self,
        strategy: str = "BFS",
        max_archive_depth: int = 5,
        max_navigation_nodes: int = 50,
        max_total_requests: int = 100,
        allowed_domains: Optional[Set[str]] = None,
        archive_patterns: Optional[List[str]] = None,
        article_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        max_visits_per_navigation_node: int = 2,
        verify_dynamic_children: bool = False,
        max_child_verification_rounds: int = 2,
    ):
        self.strategy = strategy.upper()
        self.max_archive_depth = max_archive_depth
        self.max_navigation_nodes = max_navigation_nodes
        self.max_total_requests = max_total_requests
        self.allowed_domains = {d.lower() for d in allowed_domains} if allowed_domains else set()
        self.archive_patterns = archive_patterns or ["/archive", "/category", "/group", "/topic", "/forum"]
        self.article_patterns = article_patterns or ["/article/", "/post/", "/item_", "/article_"]
        self.exclude_patterns = exclude_patterns or ["/login", "/privacy", "/contact", "/author", "/tag", "/ads", "/promo"]
        self.max_visits_per_navigation_node = max_visits_per_navigation_node
        self.verify_dynamic_children = verify_dynamic_children
        self.max_child_verification_rounds = max_child_verification_rounds

        # Frontier: List of dicts representing queued navigation nodes
        # {"url": str, "depth": int, "parent_path": List[str]}
        self.frontier: List[Dict[str, Any]] = []

        # Graph state: canonical_url -> node_dict
        self.nodes: Dict[str, Dict[str, Any]] = {}

        # Global article ledger & provenance: canonical_article_url -> set of navigation paths
        self.ledger = ArticleLedger()
        self.article_provenance: Dict[str, List[List[str]]] = {}

        # Event log
        self.events: List[Dict[str, Any]] = []

        # Metrics counters
        self.total_requests: int = 0
        self.visited_navigation_count: int = 0
        self.cycles_detected: int = 0
        self.out_of_scope_skipped: int = 0
        self.excluded_links_skipped: int = 0
        self.duplicate_navigation_avoided: int = 0
        self.depth_limited_count: int = 0
        self.max_frontier_size: int = 0
        self.child_order_changes: int = 0
        self.child_set_changes: int = 0
        self.dynamic_new_children_discovered: int = 0
        self.budget_exhausted: bool = False
        self.termination_reason: str = "IN_PROGRESS"

    def log_event(self, event_type: str, url: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "event_type": event_type,
            "url": url,
            "request_num": self.total_requests,
            "details": details or {},
        })

    def classify_link(self, raw_url: str) -> str:
        """
        Classify a discovered link into ARCHIVE_NODE, ARTICLE_NODE, EXCLUDED, or OUT_OF_SCOPE.
        """
        if not raw_url:
            return "EXCLUDED"

        parsed = urlparse(raw_url)
        domain = parsed.netloc.lower()

        # Scope check
        if self.allowed_domains and domain and domain not in self.allowed_domains:
            self.out_of_scope_skipped += 1
            self.log_event("OUT_OF_SCOPE_LINK_SKIPPED", raw_url, {"domain": domain})
            return "OUT_OF_SCOPE"

        path = parsed.path.lower()

        # Exclusion check
        for exc in self.exclude_patterns:
            if exc in path:
                self.excluded_links_skipped += 1
                self.log_event("EXCLUDED_LINK_SKIPPED", raw_url, {"pattern": exc})
                return "EXCLUDED"

        # Article check
        for art in self.article_patterns:
            if art in path:
                return "ARTICLE_NODE"

        # Archive navigation check
        for arc in self.archive_patterns:
            if arc in path:
                return "ARCHIVE_NODE"

        return "ARCHIVE_NODE"

    def enqueue_node(self, raw_url: str, parent_path: List[str], current_depth: int) -> bool:
        """
        Enqueue a candidate navigation node with cycle detection and depth limiting.
        """
        canonical = normalize_article_url(raw_url)
        if not canonical:
            return False

        # Domain registration if first root node
        if not self.allowed_domains:
            parsed = urlparse(canonical)
            if parsed.netloc:
                self.allowed_domains.add(parsed.netloc.lower())

        classification = self.classify_link(canonical)
        if classification != "ARCHIVE_NODE":
            return False

        # Cycle check against current ancestry path
        if canonical in parent_path:
            self.cycles_detected += 1
            self.log_event("NAVIGATION_CYCLE_DETECTED", canonical, {"ancestry": parent_path})
            return False

        # Depth check
        if current_depth > self.max_archive_depth:
            self.depth_limited_count += 1
            self.log_event("DEPTH_LIMIT_REACHED", canonical, {"depth": current_depth})
            return False

        # If already visited or enqueued
        if canonical in self.nodes:
            node = self.nodes[canonical]
            if parent_path and parent_path[-1] not in node["all_seen_parents"]:
                node["all_seen_parents"].append(parent_path[-1])
            if node["status"] in ("VISITED", "QUEUED"):
                self.duplicate_navigation_avoided += 1
                self.log_event("DUPLICATE_NAVIGATION_SKIPPED", canonical)
                return False

        # Register new node
        self.nodes[canonical] = {
            "canonical_url": canonical,
            "depth": current_depth,
            "first_seen_parent": parent_path[-1] if parent_path else None,
            "all_seen_parents": [parent_path[-1]] if parent_path else [],
            "visit_count": 0,
            "status": "QUEUED",
            "last_ordered_hash": None,
            "last_set_hash": None,
            "children": [],
            "is_leaf": False,
        }

        item = {
            "url": canonical,
            "depth": current_depth,
            "parent_path": list(parent_path),
        }

        if self.strategy == "DFS":
            # For DFS, stack LIFO
            self.frontier.insert(0, item)
        else:
            # For BFS, queue FIFO
            self.frontier.append(item)

        if len(self.frontier) > self.max_frontier_size:
            self.max_frontier_size = len(self.frontier)

        self.log_event("NAVIGATION_NODE_DISCOVERED", canonical, {"depth": current_depth})
        return True

    def get_next_node(self) -> Optional[Dict[str, Any]]:
        """
        Pop the next navigation node from the frontier.
        """
        if not self.frontier:
            return None
        return self.frontier.pop(0)

    def record_article_with_provenance(self, article_url: str, nav_path: List[str]) -> bool:
        """
        Record article in ledger and track all discovery navigation paths.
        """
        canonical = normalize_article_url(article_url)
        if not canonical:
            return False

        is_new = self.ledger.record_article(
            article_url=canonical,
            source_page=len(nav_path),
            visit_number=self.total_requests,
        )

        if canonical not in self.article_provenance:
            self.article_provenance[canonical] = []

        if nav_path not in self.article_provenance[canonical]:
            self.article_provenance[canonical].append(list(nav_path))

        self.log_event("ARTICLE_DISCOVERED", canonical, {"path": nav_path, "is_new": is_new})
        return is_new

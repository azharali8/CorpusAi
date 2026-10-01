"""
Multi-Page Article Policy and Logical Document Assembly Engine for CorpusAI.

Models articles whose content is split across multiple component pages (e.g. ?page=2, /continue, /part2),
tracks explicit next-page relationships, detects multi-page loops, handles missing pages,
and preserves logical article completeness and provenance.
"""

from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from src.archive_visit_policy import normalize_article_url
from src.navigation_gate_policy import NavigationGatePolicy


class LogicalArticle:
    """
    Represents a single canonical logical article spanning one or more component URLs.
    """

    def __init__(self, article_id: str, canonical_url: str):
        self.article_id = article_id
        self.canonical_url = canonical_url
        self.component_pages: List[str] = []
        self.complete: bool = False
        self.termination_reason: str = "IN_PROGRESS"
        self.gate_provenance: List[Dict[str, Any]] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "article_id": self.article_id,
            "canonical_url": self.canonical_url,
            "component_pages": list(self.component_pages),
            "component_count": len(self.component_pages),
            "complete": self.complete,
            "termination_reason": self.termination_reason,
            "gate_provenance": list(self.gate_provenance),
        }


class MultiPageArticlePolicy:
    """
    Discovers, traverses, and validates multi-page articles using explicit link relationships,
    enforces component page budgets, integrates with NavigationGatePolicy, and detects component loops.
    """

    def __init__(
        self,
        gate_policy: Optional[NavigationGatePolicy] = None,
        max_component_pages_per_article: int = 10,
        max_total_article_requests: int = 50,
    ):
        self.gate_policy = gate_policy or NavigationGatePolicy()
        self.max_component_pages_per_article = max_component_pages_per_article
        self.max_total_article_requests = max_total_article_requests

        # Logical articles store: canonical_url -> LogicalArticle
        self.logical_articles: Dict[str, LogicalArticle] = {}

        # Event log
        self.events: List[Dict[str, Any]] = []

        # Metrics
        self.total_requests: int = 0
        self.total_components_discovered: int = 0
        self.complete_articles_count: int = 0
        self.incomplete_articles_count: int = 0
        self.article_page_loops_detected: int = 0
        self.budget_exhausted: bool = False

    def log_event(self, event_type: str, url: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "event_type": event_type,
            "url": url,
            "request_num": self.total_requests,
            "details": details or {},
        })

    def fetch_logical_article(
        self,
        entry_url: str,
        fetcher_callback: Callable[[str, Dict[str, Any]], Dict[str, Any]],
        article_id: Optional[str] = None,
    ) -> LogicalArticle:
        """
        Traverse all component pages of a logical article starting from entry_url.

        Args:
            entry_url: Initial article URL discovered from archive.
            fetcher_callback: Callable(url, session_state) -> response_dict
            article_id: Optional identifier.

        Returns:
            LogicalArticle object with component pages and completion status.
        """
        canonical_entry = normalize_article_url(entry_url)
        art_id = article_id or canonical_entry

        article = LogicalArticle(article_id=art_id, canonical_url=canonical_entry)
        self.logical_articles[canonical_entry] = article

        current_url = canonical_entry
        visited_components: Set[str] = set()

        while current_url:
            if self.total_requests >= self.max_total_article_requests:
                self.budget_exhausted = True
                article.complete = False
                article.termination_reason = "ARTICLE_REQUEST_BUDGET_EXHAUSTED"
                self.incomplete_articles_count += 1
                self.log_event("ARTICLE_REQUEST_BUDGET_EXHAUSTED", current_url)
                break

            if len(article.component_pages) >= self.max_component_pages_per_article:
                article.complete = False
                article.termination_reason = "PAGE_BUDGET_EXHAUSTED"
                self.incomplete_articles_count += 1
                self.log_event("PAGE_BUDGET_EXHAUSTED", current_url, {"pages": len(article.component_pages)})
                break

            # 1. Resolve current page through NavigationGatePolicy
            self.total_requests += 1
            gate_resolution = self.gate_policy.resolve_url(current_url, fetcher_callback)
            article.gate_provenance.append(gate_resolution)

            if gate_resolution["status"] != "RESOLVED":
                article.complete = False
                article.termination_reason = f"GATE_RESOLUTION_FAILED_{gate_resolution['status']}"
                self.incomplete_articles_count += 1
                self.log_event("ARTICLE_COMPONENT_FETCH_FAILED", current_url, {"status": gate_resolution["status"]})
                break

            resolved_url = gate_resolution["resolved_url"]
            if not resolved_url:
                article.complete = False
                article.termination_reason = "RESOLVED_URL_EMPTY"
                self.incomplete_articles_count += 1
                break

            # 2. Check component loops
            if resolved_url in visited_components:
                self.article_page_loops_detected += 1
                article.complete = False
                article.termination_reason = "ARTICLE_PAGE_LOOP_DETECTED"
                self.incomplete_articles_count += 1
                self.log_event("ARTICLE_PAGE_LOOP_DETECTED", resolved_url)
                break

            visited_components.add(resolved_url)
            article.component_pages.append(resolved_url)
            self.total_components_discovered += 1
            self.log_event("ARTICLE_COMPONENT_DISCOVERED", resolved_url, {"component_index": len(article.component_pages)})

            # 3. Inspect page response for next-page link relationship
            final_resp = gate_resolution.get("final_response") or {}
            next_page_url = final_resp.get("next_page_url")

            if next_page_url:
                canonical_next = normalize_article_url(next_page_url)
                if canonical_next in visited_components:
                    self.article_page_loops_detected += 1
                    article.complete = False
                    article.termination_reason = "ARTICLE_PAGE_LOOP_DETECTED"
                    self.incomplete_articles_count += 1
                    self.log_event("ARTICLE_PAGE_LOOP_DETECTED", canonical_next)
                    break
                current_url = canonical_next
            else:
                # No further pages -> logical article complete!
                article.complete = True
                article.termination_reason = "LAST_PAGE_REACHED"
                self.complete_articles_count += 1
                self.log_event("LOGICAL_ARTICLE_COMPLETE", canonical_entry, {"total_pages": len(article.component_pages)})
                break

        return article

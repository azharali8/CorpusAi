"""
Incremental Archive Policy for CorpusAI Phase 6B.

Orchestrates incremental crawl traversal using the head-first principle:
1. Inspects the archive head first (sentinel verification).
2. If head is unchanged and prior state was complete, confirms empirical stability with minimal requests.
3. If head changed, discovers new articles and navigates down to the boundary until reaching known stable state.
4. Resumes from last potentially incomplete boundary if previous run was incomplete.
5. Employs budget controls, anomaly detection, and provenance tracking.
"""

from dataclasses import dataclass, field
import datetime
import hashlib
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from src.archive_visit_policy import (
    ArchiveVisitPolicy,
    ArticleLedger,
    compute_page_fingerprints,
    normalize_article_url,
)
from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.incremental_run import IncrementalRun
from src.run_comparison import IncrementalDiff, RunComparisonEngine


@dataclass
class IncrementalCrawlBudget:
    """Configurable budget parameters for an incremental crawl run."""
    max_archive_pages: int = 5
    max_articles: int = 25
    max_requests: int = 20
    max_visits_per_page: int = 3
    max_retries: int = 2
    concurrency: int = 1


class IncrementalArchivePolicy:
    """
    Coordinates incremental archive discovery, head sentinel checks, boundary reconciliation,
    and state collation against a previous PortalState.
    """

    def __init__(
        self,
        portal_id: str = "wordpress-news",
        budget: Optional[IncrementalCrawlBudget] = None,
        previous_state: Optional[PortalState] = None,
        head_page_url: str = "https://wordpress.org/news/all-posts/",
    ):
        self.portal_id = portal_id
        self.budget = budget or IncrementalCrawlBudget()
        self.previous_state = previous_state
        self.head_page_url = head_page_url

        self.requests_made: int = 0
        self.pages_visited: Dict[str, int] = {}
        self.current_articles: Dict[str, ArticleState] = {}
        self.current_pages: Dict[str, ArchivePageState] = {}
        self.archive_order: List[str] = []
        self.anomalies: List[str] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []

        self.head_changed: bool = False
        self.boundary_status: str = "NOT_EVALUATED"
        self.last_complete_page: Optional[str] = None
        self.last_incomplete_page: Optional[str] = previous_state.last_potentially_incomplete_page if previous_state else None
        self.budget_exhausted: bool = False

    def execute_incremental_crawl(
        self,
        page_fetcher: Callable[[str], Dict[str, Any]],  # returns {"article_urls": List[str], "next_page_url": Optional[str], "status_code": int, "html": str}
        article_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None, # optional: returns {"title": str, "content_hash": str, "raw_hash": str, "status": str}
    ) -> Tuple[PortalState, IncrementalRun, IncrementalDiff]:
        """
        Execute the incremental crawl algorithm with head sentinel verification and bounded boundary reconciliation.
        """
        start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run_id = f"run-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{self.portal_id[:6]}"

        current_url = self.head_page_url

        # Check if we should resume from previous incomplete page
        if self.previous_state and self.previous_state.last_potentially_incomplete_page:
            self.last_incomplete_page = self.previous_state.last_potentially_incomplete_page

        pages_to_visit = [current_url]
        pages_processed = 0

        while pages_to_visit and pages_processed < self.budget.max_archive_pages:
            if self.requests_made >= self.budget.max_requests:
                self.budget_exhausted = True
                self.last_incomplete_page = target_page_url if 'target_page_url' in locals() else pages_to_visit[0]
                self.warnings.append("Request budget exhausted during archive traversal.")
                break

            target_page_url = pages_to_visit.pop(0)
            self.requests_made += 1
            self.pages_visited[target_page_url] = self.pages_visited.get(target_page_url, 0) + 1

            try:
                fetch_res = page_fetcher(target_page_url)
            except Exception as e:
                self.errors.append(f"Fetch failed for page {target_page_url}: {str(e)}")
                self.current_pages[target_page_url] = ArchivePageState(
                    url=target_page_url,
                    ordered_fingerprint="",
                    unordered_fingerprint="",
                    article_urls=[],
                    crawl_status="FETCH_FAILED",
                )
                continue

            status_code = fetch_res.get("status_code", 200)
            if status_code == 403 or status_code == 401:
                self.anomalies.append("ACCESS_GATE_DETECTED")
                break
            elif status_code != 200:
                self.errors.append(f"HTTP {status_code} on {target_page_url}")
                continue

            raw_article_urls = fetch_res.get("article_urls", [])
            next_url = fetch_res.get("next_page_url")

            # Normalization
            normalized_urls = [normalize_article_url(u) for u in raw_article_urls if u]
            ordered_hash, set_hash = compute_page_fingerprints(normalized_urls)

            is_head = (target_page_url == self.head_page_url)

            # Anomaly check: empty head page
            if is_head and len(normalized_urls) == 0:
                self.anomalies.append("ARCHIVE_EMPTY_UNEXPECTEDLY")

            # Check head sentinel
            if is_head and self.previous_state and self.previous_state.head_fingerprint:
                if set_hash != self.previous_state.head_fingerprint:
                    self.head_changed = True
                else:
                    self.head_changed = False

            # Check pagination structure change
            if self.previous_state:
                prev_p = self.previous_state.archive_pages.get(target_page_url)
                if prev_p and prev_p.next_page_url != next_url:
                    # Pagination changed (e.g. new page appeared where next_url was previously None)
                    pass

            # Create/update ArchivePageState
            page_state = ArchivePageState(
                url=target_page_url,
                ordered_fingerprint=ordered_hash,
                unordered_fingerprint=set_hash,
                article_urls=normalized_urls,
                next_page_url=next_url,
                page_number=pages_processed + 1,
                crawl_status="COMPLETE",
            )
            self.current_pages[target_page_url] = page_state
            self.last_complete_page = target_page_url
            pages_processed += 1

            # Register article URLs into current state
            for u in normalized_urls:
                if u not in self.archive_order:
                    self.archive_order.append(u)

                if u not in self.current_articles:
                    # Check if present in previous state
                    prev_art = self.previous_state.articles.get(u) if self.previous_state else None
                    if prev_art:
                        art_state = ArticleState(
                            canonical_url=u,
                            first_seen_run_id=prev_art.first_seen_run_id,
                            last_seen_run_id=run_id,
                            title=prev_art.title,
                            published_at=prev_art.published_at,
                            author=prev_art.author,
                            content_hash=prev_art.content_hash,
                            raw_hash=prev_art.raw_hash,
                            status="UNCHANGED",
                            seen_on_pages=[target_page_url],
                            representation_type=prev_art.representation_type,
                            metadata=prev_art.metadata,
                        )
                    else:
                        art_state = ArticleState(
                            canonical_url=u,
                            first_seen_run_id=run_id,
                            last_seen_run_id=run_id,
                            status="NEW",
                            seen_on_pages=[target_page_url],
                        )

                    # If article fetcher is provided, fetch article details
                    if article_fetcher:
                        if self.requests_made < self.budget.max_requests and len(self.current_articles) < self.budget.max_articles:
                            self.requests_made += 1
                            try:
                                art_data = article_fetcher(u)
                                art_state.title = art_data.get("title", art_state.title)
                                art_state.published_at = art_data.get("published_at", art_state.published_at)
                                art_state.author = art_data.get("author", art_state.author)
                                art_state.content_hash = art_data.get("content_hash", art_state.content_hash)
                                art_state.raw_hash = art_data.get("raw_hash", art_state.raw_hash)
                                art_state.last_captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                            except Exception as e:
                                art_state.status = "FETCH_FAILED"
                                self.warnings.append(f"Article fetch failed for {u}: {str(e)}")

                    self.current_articles[u] = art_state
                else:
                    if target_page_url not in self.current_articles[u].seen_on_pages:
                        self.current_articles[u].seen_on_pages.append(target_page_url)

            # Stopping Condition for Incremental Runs:
            # If head is unchanged and previous run was complete, we can stop immediately (Empirical Stability)
            # BUT only if the page's next_url has not changed!
            prev_head_page = self.previous_state.archive_pages.get(self.head_page_url) if self.previous_state else None
            pagination_unchanged = (prev_head_page and prev_head_page.next_page_url == next_url)

            if is_head and not self.head_changed and self.previous_state and pagination_unchanged:
                if not self.previous_state.last_potentially_incomplete_page:
                    self.boundary_status = "BOUNDARY_RECONCILED"
                    break

            # If we reached an archive page that matches previous state exactly and pagination unchanged
            if not is_head and self.previous_state:
                prev_p = self.previous_state.archive_pages.get(target_page_url)
                if prev_p and prev_p.unordered_fingerprint == set_hash and prev_p.next_page_url == next_url:
                    self.boundary_status = "BOUNDARY_RECONCILED"
                    break

            # Add next page to queue if present
            if next_url and next_url not in self.pages_visited and next_url not in pages_to_visit:
                pages_to_visit.append(next_url)

        # If loop exited due to budget while pages remain
        if pages_to_visit and self.requests_made >= self.budget.max_requests:
            self.budget_exhausted = True
            self.last_incomplete_page = pages_to_visit[0]

        # Build PortalState snapshot
        head_page_state = self.current_pages.get(self.head_page_url)
        head_fp = head_page_state.unordered_fingerprint if head_page_state else None

        portal_state = PortalState(
            portal_id=self.portal_id,
            run_id=run_id,
            archive_pages=self.current_pages,
            articles=self.current_articles,
            archive_order=self.archive_order,
            head_fingerprint=head_fp,
            last_complete_archive_page=self.last_complete_page,
            last_potentially_incomplete_page=self.last_incomplete_page if self.budget_exhausted else None,
        )

        # Compute IncrementalDiff
        diff = RunComparisonEngine.compare_states(self.previous_state, portal_state)

        # Determine convergence status
        if self.budget_exhausted:
            convergence = "BUDGET_EXHAUSTED"
        elif self.anomalies:
            convergence = "MANUAL_REVIEW_REQUIRED"
        elif len(self.errors) > 0 and len(self.current_articles) == 0:
            convergence = "FAILED"
        else:
            convergence = "CONVERGED"

        # Build IncrementalRun
        inc_run = IncrementalRun(
            run_id=run_id,
            target_portal=self.portal_id,
            seed_urls=[self.head_page_url],
            previous_run_id=self.previous_state.run_id if self.previous_state else None,
            mode="INCREMENTAL" if self.previous_state else "BASELINE",
            started_at=start_time,
            completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            archive_page_count=len(self.current_pages),
            pages_revisited=sum(max(0, v - 1) for v in self.pages_visited.values()),
            article_count=len(self.current_articles),
            new_article_count=len(diff.new_articles),
            unchanged_article_count=len(diff.unchanged_articles),
            changed_article_count=len(diff.changed_articles),
            metadata_changed_article_count=len(diff.metadata_changed_articles),
            missing_article_count=len(diff.missing_candidates),
            reordered_article_count=len(diff.reordered_articles),
            fetch_failure_count=len(diff.fetch_failures),
            requests_made=self.requests_made,
            convergence_status=convergence,
            boundary_status=self.boundary_status,
            head_changed=self.head_changed,
            anomalies_detected=self.anomalies + diff.anomalies,
            errors=self.errors,
            warnings=self.warnings,
            manual_review_status="MANUAL_REVIEW_REQUIRED" if (self.anomalies or diff.anomalies) else "PENDING",
        )

        return portal_state, inc_run, diff

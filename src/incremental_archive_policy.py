"""
Incremental Archive Policy for CorpusAI Phase 6B.

Orchestrates incremental crawl traversal using the head-first principle:
1. Inspects the archive head first (sentinel verification).
2. If head is unchanged and prior state was complete, confirms empirical stability with minimal requests.
3. If head changed, discovers new articles and navigates down to the boundary until reaching known stable state.
4. Resumes from last potentially incomplete boundary if previous run was incomplete.
5. Employs budget controls, anomaly detection, and provenance tracking.
"""

from dataclasses import dataclass
import datetime
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.archive_visit_policy import (
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
        run_id: Optional[str] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.portal_id = portal_id
        self.budget = budget or IncrementalCrawlBudget()
        self.previous_state = previous_state
        self.head_page_url = head_page_url
        self.explicit_run_id = run_id
        self.sleep_fn = sleep_fn if sleep_fn is not None else time.sleep

        self.requests_made: int = 0
        self.pages_visited: Dict[str, int] = {}
        self.current_articles: Dict[str, ArticleState] = {}
        self.current_pages: Dict[str, ArchivePageState] = {}
        self.archive_order: List[str] = []
        self.anomalies: List[str] = []
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.retry_log: List[Dict[str, Any]] = []

        self.head_changed: bool = False
        self.boundary_status: str = "NOT_EVALUATED"
        self.last_complete_page: Optional[str] = None
        self.last_incomplete_page: Optional[str] = previous_state.last_potentially_incomplete_page if previous_state else None
        self.budget_exhausted: bool = False

    def _fetch_with_retry(
        self,
        fetcher_callable: Callable[[str], Any],
        url: str,
        budget_key: str = "request",
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[int]]:
        """
        Execute fetcher_callable with bounded intra-run retry policy on transient errors.

        Rules:
        - max_retries: number of additional attempts after the first.
        - Retry on network/connection exceptions and HTTP 5xx status codes.
        - Do NOT retry: HTTP 404/410, 401/403, 200.
        - HTTP 429: retry once with delay min(Retry-After, 60s) or 30s. Count against max_retries.
        - Delay between retries: 2 ** attempt_number seconds (exponential: 2s, 4s, ...).
        - Request budget: each retry attempt counts as one request toward self.budget.max_requests.
        - Records retry events in self.retry_log as {"url": ..., "attempt": ..., "reason": ..., "delay_seconds": ...}.
        """
        max_attempts = 1 + max(0, self.budget.max_retries)
        retried_429 = False

        for attempt in range(1, max_attempts + 1):
            if self.requests_made >= self.budget.max_requests:
                self.budget_exhausted = True
                self.warnings.append(f"Request budget exhausted ({self.budget.max_requests}) on {url}.")
                return None, "Request budget exhausted", None

            self.requests_made += 1
            res: Optional[Dict[str, Any]] = None
            err_msg: Optional[str] = None
            status_code: Optional[int] = None
            is_retriable = False
            delay = 0.0

            try:
                res = fetcher_callable(url)
                if isinstance(res, dict):
                    # Check if this fetch was resolved purely via local state reuse without network request
                    if res.get("content_capture_status") == "REUSED":
                        self.requests_made -= 1

                    status_code = res.get("status_code", 200)
                    if status_code in (401, 403):
                        return res, f"HTTP {status_code}", status_code
                    elif status_code in (404, 410):
                        return res, f"HTTP {status_code}", status_code
                    elif 200 <= status_code < 300:
                        return res, None, status_code
                    elif status_code == 429:
                        if not retried_429:
                            is_retriable = True
                            err_msg = "HTTP 429"
                            headers = res.get("headers", {})
                            retry_after = (
                                headers.get("Retry-After")
                                or headers.get("retry-after")
                                or res.get("retry_after")
                            )
                            delay = 30.0
                            if retry_after is not None:
                                try:
                                    delay = min(float(retry_after), 60.0)
                                except (ValueError, TypeError):
                                    delay = 30.0
                            retried_429 = True
                        else:
                            return res, "HTTP 429 (already retried)", 429
                    elif 500 <= status_code < 600:
                        is_retriable = True
                        err_msg = f"HTTP {status_code}"
                        delay = float(2 ** attempt)
                    else:
                        return res, f"HTTP {status_code}", status_code
                else:
                    return res, None, 200

            except Exception as e:
                code = getattr(e, "code", None)
                if code in (401, 403):
                    return None, f"HTTP {code}: {str(e)}", code
                elif code in (404, 410):
                    return None, f"HTTP {code}: {str(e)}", code
                elif code == 429:
                    if not retried_429:
                        is_retriable = True
                        err_msg = f"HTTP 429: {str(e)}"
                        headers = getattr(e, "headers", {})
                        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
                        delay = 30.0
                        if retry_after is not None:
                            try:
                                delay = min(float(retry_after), 60.0)
                            except (ValueError, TypeError):
                                delay = 30.0
                        retried_429 = True
                    else:
                        return None, f"HTTP 429: {str(e)} (already retried)", 429
                elif code is not None and 500 <= code < 600:
                    is_retriable = True
                    err_msg = f"HTTP {code}: {str(e)}"
                    delay = float(2 ** attempt)
                elif code is not None:
                    return None, f"HTTP {code}: {str(e)}", code
                else:
                    is_retriable = True
                    err_msg = str(e)
                    delay = float(2 ** attempt)

            if is_retriable:
                can_retry_more = (attempt < max_attempts) and (self.requests_made < self.budget.max_requests)
                if can_retry_more:
                    self.retry_log.append({
                        "url": url,
                        "attempt": attempt,
                        "reason": err_msg or "Retriable failure",
                        "delay_seconds": delay,
                    })
                    self.sleep_fn(delay)
                    continue
                else:
                    self.retry_log.append({
                        "url": url,
                        "attempt": attempt,
                        "reason": err_msg or "Retries exhausted",
                        "delay_seconds": 0.0,
                    })
                    return res, err_msg, status_code

        return res, err_msg, status_code

    def execute_incremental_crawl(
        self,
        page_fetcher: Callable[[str], Dict[str, Any]],  # returns {"article_urls": List[str], "next_page_url": Optional[str], "status_code": int, "html": str}
        article_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None, # optional: returns {"title": str, "content_hash": str, "raw_hash": str, "status": str}
        confirmation_probe: Optional[Callable[[str], Dict[str, Any]]] = None, # optional: probe for missing candidate verification
    ) -> Tuple[PortalState, IncrementalRun, IncrementalDiff]:
        """
        Execute the incremental crawl algorithm with head sentinel verification and bounded boundary reconciliation.
        """
        start_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _ts = datetime.datetime.now(datetime.timezone.utc)
        run_id = self.explicit_run_id or f"run-{_ts.strftime('%Y%m%dT%H%M%S')}{_ts.microsecond:06d}Z-{self.portal_id[:6]}"

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
            self.pages_visited[target_page_url] = self.pages_visited.get(target_page_url, 0) + 1

            fetch_res, err, status_code = self._fetch_with_retry(page_fetcher, target_page_url, budget_key="page")

            if status_code in (401, 403):
                self.anomalies.append("ACCESS_GATE_DETECTED")
                break

            if fetch_res is None or (status_code is not None and status_code != 200):
                self.errors.append(err or f"Fetch failed for page {target_page_url}")
                self.current_pages[target_page_url] = ArchivePageState(
                    url=target_page_url,
                    ordered_fingerprint="",
                    unordered_fingerprint="",
                    article_urls=[],
                    crawl_status="FETCH_FAILED",
                )
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
                            content_capture_status="REUSED" if prev_art.content_capture_status in ("CAPTURED", "REUSED") else prev_art.content_capture_status,
                            seen_on_pages=[target_page_url],
                            representation_type=prev_art.representation_type,
                            metadata=prev_art.metadata,
                            last_captured_at=prev_art.last_captured_at,
                            warc_file=prev_art.warc_file,
                            warc_record_id=prev_art.warc_record_id,
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
                            art_data, art_err, art_status = self._fetch_with_retry(article_fetcher, u, budget_key="article")
                            if art_status in (401, 403):
                                self.anomalies.append("ACCESS_GATE_DETECTED")
                            elif art_status in (404, 410):
                                art_state.status = "REMOVED"
                                art_state.content_capture_status = "REMOVED"
                            elif art_data is not None and (art_status == 200 or art_status is None):
                                art_state.title = art_data.get("title", art_state.title)
                                art_state.published_at = art_data.get("published_at", art_state.published_at)
                                art_state.author = art_data.get("author", art_state.author)
                                new_content_hash = art_data.get("content_hash", art_state.content_hash)
                                if prev_art and prev_art.content_hash and new_content_hash and prev_art.content_hash != new_content_hash:
                                    art_state.status = "CONTENT_CHANGED"
                                elif prev_art:
                                    art_state.status = "UNCHANGED"
                                art_state.content_hash = new_content_hash
                                art_state.raw_hash = art_data.get("raw_hash", art_state.raw_hash)
                                art_state.last_captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                art_state.warc_file = art_data.get("warc_file", art_state.warc_file)
                                art_state.warc_record_id = art_data.get("warc_record_id", art_state.warc_record_id)
                                if "content_capture_status" in art_data:
                                    art_state.content_capture_status = art_data["content_capture_status"]
                                elif art_state.content_hash:
                                    art_state.content_capture_status = "CAPTURED"
                            else:
                                art_state.status = "FETCH_FAILED"
                                art_state.content_capture_status = "FAILED"
                                self.warnings.append(f"Article fetch failed for {u}: {art_err or 'unknown'}")

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

        # Optional confirmation probe: if an article was present in previous complete crawl
        # but missing in current complete crawl, probe it to confirm removal vs transient error
        if confirmation_probe and self.previous_state and not self.budget_exhausted:
            for unobserved_u in set(self.previous_state.articles.keys()) - set(self.current_articles.keys()):
                if self.requests_made < self.budget.max_requests:
                    self.requests_made += 1
                    try:
                        probe_res = confirmation_probe(unobserved_u)
                        p_status = probe_res.get("status_code", 404)
                        if p_status in (404, 410):
                            # Confirmed removed: register as REMOVED state transition
                            prev_a = self.previous_state.articles[unobserved_u]
                            self.current_articles[unobserved_u] = ArticleState(
                                canonical_url=unobserved_u,
                                first_seen_run_id=prev_a.first_seen_run_id,
                                last_seen_run_id=run_id,
                                title=prev_a.title,
                                content_hash=prev_a.content_hash,
                                status="REMOVED",
                                content_capture_status="REMOVED",
                                warc_file=prev_a.warc_file,
                                warc_record_id=prev_a.warc_record_id,
                            )
                    except Exception as e:
                        self.warnings.append(f"Confirmation probe error for {unobserved_u}: {str(e)}")

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
            retry_log=list(self.retry_log),
            manual_review_status="MANUAL_REVIEW_REQUIRED" if (self.anomalies or diff.anomalies) else "PENDING",
        )

        return portal_state, inc_run, diff

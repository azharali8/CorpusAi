"""
Run Comparison and Incremental Diff Engine for CorpusAI Phase 6B.

Compares two PortalState snapshots (previous vs current) to compute fine-grained
classification of changes: new articles, unchanged articles, content changes,
metadata changes, missing candidates, reorderings, page changes, and anomalies.
"""

from dataclasses import asdict, dataclass, field
import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple

from src.portal_state import ArticleState, ArchivePageState, PortalState


@dataclass
class IncrementalDiff:
    """Detailed structural difference between a previous PortalState and a current PortalState."""
    previous_run_id: Optional[str]
    current_run_id: str

    # Article classifications: lists of canonical URLs
    new_articles: List[str] = field(default_factory=list)
    unchanged_articles: List[str] = field(default_factory=list)
    changed_articles: List[str] = field(default_factory=list)            # Body/content text changed
    metadata_changed_articles: List[str] = field(default_factory=list)   # Title/Date/Author changed, content identical
    missing_candidates: List[str] = field(default_factory=list)          # Confirmed missing across full traversal
    not_observed_in_scope: List[str] = field(default_factory=list)       # In previous state, not observed in current bounded scope (e.g. shifted to older pages)
    reordered_articles: List[str] = field(default_factory=list)          # Present in both, but position in archive changed
    fetch_failures: List[str] = field(default_factory=list)

    # Archive page classifications: url -> change_type
    archive_page_changes: Dict[str, str] = field(default_factory=dict)
    # UNCHANGED, ORDER_CHANGED, MEMBERSHIP_CHANGED, PAGINATION_CHANGED, NEW_PAGE, MISSING_PAGE, FETCH_FAILED

    # High-level metrics
    head_changed: bool = False
    pagination_changed: bool = False
    anomalies: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manual_review_required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunComparisonEngine:
    """
    Compares previous PortalState and current crawl observations to classify changes.
    """

    @staticmethod
    def compare_states(
        previous_state: Optional[PortalState],
        current_state: PortalState,
    ) -> IncrementalDiff:
        """
        Compute IncrementalDiff between previous PortalState and current PortalState.
        """
        diff = IncrementalDiff(
            previous_run_id=previous_state.run_id if previous_state else None,
            current_run_id=current_state.run_id,
        )

        if not previous_state:
            # Baseline run: everything in current is NEW
            diff.new_articles = list(current_state.articles.keys())
            for page_url, page_state in current_state.archive_pages.items():
                diff.archive_page_changes[page_url] = "NEW_PAGE"
            return diff

        prev_articles = previous_state.articles
        curr_articles = current_state.articles

        prev_urls = set(prev_articles.keys())
        curr_urls = set(curr_articles.keys())

        # 1. New articles
        new_urls = curr_urls - prev_urls
        diff.new_articles = sorted(list(new_urls))

        # 2. Articles in previous but not seen in current
        # If current run covered fewer archive pages than previous or is a bounded run,
        # unobserved articles have simply moved past the inspected boundary.
        unobserved_urls = prev_urls - curr_urls
        is_partial_scope = (len(current_state.archive_pages) < len(previous_state.archive_pages)) or (len(current_state.archive_pages) < 5)
        if is_partial_scope:
            diff.not_observed_in_scope = sorted(list(unobserved_urls))
            diff.missing_candidates = []
        else:
            diff.missing_candidates = sorted(list(unobserved_urls))
            diff.not_observed_in_scope = sorted(list(unobserved_urls))

        # 3. Intersecting articles: check content vs metadata changes vs unchanged
        common_urls = curr_urls & prev_urls
        for url in sorted(list(common_urls)):
            p_art = prev_articles[url]
            c_art = curr_articles[url]

            # Compare content body hashes if available
            p_content_hash = p_art.content_hash
            c_content_hash = c_art.content_hash

            # Compare metadata fingerprints (title, date, author)
            p_meta = (p_art.title or "", p_art.published_at or "", p_art.author or "")
            c_meta = (c_art.title or "", c_art.published_at or "", c_art.author or "")

            if c_art.status == "FETCH_FAILED":
                diff.fetch_failures.append(url)
            elif p_content_hash and c_content_hash and p_content_hash != c_content_hash:
                diff.changed_articles.append(url)
                c_art.status = "CONTENT_CHANGED"
            elif p_meta != c_meta and (p_meta != ("", "", "") and c_meta != ("", "", "")):
                diff.metadata_changed_articles.append(url)
                c_art.status = "METADATA_CHANGED"
            else:
                diff.unchanged_articles.append(url)
                c_art.status = "UNCHANGED"

        # 4. Check ordering changes across shared sequence
        prev_shared_order = [u for u in previous_state.archive_order if u in common_urls]
        curr_shared_order = [u for u in current_state.archive_order if u in common_urls]
        if prev_shared_order != curr_shared_order:
            # Find which specific items reordered
            reordered = set()
            for i, u in enumerate(curr_shared_order):
                if i < len(prev_shared_order) and prev_shared_order[i] != u:
                    reordered.add(u)
            diff.reordered_articles = sorted(list(reordered))

        # 5. Archive Page comparisons
        prev_pages = previous_state.archive_pages
        curr_pages = current_state.archive_pages

        for p_url, c_page in curr_pages.items():
            if p_url not in prev_pages:
                diff.archive_page_changes[p_url] = "NEW_PAGE"
            else:
                p_page = prev_pages[p_url]
                if c_page.unordered_fingerprint != p_page.unordered_fingerprint:
                    diff.archive_page_changes[p_url] = "MEMBERSHIP_CHANGED"
                elif c_page.ordered_fingerprint != p_page.ordered_fingerprint:
                    diff.archive_page_changes[p_url] = "ORDER_CHANGED"
                elif (c_page.next_page_url != p_page.next_page_url) or (c_page.previous_page_url != p_page.previous_page_url):
                    diff.archive_page_changes[p_url] = "PAGINATION_CHANGED"
                    diff.pagination_changed = True
                else:
                    diff.archive_page_changes[p_url] = "UNCHANGED"

        # Check head page change
        if current_state.head_fingerprint and previous_state.head_fingerprint:
            if current_state.head_fingerprint != previous_state.head_fingerprint:
                diff.head_changed = True

        # 6. Anomaly Detection
        anomalies = []

        # Anomaly A: Article count collapsed (e.g. > 50% drop)
        if len(prev_urls) >= 10 and len(curr_urls) < len(prev_urls) * 0.5:
            anomalies.append("ARCHIVE_COLLAPSED_UNEXPECTEDLY")

        # Anomaly B: Head page is empty
        if current_state.archive_pages:
            first_page = list(current_state.archive_pages.values())[0]
            if len(first_page.article_urls) == 0:
                anomalies.append("ARCHIVE_EMPTY_UNEXPECTEDLY")

        # Anomaly C: Massive inexplicable reordering
        if len(common_urls) >= 10 and len(diff.reordered_articles) > len(common_urls) * 0.8:
            anomalies.append("MASSIVE_UNEXPECTED_REORDERING")

        diff.anomalies = anomalies
        if anomalies:
            diff.manual_review_required = True

        return diff

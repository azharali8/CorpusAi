"""
Archive Visit Policy and Crawl Stability Engine for CorpusAI.

Models paginated archive observations, fingerprinting (ordered vs. set),
global article URL tracking ledgers, stability checking, and configurable
revisit policies (Naive, Precision, Robust, Robust_Head_Verified) to handle unstable pagination,
same-date random reordering, cross-page shifts, mid-crawl insertions, and cached pages.
"""

import hashlib
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode


def normalize_article_url(raw_url: str, strip_fragment: bool = True, strip_tracking_params: bool = True) -> str:
    """
    Normalize article URLs deterministically without over-normalizing query parameters.

    Preserves semantic query parameters (e.g. ?id=123, ?article=45)
    Strips URL fragments (#section) and standard tracking parameters (utm_*, ref, fbclid).
    """
    if not raw_url or not isinstance(raw_url, str):
        return ""

    parsed = urlparse(raw_url.strip())

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"

    # Normalize trailing slashes on non-root paths if standard
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Query param handling
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    if strip_tracking_params:
        tracking_keys = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid"}
        query_params = {k: v for k, v in query_params.items() if k.lower() not in tracking_keys}

    # Deterministic query encoding
    sorted_query = urlencode(sorted((k, v) for k, vs in query_params.items() for v in vs))

    fragment = "" if strip_fragment else parsed.fragment

    return urlunparse((scheme, netloc, path, parsed.params, sorted_query, fragment))


def compute_page_fingerprints(article_urls: List[str]) -> Tuple[str, str]:
    """
    Compute ordered and unordered (set) SHA-256 fingerprints for a list of article URLs.

    Returns:
        (ordered_fingerprint, set_fingerprint)
    """
    normalized_urls = [normalize_article_url(u) for u in article_urls if u]

    # Ordered fingerprint: exact sequence
    ordered_str = "\n".join(normalized_urls)
    ordered_hash = hashlib.sha256(ordered_str.encode("utf-8")).hexdigest()

    # Set fingerprint: sorted unique set
    unique_sorted = sorted(list(set(normalized_urls)))
    set_str = "\n".join(unique_sorted)
    set_hash = hashlib.sha256(set_str.encode("utf-8")).hexdigest()

    return ordered_hash, set_hash


class ArticleLedger:
    """
    Tracks canonical article URLs across all archive page observations during a crawl.
    """

    def __init__(self):
        self.articles: Dict[str, Dict[str, Any]] = {}
        self.duplicate_observation_count: int = 0

    def record_article(
        self,
        article_url: str,
        source_page: int,
        visit_number: int,
        timestamp: Optional[str] = None,
        pub_date: Optional[str] = None,
    ) -> bool:
        """
        Record observation of an article URL.

        Returns:
            True if article is newly discovered, False if it was already in the ledger.
        """
        canonical_url = normalize_article_url(article_url)
        if not canonical_url:
            return False

        if canonical_url not in self.articles:
            self.articles[canonical_url] = {
                "article_url": canonical_url,
                "first_seen_visit": visit_number,
                "last_seen_visit": visit_number,
                "seen_on_pages": [source_page],
                "observation_count": 1,
                "publication_date": pub_date,
            }
            return True
        else:
            self.duplicate_observation_count += 1
            entry = self.articles[canonical_url]
            entry["last_seen_visit"] = visit_number
            if source_page not in entry["seen_on_pages"]:
                entry["seen_on_pages"].append(source_page)
            entry["observation_count"] += 1
            return False

    def get_unique_count(self) -> int:
        return len(self.articles)

    def get_all_urls(self) -> Set[str]:
        return set(self.articles.keys())


class ArchiveVisitPolicy:
    """
    Deterministic visit policy engine that registers observations, detects
    ordering vs set changes, manages revisits, enforces visit budgets,
    implements head-page verification, boundary propagation reconciliation,
    and determines archive convergence.
    """

    def __init__(
        self,
        profile: str = "ROBUST",
        max_visits_per_page: int = 3,
        max_total_requests: int = 50,
        required_stable_observations: int = 2,
        neighbor_revisit_on_set_change: bool = True,
        head_verification_enabled: bool = False,
        head_page_number: int = 1,
        required_head_stable_observations: int = 2,
        boundary_propagation_enabled: bool = True,
        max_verification_rounds: int = 3,
    ):
        self.profile = profile.upper()
        self.max_visits_per_page = max_visits_per_page
        self.max_total_requests = max_total_requests
        self.required_stable_observations = required_stable_observations
        self.neighbor_revisit_on_set_change = neighbor_revisit_on_set_change

        # Head verification & reconciliation parameters
        self.head_verification_enabled = head_verification_enabled
        self.head_page_number = head_page_number
        self.required_head_stable_observations = required_head_stable_observations
        self.boundary_propagation_enabled = boundary_propagation_enabled
        self.max_verification_rounds = max_verification_rounds

        # Crawl phase state: INITIAL_TRAVERSAL, HEAD_VERIFICATION, RECONCILIATION, FINAL_VERIFICATION, CONVERGED
        self.current_phase: str = "INITIAL_TRAVERSAL"
        self.head_verified_after_traversal: bool = False
        self.consecutive_head_stable_count: int = 0
        self.last_head_set_hash: Optional[str] = None
        self.verification_round_count: int = 0
        self.initial_traversal_complete: bool = False

        # State tracking per page: page_num -> dict
        # {
        #   "visit_count": int,
        #   "observations": List[dict],
        #   "consecutive_set_stable_count": int,
        #   "last_ordered_hash": str,
        #   "last_set_hash": str,
        #   "is_stable": bool
        # }
        self.page_states: Dict[int, Dict[str, Any]] = {}

        # Global event log
        self.events: List[Dict[str, Any]] = []

        # Queue of pages scheduled for revisit: List[int]
        self.revisit_queue: List[int] = []

        # Global ledger
        self.ledger = ArticleLedger()

        # Metrics counters
        self.total_requests: int = 0
        self.total_revisits: int = 0
        self.order_only_changes: int = 0
        self.set_changes: int = 0
        self.new_articles_count: int = 0
        self.head_changes_detected: int = 0
        self.new_articles_during_verification: int = 0
        self.propagation_depth: int = 0
        self.budget_exhausted: bool = False
        self.termination_reason: str = "IN_PROGRESS"

        # Request phase breakdown counters
        self.initial_requests_count: int = 0
        self.head_verification_requests_count: int = 0
        self.reconciliation_requests_count: int = 0

    def log_event(self, event_type: str, page_num: int, details: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "event_type": event_type,
            "page_num": page_num,
            "request_num": self.total_requests,
            "phase": self.current_phase,
            "details": details or {},
        })

    def register_observation(
        self,
        page_num: int,
        article_urls: List[str],
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a single observation of an archive page.

        Returns a summary dict of the observation assessment.
        """
        self.total_requests += 1

        # Track request count by phase
        if self.current_phase == "INITIAL_TRAVERSAL":
            self.initial_requests_count += 1
        elif self.current_phase in ("HEAD_VERIFICATION", "FINAL_VERIFICATION"):
            self.head_verification_requests_count += 1
        elif self.current_phase == "RECONCILIATION":
            self.reconciliation_requests_count += 1

        if self.total_requests > self.max_total_requests:
            self.budget_exhausted = True
            self.termination_reason = "BUDGET_EXHAUSTED"
            self.log_event("BUDGET_EXHAUSTED", page_num, {"reason": "max_total_requests_exceeded"})
            return {"status": "BUDGET_EXHAUSTED", "change_type": "NONE"}

        if page_num not in self.page_states:
            self.page_states[page_num] = {
                "visit_count": 0,
                "observations": [],
                "consecutive_set_stable_count": 1,
                "last_ordered_hash": None,
                "last_set_hash": None,
                "is_stable": False,
            }
            is_first_visit = True
        else:
            is_first_visit = False
            self.total_revisits += 1

        state = self.page_states[page_num]
        state["visit_count"] += 1

        if state["visit_count"] > self.max_visits_per_page:
            self.log_event("PAGE_VISIT_LIMIT_REACHED", page_num, {"visits": state["visit_count"]})

        # Calculate fingerprints
        ordered_hash, set_hash = compute_page_fingerprints(article_urls)

        # Record articles in ledger
        new_on_page = 0
        for url in article_urls:
            is_new = self.ledger.record_article(
                article_url=url,
                source_page=page_num,
                visit_number=self.total_requests,
                timestamp=timestamp,
            )
            if is_new:
                new_on_page += 1
                self.new_articles_count += 1
                if self.current_phase in ("HEAD_VERIFICATION", "RECONCILIATION", "FINAL_VERIFICATION"):
                    self.new_articles_during_verification += 1
                    self.log_event("NEW_ARTICLE_DURING_VERIFICATION", page_num, {"article_url": url})
                else:
                    self.log_event("NEW_ARTICLE_DISCOVERED", page_num, {"article_url": url})

        change_type = "NONE"

        if is_first_visit:
            change_type = "FIRST_VISIT"
            state["consecutive_set_stable_count"] = 1
            self.log_event("PAGE_FIRST_SEEN", page_num, {"article_count": len(article_urls)})
            if page_num == self.head_page_number:
                self.last_head_set_hash = set_hash
                self.consecutive_head_stable_count = 1
        else:
            prev_ordered = state["last_ordered_hash"]
            prev_set = state["last_set_hash"]

            if set_hash == prev_set and ordered_hash == prev_ordered:
                change_type = "NO_CHANGE"
                state["consecutive_set_stable_count"] += 1
                self.log_event("PAGE_OBSERVATION_IDENTICAL", page_num)
                if page_num == self.head_page_number:
                    self.consecutive_head_stable_count += 1
            elif set_hash == prev_set and ordered_hash != prev_ordered:
                change_type = "ORDER_CHANGED_ONLY"
                self.order_only_changes += 1
                # Same-date reordering should NOT reset set stability count!
                state["consecutive_set_stable_count"] += 1
                self.log_event("ORDER_CHANGED_ONLY", page_num)
                if page_num == self.head_page_number:
                    self.consecutive_head_stable_count += 1
            else:
                change_type = "ARTICLE_SET_CHANGED"
                self.set_changes += 1
                # Set changed -> reset consecutive stability count
                state["consecutive_set_stable_count"] = 1
                state["is_stable"] = False
                self.log_event("ARTICLE_SET_CHANGED", page_num, {"new_articles": new_on_page})

                if page_num == self.head_page_number:
                    self.head_changes_detected += 1
                    self.consecutive_head_stable_count = 1
                    self.head_verified_after_traversal = False
                    self.log_event("HEAD_SET_CHANGED", page_num, {"new_articles": new_on_page})

                # Schedule boundary propagation reconciliation if policy enabled
                if self.profile != "NAIVE":
                    if self.boundary_propagation_enabled or self.neighbor_revisit_on_set_change:
                        self.trigger_boundary_propagation(page_num)

        state["last_ordered_hash"] = ordered_hash
        state["last_set_hash"] = set_hash
        if page_num == self.head_page_number:
            self.last_head_set_hash = set_hash

        # Check page stability threshold
        if state["consecutive_set_stable_count"] >= self.required_stable_observations:
            if not state["is_stable"]:
                state["is_stable"] = True
                self.log_event("PAGE_STABLE", page_num, {"consecutive_stable": state["consecutive_set_stable_count"]})

        # Head verification tracking
        if page_num == self.head_page_number and self.initial_traversal_complete:
            if self.consecutive_head_stable_count >= self.required_head_stable_observations:
                self.head_verified_after_traversal = True
                self.log_event("HEAD_STABLE", page_num, {"consecutive_head_stable": self.consecutive_head_stable_count})

        observation_record = {
            "visit_number": state["visit_count"],
            "total_request_number": self.total_requests,
            "timestamp": timestamp,
            "article_count": len(article_urls),
            "ordered_hash": ordered_hash,
            "set_hash": set_hash,
            "change_type": change_type,
            "phase": self.current_phase,
        }
        state["observations"].append(observation_record)

        return {
            "page_num": page_num,
            "visit_count": state["visit_count"],
            "change_type": change_type,
            "is_stable": state["is_stable"],
            "consecutive_set_stable_count": state["consecutive_set_stable_count"],
            "new_articles_discovered": new_on_page,
            "head_verified": self.head_verified_after_traversal,
        }

    def trigger_boundary_propagation(self, changed_page: int) -> None:
        """
        Schedule downstream boundary propagation when an article set changes on a page.
        page N changed -> schedule page N, page N+1 (and page N-1 if appropriate).
        """
        self.log_event("BOUNDARY_PROPAGATION_STARTED", changed_page)
        self.schedule_revisit(changed_page)
        self.schedule_revisit(changed_page + 1)
        if changed_page > 1:
            self.schedule_revisit(changed_page - 1)
        if changed_page + 1 > self.propagation_depth:
            self.propagation_depth = changed_page + 1

    def schedule_revisit(self, page_num: int) -> bool:
        """
        Schedule a page for revisit with queue deduplication and budget safety checks.
        """
        if page_num <= 0:
            return False

        current_visits = self.page_states.get(page_num, {}).get("visit_count", 0)
        if current_visits >= self.max_visits_per_page:
            return False

        if page_num not in self.revisit_queue:
            self.revisit_queue.append(page_num)
            self.log_event("PAGE_REVISIT_SCHEDULED", page_num)
            return True

        return False

    def get_next_revisit_page(self) -> Optional[int]:
        """
        Pop the next page from the revisit queue.
        """
        while self.revisit_queue:
            page = self.revisit_queue.pop(0)
            current_visits = self.page_states.get(page, {}).get("visit_count", 0)
            if current_visits < self.max_visits_per_page:
                return page
        return None

    def is_archive_converged(self, all_known_pages: Set[int]) -> bool:
        """
        Check if the archive crawl has converged under the active policy.

        Strong Convergence criteria:
        1. All required visited pages exist in page_states and meet stability requirement (is_stable == True).
        2. Revisit queue is empty.
        3. Total request budget not exhausted.
        4. If head_verification_enabled:
           - head_verified_after_traversal == True
           - consecutive_head_stable_count >= required_head_stable_observations
        """
        if self.budget_exhausted:
            return False

        if self.revisit_queue:
            return False

        if not all_known_pages:
            return False

        for p in all_known_pages:
            state = self.page_states.get(p)
            if not state or not state["is_stable"]:
                return False

        if self.head_verification_enabled:
            if not self.head_verified_after_traversal:
                return False
            if self.consecutive_head_stable_count < self.required_head_stable_observations:
                return False

        return True


def create_policy(profile_name: str, max_pages: int = 5) -> ArchiveVisitPolicy:
    """
    Factory helper to instantiate standardized policy profiles.
    """
    name = profile_name.upper()
    if name == "NAIVE":
        return ArchiveVisitPolicy(
            profile="NAIVE",
            max_visits_per_page=1,
            max_total_requests=max_pages + 2,
            required_stable_observations=1,
            neighbor_revisit_on_set_change=False,
            head_verification_enabled=False,
        )
    elif name == "PRECISION":
        return ArchiveVisitPolicy(
            profile="PRECISION",
            max_visits_per_page=2,
            max_total_requests=max_pages * 3,
            required_stable_observations=2,
            neighbor_revisit_on_set_change=False,
            head_verification_enabled=False,
        )
    elif name == "OLD_ROBUST" or name == "ROBUST":
        # Preserves the previous Task 2 Robust policy without head verification
        return ArchiveVisitPolicy(
            profile="ROBUST",
            max_visits_per_page=4,
            max_total_requests=max_pages * 5,
            required_stable_observations=2,
            neighbor_revisit_on_set_change=True,
            head_verification_enabled=False,
            boundary_propagation_enabled=True,
        )
    elif name in ("NEW_ROBUST_HEAD_VERIFIED", "ROBUST_HEAD_VERIFIED"):
        # The new Task 2B Strong Convergence policy
        return ArchiveVisitPolicy(
            profile="ROBUST_HEAD_VERIFIED",
            max_visits_per_page=5,
            max_total_requests=max_pages * 6,
            required_stable_observations=2,
            neighbor_revisit_on_set_change=True,
            head_verification_enabled=True,
            head_page_number=1,
            required_head_stable_observations=2,
            boundary_propagation_enabled=True,
            max_verification_rounds=3,
        )
    else:
        raise ValueError(f"Unknown policy profile: {profile_name}")

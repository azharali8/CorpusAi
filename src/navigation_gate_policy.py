"""
Navigation Gate Policy and Interstitial State Management for CorpusAI.

Models access interstitials, age gates, consent dialogs, redirect chains,
origin-scoped session state, gate provenance, and policy approval boundaries.
"""

from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from src.archive_visit_policy import normalize_article_url


class NavigationGatePolicy:
    """
    Evaluates observed navigation gates/interstitials, enforces policy approval boundaries,
    maintains origin-scoped session cookies/states, detects redirect/gate loops,
    and resolves destination article URLs.
    """

    def __init__(
        self,
        allowed_gate_actions: Optional[Dict[str, bool]] = None,
        max_redirect_hops: int = 5,
        max_gate_hops: int = 5,
        max_gate_interactions_per_origin: int = 3,
        allowed_domains: Optional[Set[str]] = None,
    ):
        # Configurable allowed gate actions (e.g. {"AGE_CONFIRMATION": True, "CONSENT": True})
        self.allowed_gate_actions = allowed_gate_actions or {
            "AGE_CONFIRMATION": True,
            "CONSENT": True,
            "INTERSTITIAL": True,
            "REDIRECT": True,
        }
        self.max_redirect_hops = max_redirect_hops
        self.max_gate_hops = max_gate_hops
        self.max_gate_interactions_per_origin = max_gate_interactions_per_origin
        self.allowed_domains = {d.lower() for d in allowed_domains} if allowed_domains else set()

        # Origin-scoped session state: origin (e.g. "portal.test") -> dict of session cookies/flags
        # {"portal.test": {"age_verified": True, "consent_given": True, "session_active": True}}
        self.session_states: Dict[str, Dict[str, Any]] = {}

        # Tracking interactions per origin: origin -> int
        self.origin_interaction_counts: Dict[str, int] = {}

        # Gate provenance records: List[dict]
        self.provenance_records: List[Dict[str, Any]] = []

        # Structured event log
        self.events: List[Dict[str, Any]] = []

        # Metrics
        self.total_gate_encounters: int = 0
        self.total_gate_interactions: int = 0
        self.total_session_reuses: int = 0
        self.total_redirect_hops: int = 0
        self.loops_detected: int = 0
        self.out_of_scope_gate_targets: int = 0

    def log_event(self, event_type: str, url: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "event_type": event_type,
            "url": url,
            "details": details or {},
        })

    def get_origin(self, url: str) -> str:
        """Extract netloc/origin from URL."""
        parsed = urlparse(url)
        return parsed.netloc.lower()

    def get_session_state(self, origin: str) -> Dict[str, Any]:
        """Retrieve or create origin-scoped session state."""
        if origin not in self.session_states:
            self.session_states[origin] = {}
        return self.session_states[origin]

    def resolve_url(
        self,
        start_url: str,
        fetcher_callback: Any,
    ) -> Dict[str, Any]:
        """
        Deterministically resolve an initial article or navigation URL through any gates or redirect chains.

        Args:
            start_url: Candidate URL to resolve.
            fetcher_callback: Callable(url, session_state) -> response_dict

        Returns:
            Dict containing:
            {
                "original_url": str,
                "resolved_url": Optional[str],
                "status": "RESOLVED" | "GATE_BLOCKED" | "GATE_LOOP_DETECTED" | "REDIRECT_LIMIT_EXCEEDED" | "UNSUPPORTED_GATE" | "OUT_OF_SCOPE" | "FETCH_FAILED",
                "gate_encountered": bool,
                "gate_type": Optional[str],
                "redirect_chain": List[str],
                "actions_taken": List[str],
                "session_reused": bool,
                "final_response": Optional[Dict[str, Any]]
            }
        """
        canonical_start = normalize_article_url(start_url)
        origin = self.get_origin(canonical_start)

        # Domain registration if first URL
        if not self.allowed_domains and origin:
            self.allowed_domains.add(origin)

        current_url = canonical_start
        redirect_chain = [current_url]
        visited_in_chain: List[str] = [current_url]
        actions_taken = []
        gate_encountered = False
        primary_gate_type = None
        session_reused = False

        redirect_count = 0
        gate_count = 0

        while True:
            current_origin = self.get_origin(current_url)

            # Scope check
            if self.allowed_domains and current_origin not in self.allowed_domains:
                self.out_of_scope_gate_targets += 1
                self.log_event("OUT_OF_SCOPE_GATE_TARGET", current_url, {"origin": current_origin})
                return {
                    "original_url": canonical_start,
                    "resolved_url": None,
                    "status": "OUT_OF_SCOPE_GATE_TARGET",
                    "gate_encountered": gate_encountered,
                    "gate_type": primary_gate_type,
                    "redirect_chain": redirect_chain,
                    "actions_taken": actions_taken,
                    "session_reused": session_reused,
                    "final_response": None,
                }

            session_state = self.get_session_state(current_origin)

            # Execute fetch through simulator/fetcher callback
            response = fetcher_callback(current_url, session_state)
            status_code = response.get("status_code", 200)
            page_type = response.get("page_type", "CONTENT")
            gate_type = response.get("gate_type")
            target_url = response.get("target_url")
            cookies_set = response.get("cookies_set", {})

            # Update session state with any returned cookies
            if cookies_set:
                session_state.update(cookies_set)
                self.log_event("SESSION_STATE_CREATED", current_url, {"cookies": cookies_set})

            # Check if session was reused
            if response.get("session_reused", False):
                session_reused = True
                self.total_session_reuses += 1
                self.log_event("SESSION_STATE_REUSED", current_url, {"origin": current_origin})

            # 1. Normal Content (No gate / resolved content)
            if page_type == "CONTENT" and status_code == 200:
                self.log_event("DESTINATION_RESOLVED", current_url)
                res = {
                    "original_url": canonical_start,
                    "resolved_url": current_url,
                    "status": "RESOLVED",
                    "gate_encountered": gate_encountered,
                    "gate_type": primary_gate_type,
                    "redirect_chain": redirect_chain,
                    "actions_taken": actions_taken,
                    "session_reused": session_reused,
                    "final_response": response,
                }
                self.provenance_records.append(res)
                return res

            # 2. HTTP Fetch Failures
            if status_code in (404, 500, 403) and page_type != "GATE":
                self.log_event("FETCH_FAILED", current_url, {"status_code": status_code})
                return {
                    "original_url": canonical_start,
                    "resolved_url": None,
                    "status": "FETCH_FAILED",
                    "gate_encountered": gate_encountered,
                    "gate_type": primary_gate_type,
                    "redirect_chain": redirect_chain,
                    "actions_taken": actions_taken,
                    "session_reused": session_reused,
                    "final_response": response,
                }

            # 3. HTTP / Meta Redirect Handling
            if page_type == "REDIRECT" or (300 <= status_code < 400):
                redirect_count += 1
                self.total_redirect_hops += 1
                if redirect_count > self.max_redirect_hops:
                    self.log_event("REDIRECT_LIMIT_EXCEEDED", current_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "REDIRECT_LIMIT_EXCEEDED",
                        "gate_encountered": gate_encountered,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                next_url = normalize_article_url(target_url or "")
                if next_url in visited_in_chain:
                    self.loops_detected += 1
                    self.log_event("REDIRECT_LOOP_DETECTED", next_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "REDIRECT_LOOP_DETECTED",
                        "gate_encountered": gate_encountered,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                current_url = next_url
                redirect_chain.append(current_url)
                visited_in_chain.append(current_url)
                continue

            # 4. Gate / Interstitial Handling
            if page_type == "GATE":
                gate_encountered = True
                if not primary_gate_type:
                    primary_gate_type = gate_type
                self.total_gate_encounters += 1
                self.log_event("GATE_DETECTED", current_url, {"gate_type": gate_type})

                gate_count += 1
                if gate_count > self.max_gate_hops:
                    self.loops_detected += 1
                    self.log_event("GATE_LOOP_DETECTED", current_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "GATE_LOOP_DETECTED",
                        "gate_encountered": True,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                # Check Policy Approval Boundary
                is_allowed = self.allowed_gate_actions.get(gate_type, False)
                if not is_allowed:
                    self.log_event("GATE_ACTION_DENIED", current_url, {"gate_type": gate_type})
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "GATE_BLOCKED",
                        "gate_encountered": True,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                # Check Origin Interaction Limit
                current_origin_interactions = self.origin_interaction_counts.get(current_origin, 0)
                if current_origin_interactions >= self.max_gate_interactions_per_origin:
                    self.log_event("GATE_ORIGIN_BUDGET_EXHAUSTED", current_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "GATE_ORIGIN_BUDGET_EXHAUSTED",
                        "gate_encountered": True,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                # Check if unsupported gate
                interaction_type = response.get("interaction_required", "CONFIRM")
                if interaction_type == "UNSUPPORTED":
                    self.log_event("UNSUPPORTED_GATE", current_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "UNSUPPORTED_GATE",
                        "gate_encountered": True,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                # Execute allowed gate interaction
                self.total_gate_interactions += 1
                self.origin_interaction_counts[current_origin] = current_origin_interactions + 1
                action_name = f"APPROVE_{gate_type}_{interaction_type}"
                actions_taken.append(action_name)
                self.log_event("GATE_ACTION_ALLOWED", current_url, {"action": action_name})

                # Simulator provides resolved next target after gate interaction
                next_url = normalize_article_url(target_url or current_url)

                # Check loop
                if next_url == current_url and not cookies_set and not response.get("session_updated"):
                    self.loops_detected += 1
                    self.log_event("GATE_LOOP_DETECTED", next_url)
                    return {
                        "original_url": canonical_start,
                        "resolved_url": None,
                        "status": "GATE_LOOP_DETECTED",
                        "gate_encountered": True,
                        "gate_type": primary_gate_type,
                        "redirect_chain": redirect_chain,
                        "actions_taken": actions_taken,
                        "session_reused": session_reused,
                        "final_response": response,
                    }

                current_url = next_url
                redirect_chain.append(current_url)
                visited_in_chain.append(current_url)
                continue

            # Fallback for unexpected page_type
            return {
                "original_url": canonical_start,
                "resolved_url": None,
                "status": "UNKNOWN_ERROR",
                "gate_encountered": gate_encountered,
                "gate_type": primary_gate_type,
                "redirect_chain": redirect_chain,
                "actions_taken": actions_taken,
                "session_reused": session_reused,
                "final_response": response,
            }

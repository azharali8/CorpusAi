"""
Gated and Multi-Page Article Portal Simulator for CorpusAI Task 4.

Models 15 deterministic scenarios:
- SCENARIO A: No Gate (Direct access)
- SCENARIO B: Age Confirmation Gate (Session cookie sets on confirmation)
- SCENARIO C: Site-Wide Age Session (First article triggers gate; subsequent articles reuse session)
- SCENARIO D: Consent Interstitial (Requires explicit accept action)
- SCENARIO E: Redirect Chain (Archive -> Intermediate -> Gate -> Article)
- SCENARIO F: Gate Loop (Gate -> Target -> Gate -> ...)
- SCENARIO G: Unresolvable Unsupported Gate
- SCENARIO H: External Out-of-Scope Gate Target
- SCENARIO I: Three-Page Logical Article (Page 1 -> Page 2 -> Page 3)
- SCENARIO J: Multi-Page Loop (Page 1 -> Page 2 -> Page 1)
- SCENARIO K: Missing Middle Page (Page 1 -> Page 2 (404))
- SCENARIO L: Duplicate Component Links
- SCENARIO M: Arbitrary URL Shapes (?id=123 -> /continue -> /read/abc)
- SCENARIO N: Gate on Page 2 (Page 1 direct, Page 2 age-gated, Page 3 direct)
- SCENARIO O: Session State Expires Mid-Article (Requires re-verification on Page 3)
"""

import copy
from typing import Any, Dict, List, Optional, Set, Tuple


class GatedPortalSimulator:
    """
    Simulates a portal with access gates, interstitials, redirect hops,
    and multi-page articles.
    """

    def __init__(self, scenario: str = "A_NO_GATE"):
        self.scenario = scenario.upper()
        self.request_count: int = 0
        self.visit_counts: Dict[str, int] = {}
        self.session_expiry_counter: int = 0

    def fetch_url(self, url: str, session_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch a URL with active simulated origin session state.
        """
        self.request_count += 1
        self.visit_counts[url] = self.visit_counts.get(url, 0) + 1
        visit_num = self.visit_counts[url]

        # -------------------------------------------------------------
        # SCENARIO A: No Gate
        # -------------------------------------------------------------
        if self.scenario in ("A_NO_GATE", "A"):
            return {
                "url": url,
                "status_code": 200,
                "page_type": "CONTENT",
                "next_page_url": None,
                "cookies_set": {},
            }

        # -------------------------------------------------------------
        # SCENARIO B: Age Confirmation Gate
        # -------------------------------------------------------------
        elif self.scenario in ("B_AGE_GATE", "B"):
            if not session_state.get("age_verified"):
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "GATE",
                    "gate_type": "AGE_CONFIRMATION",
                    "interaction_required": "CONFIRM",
                    "target_url": url,
                    "cookies_set": {"age_verified": True},
                    "session_updated": True,
                }
            else:
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "CONTENT",
                    "next_page_url": None,
                    "session_reused": True,
                }

        # -------------------------------------------------------------
        # SCENARIO C: Site-Wide Age Session
        # -------------------------------------------------------------
        elif self.scenario in ("C_SITEWIDE_AGE_SESSION", "C"):
            if not session_state.get("age_verified"):
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "GATE",
                    "gate_type": "AGE_CONFIRMATION",
                    "interaction_required": "CONFIRM",
                    "target_url": url,
                    "cookies_set": {"age_verified": True},
                    "session_updated": True,
                }
            else:
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "CONTENT",
                    "next_page_url": None,
                    "session_reused": True,
                }

        # -------------------------------------------------------------
        # SCENARIO D: Consent Interstitial
        # -------------------------------------------------------------
        elif self.scenario in ("D_CONSENT_INTERSTITIAL", "D"):
            if not session_state.get("consent_given"):
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "GATE",
                    "gate_type": "CONSENT",
                    "interaction_required": "ACCEPT",
                    "target_url": url,
                    "cookies_set": {"consent_given": True},
                    "session_updated": True,
                }
            else:
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "CONTENT",
                    "next_page_url": None,
                    "session_reused": True,
                }

        # -------------------------------------------------------------
        # SCENARIO E: Redirect Chain (URL -> Hop1 -> Gate -> Article)
        # -------------------------------------------------------------
        elif self.scenario in ("E_REDIRECT_CHAIN", "E"):
            if url == "https://portal.test/entry":
                return {
                    "url": url,
                    "status_code": 302,
                    "page_type": "REDIRECT",
                    "target_url": "https://portal.test/intermediate",
                }
            elif url == "https://portal.test/intermediate":
                if not session_state.get("age_verified"):
                    return {
                        "url": url,
                        "status_code": 200,
                        "page_type": "GATE",
                        "gate_type": "AGE_CONFIRMATION",
                        "interaction_required": "CONFIRM",
                        "target_url": "https://portal.test/article/destination",
                        "cookies_set": {"age_verified": True},
                        "session_updated": True,
                    }
                else:
                    return {
                        "url": url,
                        "status_code": 302,
                        "page_type": "REDIRECT",
                        "target_url": "https://portal.test/article/destination",
                    }
            elif url == "https://portal.test/article/destination":
                return {
                    "url": url,
                    "status_code": 200,
                    "page_type": "CONTENT",
                    "next_page_url": None,
                }

        # -------------------------------------------------------------
        # SCENARIO F: Gate Loop
        # -------------------------------------------------------------
        elif self.scenario in ("F_GATE_LOOP", "F"):
            return {
                "url": url,
                "status_code": 200,
                "page_type": "GATE",
                "gate_type": "AGE_CONFIRMATION",
                "interaction_required": "CONFIRM",
                "target_url": url,
                "cookies_set": {}, # Does not set cookie, loops indefinitely!
            }

        # -------------------------------------------------------------
        # SCENARIO G: Unresolvable Unsupported Gate
        # -------------------------------------------------------------
        elif self.scenario in ("G_UNRESOLVABLE_GATE", "G"):
            return {
                "url": url,
                "status_code": 200,
                "page_type": "GATE",
                "gate_type": "UNKNOWN_GATE",
                "interaction_required": "UNSUPPORTED",
                "target_url": url,
            }

        # -------------------------------------------------------------
        # SCENARIO H: External Out-of-Scope Gate Target
        # -------------------------------------------------------------
        elif self.scenario in ("H_EXTERNAL_TARGET", "H"):
            return {
                "url": url,
                "status_code": 200,
                "page_type": "GATE",
                "gate_type": "INTERSTITIAL",
                "interaction_required": "CONFIRM",
                "target_url": "https://external-ad-network.com/landing",
            }

        # -------------------------------------------------------------
        # SCENARIO I: Three-Page Logical Article
        # -------------------------------------------------------------
        elif self.scenario in ("I_THREE_PAGE_ARTICLE", "I"):
            if url == "https://portal.test/article/multipage_01":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/multipage_01?page=2"}
            elif url == "https://portal.test/article/multipage_01?page=2":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/multipage_01?page=3"}
            elif url == "https://portal.test/article/multipage_01?page=3":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

        # -------------------------------------------------------------
        # SCENARIO J: Multi-Page Loop (P1 -> P2 -> P1)
        # -------------------------------------------------------------
        elif self.scenario in ("J_MULTIPAGE_LOOP", "J"):
            if url == "https://portal.test/article/loop_01":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/loop_02"}
            elif url == "https://portal.test/article/loop_02":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/loop_01"}

        # -------------------------------------------------------------
        # SCENARIO K: Missing Middle Page (P1 -> P2 (404))
        # -------------------------------------------------------------
        elif self.scenario in ("K_MISSING_PAGE", "K"):
            if url == "https://portal.test/article/broken_01":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/broken_02"}
            elif url == "https://portal.test/article/broken_02":
                return {"url": url, "status_code": 404, "page_type": "CONTENT", "next_page_url": None}

        # -------------------------------------------------------------
        # SCENARIO L: Duplicate Component Links
        # -------------------------------------------------------------
        elif self.scenario in ("L_DUPLICATE_COMPONENT_LINK", "L"):
            if url == "https://portal.test/article/dup_01":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/dup_02"}
            elif url == "https://portal.test/article/dup_02":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

        # -------------------------------------------------------------
        # SCENARIO M: Arbitrary URL Shapes
        # -------------------------------------------------------------
        elif self.scenario in ("M_ARBITRARY_SHAPES", "M"):
            if url == "https://portal.test/story?id=123":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/story/123/continue"}
            elif url == "https://portal.test/story/123/continue":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/read/abc987"}
            elif url == "https://portal.test/read/abc987":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

        # -------------------------------------------------------------
        # SCENARIO N: Gate on Page 2
        # -------------------------------------------------------------
        elif self.scenario in ("N_GATE_ON_PAGE_2", "N"):
            if url == "https://portal.test/article/gated_p2_1":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/gated_p2_2"}
            elif url == "https://portal.test/article/gated_p2_2":
                if not session_state.get("age_verified"):
                    return {
                        "url": url,
                        "status_code": 200,
                        "page_type": "GATE",
                        "gate_type": "AGE_CONFIRMATION",
                        "interaction_required": "CONFIRM",
                        "target_url": url,
                        "cookies_set": {"age_verified": True},
                        "session_updated": True,
                    }
                else:
                    return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/gated_p2_3"}
            elif url == "https://portal.test/article/gated_p2_3":
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

        # -------------------------------------------------------------
        # SCENARIO O: Session State Expires Mid-Article
        # -------------------------------------------------------------
        elif self.scenario in ("O_SESSION_EXPIRES", "O"):
            if url == "https://portal.test/article/exp_01":
                if not session_state.get("age_verified"):
                    return {
                        "url": url,
                        "status_code": 200,
                        "page_type": "GATE",
                        "gate_type": "AGE_CONFIRMATION",
                        "interaction_required": "CONFIRM",
                        "target_url": url,
                        "cookies_set": {"age_verified": True},
                        "session_updated": True,
                    }
                else:
                    return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/exp_02"}

            elif url == "https://portal.test/article/exp_02":
                # Mid-article expiration trigger
                session_state["age_verified"] = False
                return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": "https://portal.test/article/exp_03"}

            elif url == "https://portal.test/article/exp_03":
                if not session_state.get("age_verified"):
                    return {
                        "url": url,
                        "status_code": 200,
                        "page_type": "GATE",
                        "gate_type": "AGE_CONFIRMATION",
                        "interaction_required": "CONFIRM",
                        "target_url": url,
                        "cookies_set": {"age_verified": True},
                        "session_updated": True,
                    }
                else:
                    return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

        return {"url": url, "status_code": 200, "page_type": "CONTENT", "next_page_url": None}

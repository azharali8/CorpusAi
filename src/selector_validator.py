"""
Selector Validation Module.

Evaluates candidate CSS/XPath selectors against a corpus of representative HTML documents
to measure extraction success rate, empty result frequency, and multiple match counts before
accepting rules into configuration.
"""

from typing import Any, Dict, List, Optional, Union
from src.extractor import DeterministicExtractor

DEFAULT_SELECTOR_THRESHOLD = 0.90


class SelectorValidator:
    """
    Validates extraction selectors across representative HTML sample pages.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_SELECTOR_THRESHOLD,
        extractor: Optional[DeterministicExtractor] = None,
    ):
        self.threshold = threshold
        self.extractor = extractor or DeterministicExtractor()

    def validate_selector(
        self,
        html_samples: List[str],
        selector: Union[str, Dict[str, Any]],
        selector_type: str = "css",
    ) -> Dict[str, Any]:
        """
        Validate a single selector against a list of HTML string samples.

        Args:
            html_samples: List of HTML document contents as strings.
            selector: Either a selector string or a dict like {"selector": "...", "type": "..."}
            selector_type: Default type if selector is a string ("css" or "xpath").

        Returns:
            Dictionary containing tested_pages, successful_pages, success_rate,
            empty_count, multiple_match_count, and is_acceptable boolean flag.
        """
        if isinstance(selector, dict):
            sel_str = selector.get("selector", "")
            sel_type = selector.get("type", selector_type).lower()
            rule = selector
        else:
            sel_str = selector
            sel_type = selector_type.lower()
            rule = {"selector": sel_str, "type": sel_type}

        total_pages = len(html_samples)
        if total_pages == 0:
            return {
                "selector": sel_str,
                "type": sel_type,
                "tested_pages": 0,
                "successful_pages": 0,
                "success_rate": 0.0,
                "empty_count": 0,
                "multiple_match_count": 0,
                "failure_count": 0,
                "is_acceptable": False,
                "threshold": self.threshold,
            }

        successful_pages = 0
        empty_count = 0
        multiple_match_count = 0
        failure_count = 0

        for html_doc in html_samples:
            res = self.extractor.extract_field(html_doc, rule)
            if res["success"]:
                successful_pages += 1
                if res.get("match_count", 1) > 1:
                    multiple_match_count += 1
            else:
                failure_count += 1
                if "empty text" in (res.get("error") or "").lower():
                    empty_count += 1

        success_rate = round(successful_pages / total_pages, 4)
        is_acceptable = success_rate >= self.threshold

        return {
            "selector": sel_str,
            "type": sel_type,
            "tested_pages": total_pages,
            "successful_pages": successful_pages,
            "success_rate": success_rate,
            "empty_count": empty_count,
            "multiple_match_count": multiple_match_count,
            "failure_count": failure_count,
            "is_acceptable": is_acceptable,
            "threshold": self.threshold,
        }

    def validate_rule_set(
        self,
        html_samples: List[str],
        rules: Dict[str, Union[str, Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Validate an entire dictionary of rules (e.g. title, body, date, author).

        Returns:
            Dictionary with per-field validation stats and an overall_acceptable flag.
        """
        field_reports: Dict[str, Dict[str, Any]] = {}
        all_acceptable = True

        for field_name, rule_def in rules.items():
            report = self.validate_selector(html_samples, rule_def)
            field_reports[field_name] = report
            if not report["is_acceptable"]:
                all_acceptable = False

        return {
            "fields": field_reports,
            "overall_acceptable": all_acceptable,
            "tested_pages": len(html_samples),
            "threshold": self.threshold,
        }


def validate_selector(
    html_samples: List[str],
    selector: Union[str, Dict[str, Any]],
    threshold: float = DEFAULT_SELECTOR_THRESHOLD,
) -> Dict[str, Any]:
    """
    Convenience function to validate a selector.
    """
    validator = SelectorValidator(threshold=threshold)
    return validator.validate_selector(html_samples, selector)

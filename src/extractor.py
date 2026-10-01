"""
Deterministic HTML Extractor module.

Executes CSS and XPath extraction rules against HTML documents using BeautifulSoup and lxml.
Returns structured field outputs accompanied by explicit success/failure status.
"""

from typing import Any, Dict, Optional, Union
from bs4 import BeautifulSoup
from lxml import html as lxml_html
from lxml.etree import XPathError


class DeterministicExtractor:
    """
    Deterministic HTML Extractor that applies explicit selector rules to HTML content.
    """

    def __init__(self, parser: str = "lxml"):
        self.parser = parser

    def _extract_css(self, soup: BeautifulSoup, selector: str) -> Dict[str, Any]:
        """
        Extract content using a CSS selector via BeautifulSoup.
        """
        try:
            elements = soup.select(selector)
        except Exception as e:
            return {
                "value": None,
                "success": False,
                "error": f"Invalid CSS selector '{selector}': {str(e)}",
                "match_count": 0,
            }

        if not elements:
            return {
                "value": None,
                "success": False,
                "error": f"No element found for selector '{selector}'",
                "match_count": 0,
            }

        # If single element
        if len(elements) == 1:
            raw_text = elements[0].get_text(separator=" ", strip=True)
            if not raw_text:
                return {
                    "value": None,
                    "success": False,
                    "error": "Matched element contains empty text",
                    "match_count": 1,
                }
            return {
                "value": raw_text,
                "success": True,
                "error": None,
                "match_count": 1,
            }

        # Multiple elements matched: extract and join non-empty elements
        texts = [el.get_text(separator=" ", strip=True) for el in elements]
        non_empty = [t for t in texts if t]
        if not non_empty:
            return {
                "value": None,
                "success": False,
                "error": "Matched multiple elements but all contain empty text",
                "match_count": len(elements),
            }

        joined_text = "\n\n".join(non_empty) if len(non_empty) > 1 else non_empty[0]
        return {
            "value": joined_text,
            "success": True,
            "error": None,
            "match_count": len(elements),
        }

    def _extract_xpath(self, tree: Any, xpath_expr: str) -> Dict[str, Any]:
        """
        Extract content using an XPath expression via lxml.
        """
        try:
            results = tree.xpath(xpath_expr)
        except XPathError as e:
            return {
                "value": None,
                "success": False,
                "error": f"Invalid XPath expression '{xpath_expr}': {str(e)}",
                "match_count": 0,
            }
        except Exception as e:
            return {
                "value": None,
                "success": False,
                "error": f"XPath extraction error: {str(e)}",
                "match_count": 0,
            }

        if not results:
            return {
                "value": None,
                "success": False,
                "error": f"No element found for XPath '{xpath_expr}'",
                "match_count": 0,
            }

        extracted_texts = []
        for item in results:
            if hasattr(item, "text_content"):
                t = item.text_content().strip()
                if t:
                    extracted_texts.append(t)
            elif isinstance(item, str):
                t = item.strip()
                if t:
                    extracted_texts.append(t)

        if not extracted_texts:
            return {
                "value": None,
                "success": False,
                "error": "Matched element(s) contain empty text",
                "match_count": len(results),
            }

        return {
            "value": "\n\n".join(extracted_texts) if len(extracted_texts) > 1 else extracted_texts[0],
            "success": True,
            "error": None,
            "match_count": len(results),
        }

    def extract_field(
        self, html_content: str, rule: Union[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Extract a single field from HTML content given a rule string or rule dict.
        """
        if not html_content or not html_content.strip():
            return {
                "value": None,
                "success": False,
                "error": "Empty or missing HTML content",
                "match_count": 0,
            }

        if isinstance(rule, str):
            selector = rule
            rule_type = "css"
        elif isinstance(rule, dict):
            selector = rule.get("selector", "")
            rule_type = rule.get("type", "css").lower()
        else:
            return {
                "value": None,
                "success": False,
                "error": f"Invalid rule specification type: {type(rule)}",
                "match_count": 0,
            }

        if not selector:
            return {
                "value": None,
                "success": False,
                "error": "Selector is empty or unspecified",
                "match_count": 0,
            }

        if rule_type == "xpath":
            try:
                tree = lxml_html.fromstring(html_content)
                return self._extract_xpath(tree, selector)
            except Exception as e:
                return {
                    "value": None,
                    "success": False,
                    "error": f"HTML parsing error for XPath: {str(e)}",
                    "match_count": 0,
                }
        else:
            # Default to CSS / BeautifulSoup
            try:
                soup = BeautifulSoup(html_content, self.parser)
                return self._extract_css(soup, selector)
            except Exception as e:
                return {
                    "value": None,
                    "success": False,
                    "error": f"HTML parsing error for CSS: {str(e)}",
                    "match_count": 0,
                }

    def extract(
        self, html_content: str, rules: Dict[str, Union[str, Dict[str, Any]]]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Extract all specified fields from the HTML content according to rules.
        Returns mapping of field_name -> {"value": ..., "success": True/False, ...}
        """
        results: Dict[str, Dict[str, Any]] = {}
        for field, rule in rules.items():
            results[field] = self.extract_field(html_content, rule)
        return results


def extract(
    html_content: str, rules: Dict[str, Union[str, Dict[str, Any]]]
) -> Dict[str, Dict[str, Any]]:
    """
    Convenience functional wrapper for deterministic extraction.
    """
    extractor = DeterministicExtractor()
    return extractor.extract(html_content, rules)

"""
Real-World Archive Page Article Discovery Module for CorpusAI.

Extracts ordered, unique article URLs from live/archived archive page HTML
using semantic DOM inspection (<article>, post containers, entry titles, header links)
and path filtering. Strictly rejects navigation, category, documentation, and external links.
"""

from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from src.archive_visit_policy import normalize_article_url


class ArchivePageDiscovery:
    """
    Discovers ordered candidate article URLs from an archive page's HTML structure.
    """

    def __init__(
        self,
        base_url: str = "https://wordpress.org/news/all-posts/",
        allowed_domains: Optional[Set[str]] = None,
        article_path_prefixes: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
    ):
        self.base_url = base_url
        parsed = urlparse(base_url)
        self.allowed_domains = {d.lower() for d in allowed_domains} if allowed_domains else {parsed.netloc.lower()}
        # For WordPress News: /news/YYYY/MM/... or /news/slug/
        self.article_path_prefixes = article_path_prefixes or ["/news/"]
        self.exclude_patterns = exclude_patterns or [
            "/news/all-posts",
            "/news/category",
            "/news/tag",
            "/news/author",
            "/news/page/",
            "/news/feed",
            "/news/comments",
            "/about",
            "/download",
            "/plugins",
            "/themes",
            "/patterns",
            "/learn",
            "/documentation",
            "/forums",
            "/hosting",
            "/showcase",
            "/mobile",
            "/support",
            "/login",
            "/privacy",
            "/contact",
            "/wp-admin",
            "/wp-content",
            "/wp-includes",
            "/wp-json",
            "#",
        ]

    def is_valid_article_link(self, raw_url: str) -> bool:
        if not raw_url or not isinstance(raw_url, str):
            return False

        full_url = urljoin(self.base_url, raw_url.strip())
        parsed = urlparse(full_url)
        domain = parsed.netloc.lower()

        # Check domain
        if self.allowed_domains and domain not in self.allowed_domains:
            return False

        path = parsed.path.lower()

        # Check exclusions
        for exc in self.exclude_patterns:
            if exc in path or exc in full_url.lower():
                return False

        # Exclude exact base archive URL
        base_path = urlparse(self.base_url).path.lower()
        if path == base_path or path == base_path.rstrip("/"):
            return False

        # Must match article prefix
        matches_prefix = False
        for prefix in self.article_path_prefixes:
            if path.startswith(prefix.lower()):
                matches_prefix = True
                break

        if not matches_prefix:
            return False

        # Ensure path has a post slug beyond just /news/
        # e.g., /news/2026/03/post-title/ or /news/post-title/
        sub_path = path.replace("/news/", "").strip("/")
        if not sub_path:
            return False

        return True

    def discover_article_urls(self, html_content: str) -> Dict[str, Any]:
        """
        Parse HTML and discover ordered candidate article URLs.

        Returns:
            {
                "total_candidate_links_seen": int,
                "discovered_article_urls": List[str],
                "first_10_urls": List[str],
                "duplicates_removed": int,
            }
        """
        if not html_content:
            return {
                "total_candidate_links_seen": 0,
                "discovered_article_urls": [],
                "first_10_urls": [],
                "duplicates_removed": 0,
            }

        soup = BeautifulSoup(html_content, "html.parser")

        candidate_links_count = 0
        seen_urls: Set[str] = set()
        ordered_urls: List[str] = []
        duplicates_removed = 0

        # Primary extraction: Look for semantic <article> containers and entry titles first
        article_elements = soup.find_all(["article", "li", "div"], class_=lambda c: c and any(
            k in str(c).lower() for k in ["post", "entry", "wp-block-post", "article-item"]
        ))

        if not article_elements:
            article_elements = soup.find_all("article")

        # First pass through semantic containers if present
        if article_elements:
            for elem in article_elements:
                # Find headline/title links first
                title_elem = elem.find(["h1", "h2", "h3", "h4"], class_=lambda c: c and any(
                    k in str(c).lower() for k in ["title", "headline", "entry-title"]
                ))
                if not title_elem:
                    title_elem = elem.find(["h1", "h2", "h3", "h4"])

                links_to_check = []
                if title_elem:
                    links_to_check.extend(title_elem.find_all("a", href=True))
                # Also check any links within the element
                links_to_check.extend(elem.find_all("a", href=True))

                for a_tag in links_to_check:
                    href = a_tag.get("href", "").strip()
                    if not href:
                        continue
                    candidate_links_count += 1
                    full_url = urljoin(self.base_url, href)
                    canonical = normalize_article_url(full_url)

                    if self.is_valid_article_link(canonical):
                        if canonical not in seen_urls:
                            seen_urls.add(canonical)
                            ordered_urls.append(canonical)
                        else:
                            duplicates_removed += 1

        # Fallback / Comprehensive pass: Scan all <a> tags in document order if few/none found
        if len(ordered_urls) < 10:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag.get("href", "").strip()
                if not href:
                    continue
                candidate_links_count += 1
                full_url = urljoin(self.base_url, href)
                canonical = normalize_article_url(full_url)

                if self.is_valid_article_link(canonical):
                    if canonical not in seen_urls:
                        seen_urls.add(canonical)
                        ordered_urls.append(canonical)
                    else:
                        duplicates_removed += 1

        return {
            "total_candidate_links_seen": candidate_links_count,
            "discovered_article_urls": ordered_urls,
            "first_10_urls": ordered_urls[:10],
            "duplicates_removed": duplicates_removed,
        }

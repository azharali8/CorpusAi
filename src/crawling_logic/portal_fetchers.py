"""
Portal-specific fetcher and crawler probe construction.
"""

import logging
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.archive_page_discovery import ArchivePageDiscovery
from src.article_inspector import extract_article_metadata
from src.downloader import ResourcePreservationHarvester
from src.portal_state import PortalState
from src.utils.crypto import sha256_bytes


def build_portal_fetchers(
    harvester: ResourcePreservationHarvester,
    logger: logging.Logger,
    warc_content_path: str,
    previous_state: Optional[PortalState] = None,
    heartbeat: Any = None,
) -> Dict[str, Callable[..., Any]]:
    """
    Construct page_fetcher, article_fetcher, and confirmation_probe closures
    for standard WordPress and news portals.
    """

    def page_fetcher(page_url: str) -> Dict[str, Any]:
        if heartbeat is not None and not heartbeat.is_healthy():
            raise RuntimeError(
                f"Heartbeat failure during crawl: {heartbeat.failure_reason}"
            )
        logger.debug(f"Fetching archive page: {page_url}")
        res = harvester.fetch_url(page_url, resource_type="archive_page")
        if res.status_code != 200:
            return {
                "article_urls": [],
                "next_page_url": None,
                "status_code": res.status_code,
                "html": (
                    res.payload.decode("utf-8", errors="replace")
                    if res.payload
                    else ""
                ),
            }

        html = res.payload.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        article_urls: List[str] = []

        # Find post article links
        for post in soup.find_all(class_=lambda c: c and "post" in c.split()):
            link = post.find("a", href=True)
            if link and link.get("href"):
                full_u = urljoin(page_url, link["href"].strip())
                if full_u not in article_urls:
                    article_urls.append(full_u)

        if not article_urls:
            for a in soup.find_all("a", class_=lambda c: c and "post" in c.split()):
                if a.get("href"):
                    full_u = urljoin(page_url, a["href"].strip())
                    if full_u not in article_urls:
                        article_urls.append(full_u)

        if not article_urls:
            discovery = ArchivePageDiscovery(base_url=page_url)
            article_urls = discovery.discover_article_urls(html).get(
                "discovered_article_urls", []
            )

        # Next page extraction
        next_page_url: Optional[str] = None
        next_tag = soup.find(
            "a",
            class_=lambda c: c
            and any(
                k in str(c).lower()
                for k in ["wp-block-query-pagination-next", "pagination-next", "next"]
            ),
        )
        if not next_tag:
            next_tag = soup.find("link", rel="next") or soup.find("a", rel="next")
        if next_tag and next_tag.get("href"):
            next_page_url = urljoin(page_url, next_tag["href"].strip())

        return {
            "article_urls": article_urls,
            "next_page_url": next_page_url,
            "status_code": res.status_code,
            "html": html,
        }

    def article_fetcher(article_url: str) -> Dict[str, Any]:
        if heartbeat is not None and not heartbeat.is_healthy():
            raise RuntimeError(
                f"Heartbeat failure during crawl: {heartbeat.failure_reason}"
            )
        logger.debug(f"Article fetch requested: {article_url}")

        # In incremental mode, reuse prior provenance without live request if already archived
        if previous_state and article_url in previous_state.articles:
            prev_a = previous_state.articles[article_url]
            if (
                prev_a.content_capture_status in ("CAPTURED", "REUSED")
                and prev_a.content_hash
            ):
                logger.debug(f"Reusing prior state provenance for {article_url}")
                return {
                    "title": prev_a.title,
                    "published_at": prev_a.published_at,
                    "author": prev_a.author,
                    "content_hash": prev_a.content_hash,
                    "raw_hash": prev_a.raw_hash,
                    "status": "UNCHANGED",
                    "content_capture_status": "REUSED",
                    "warc_file": prev_a.warc_file,
                    "warc_record_id": prev_a.warc_record_id,
                    "status_code": 200,
                }

        res = harvester.fetch_url(article_url, resource_type="html")
        if res.status_code != 200:
            return {
                "status_code": res.status_code,
                "status": "FETCH_FAILED" if res.status_code >= 500 else "REMOVED",
            }

        art_html = res.payload.decode("utf-8", errors="replace")
        meta = extract_article_metadata(art_html, base_url=article_url)
        content_hash = sha256_bytes(art_html.encode("utf-8"))

        return {
            "status_code": res.status_code,
            "title": meta.get("heading")
            or meta.get("title")
            or f"Article {article_url}",
            "published_at": meta.get("publication_date"),
            "author": meta.get("author"),
            "content_hash": content_hash,
            "raw_hash": content_hash,
            "status": "NEW",
            "content_capture_status": "CAPTURED",
            "warc_file": warc_content_path,
            "warc_record_id": f"<urn:uuid:{sha256_bytes(article_url.encode('utf-8'))[:16]}>",
            "metadata": meta,
        }

    def confirmation_probe(probe_url: str) -> Dict[str, Any]:
        logger.debug(f"Confirmation probe for candidate: {probe_url}")
        res = harvester.fetch_url(probe_url, resource_type="html")
        return {"status_code": res.status_code}

    return {
        "page_fetcher": page_fetcher,
        "article_fetcher": article_fetcher,
        "confirmation_probe": confirmation_probe,
    }

"""
Resource Downloader and Archival Asset Harvester for CorpusAI.

Provides polite, bounded downloading of HTML pages and supporting static assets
(CSS stylesheets, JavaScript scripts, images, webfonts, and media) with
strict request budgets, content deduplication via SHA-256 digests, and
direct WARC preservation using warcio.
"""

from dataclasses import dataclass, field
import hashlib
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.parse
import urllib.request
import urllib.robotparser

from src.article_inspector import extract_referenced_assets
from src.warc_utils import create_warc_response_file


@dataclass
class DownloadedResource:
    """Represents a downloaded HTTP resource."""
    url: str
    status_code: int
    content_type: str
    payload: bytes
    sha256: str
    headers: Dict[str, str] = field(default_factory=dict)
    resource_type: str = "asset"
    fetch_duration: float = 0.0
    error: Optional[str] = None


class ResourcePreservationHarvester:
    """
    Polite, bounded resource harvester that fetches an HTML document and its
    referenced assets, storing them as distinguishable records in a WARC file.
    """

    def __init__(
        self,
        user_agent: str = "CorpusAI-Research-Validator/1.0 (+https://github.com/CorpusAI; Academic Research Evaluation)",
        delay_seconds: float = 1.0,
        max_asset_requests: int = 50,
        timeout: float = 15.0,
    ):
        self.user_agent = user_agent
        self.delay_seconds = delay_seconds
        self.max_asset_requests = max_asset_requests
        self.timeout = timeout
        self.downloaded_resources: Dict[str, DownloadedResource] = {}
        self.request_log: List[Dict[str, Any]] = []

    def fetch_url(self, url: str, resource_type: str = "asset") -> DownloadedResource:
        """Fetch a single URL politely and record response."""
        time.sleep(self.delay_seconds)
        t0 = time.time()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                content_bytes = resp.read()
                content_type = resp.headers.get("Content-Type", "application/octet-stream")
                headers = dict(resp.headers.items())
                duration = round(time.time() - t0, 3)
                digest = hashlib.sha256(content_bytes).hexdigest()

                res = DownloadedResource(
                    url=url,
                    status_code=status,
                    content_type=content_type,
                    payload=content_bytes,
                    sha256=digest,
                    headers=headers,
                    resource_type=resource_type,
                    fetch_duration=duration,
                )
                self.downloaded_resources[url] = res
                self.request_log.append({
                    "url": url,
                    "status_code": status,
                    "duration_seconds": duration,
                    "bytes": len(content_bytes),
                    "resource_type": resource_type,
                })
                return res
        except urllib.error.HTTPError as e:
            duration = round(time.time() - t0, 3)
            res = DownloadedResource(
                url=url,
                status_code=e.code,
                content_type=e.headers.get("Content-Type", "text/plain") if e.headers else "text/plain",
                payload=e.read() if hasattr(e, "read") else b"",
                sha256="",
                resource_type=resource_type,
                fetch_duration=duration,
                error=str(e),
            )
            self.downloaded_resources[url] = res
            self.request_log.append({
                "url": url,
                "status_code": e.code,
                "error": str(e),
                "duration_seconds": duration,
                "resource_type": resource_type,
            })
            return res
        except Exception as e:
            duration = round(time.time() - t0, 3)
            res = DownloadedResource(
                url=url,
                status_code=0,
                content_type="text/plain",
                payload=b"",
                sha256="",
                resource_type=resource_type,
                fetch_duration=duration,
                error=str(e),
            )
            self.downloaded_resources[url] = res
            self.request_log.append({
                "url": url,
                "error": str(e),
                "duration_seconds": duration,
                "resource_type": resource_type,
            })
            return res

    def harvest_page_and_assets(
        self,
        page_url: str,
        html_content: Optional[str] = None,
        asset_types: Optional[Set[str]] = None,
    ) -> List[DownloadedResource]:
        """
        Harvest a page and all its supporting assets (CSS, JS, images, fonts).
        If html_content is not provided, page_url is fetched first.
        """
        results: List[DownloadedResource] = []
        if html_content is None:
            page_res = self.fetch_url(page_url, resource_type="html")
            results.append(page_res)
            if page_res.status_code != 200:
                return results
            html_content = page_res.payload.decode("utf-8", errors="replace")

        # Discover referenced assets
        discovered = extract_referenced_assets(html_content, page_url=page_url)
        allowed_types = asset_types or {"css", "javascript", "image", "font"}

        for item in discovered:
            if len(self.request_log) >= self.max_asset_requests:
                break
            a_url = item["url"]
            a_type = item["resource_type"]
            if a_type in allowed_types and a_url not in self.downloaded_resources:
                res = self.fetch_url(a_url, resource_type=a_type)
                results.append(res)

        return results

    def save_to_warc(
        self,
        warc_path: str,
        resources: Optional[List[DownloadedResource]] = None,
        gzip: bool = True,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Save harvested resources to a WARC file.
        Distinguishes primary content records from supporting asset records
        via custom WARC headers (WARC-Target-Role).
        """
        items_to_save = resources or list(self.downloaded_resources.values())
        warc_records = []
        for r in items_to_save:
            if r.status_code == 0 or not r.payload:
                continue
            role = "primary-content" if r.resource_type == "html" else f"supporting-asset-{r.resource_type}"
            warc_records.append({
                "uri": r.url,
                "status_code": r.status_code,
                "content_type": r.content_type,
                "payload": r.payload,
                "custom_warc_headers": {
                    "WARC-Target-Role": role,
                    "WARC-Payload-Digest": f"sha256:{r.sha256}",
                },
            })

        return create_warc_response_file(warc_path, warc_records, gzip=gzip)

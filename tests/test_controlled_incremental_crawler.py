"""
Controlled Local HTTP Portal and Integration Tests for End-to-End Incremental Crawling.

Exercises the real crawl, download, state persistence, catalog, and WARC-writing pipeline
using a controllable local loopback HTTP server.

Scenarios tested:
1. Initial baseline: 2 archive pages, 4 articles, state and WARC records created.
2. Unchanged rerun: head sentinel matches, 0 article-body downloads, prior WARC provenance reused.
3. Changed article body: detectable via revalidation or metadata, new body downloaded & preserved in new WARC.
4. New article: discovered at head, fetched, boundary traversal stops cleanly.
5. Confirmed removal: article removed from listing, confirmed via targeted 404 probe without deleting prior records.
6. Transient failure & recovery: single article returns HTTP 500 on run N, recovered on run N+1.
7. Budget exhaustion: partial traversal recorded cleanly without false success classification.
"""

import http.server
import json
import os
import socketserver
import threading
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

import pytest
from warcio.archiveiterator import ArchiveIterator

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.downloader import DownloadedResource, ResourcePreservationHarvester
from src.incremental_archive_policy import (
    IncrementalArchivePolicy,
    IncrementalCrawlBudget,
)
from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.run_comparison import IncrementalDiff, RunComparisonEngine


class ControlledPortalHandler(http.server.BaseHTTPRequestHandler):
    """
    Controllable loopback HTTP server that serves configurable archive pages and articles.
    Records every request received with method, path, and headers.
    """
    portal_state_data: Dict[str, Any] = {}
    request_log: List[Dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default server stderr logging

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        log_entry = {
            "method": "GET",
            "path": path,
            "headers": dict(self.headers.items()),
        }
        self.request_log.append(log_entry)

        # Check if route exists in portal_state_data
        if path in self.portal_state_data:
            item = self.portal_state_data[path]
            status = item.get("status", 200)
            if status != 200:
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"Error")
                return

            content_type = item.get("content_type", "text/html; charset=utf-8")
            body = item.get("body", "")
            if isinstance(body, str):
                body = body.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if "etag" in item:
                self.send_header("ETag", item["etag"])
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


class ControlledPortalServer:
    """Helper to start and stop the ControlledPortalHandler on an ephemeral port."""
    def __init__(self, routes: Dict[str, Any]):
        self.routes = routes
        self.server: Optional[socketserver.TCPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0
        self.request_log: List[Dict[str, Any]] = []

    def start(self) -> str:
        routes = self.routes
        req_log = self.request_log

        class BoundHandler(ControlledPortalHandler):
            portal_state_data = routes
            request_log = req_log

        self.server = socketserver.TCPServer(("127.0.0.1", 0), BoundHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def update_routes(self, new_routes: Dict[str, Any]) -> None:
        self.routes.clear()
        self.routes.update(new_routes)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None

    def __enter__(self) -> "ControlledPortalServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


def _read_warc_records(warc_path: str) -> List[Dict[str, Any]]:
    """Helper to read records from a WARC file using warcio."""
    records = []
    with open(warc_path, "rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                uri = record.rec_headers.get_header("WARC-Target-URI")
                rec_id = record.rec_headers.get_header("WARC-Record-ID")
                role = record.rec_headers.get_header("WARC-Target-Role")
                payload = record.raw_stream.read()
                records.append({
                    "uri": uri,
                    "record_id": rec_id,
                    "role": role,
                    "payload": payload,
                })
    return records


# ===========================================================================
# Controlled End-to-End Test Suite
# ===========================================================================

def test_controlled_initial_baseline_and_unchanged_rerun(tmp_path):
    """
    Scenario 1 & 2:
    - Run 1 (Baseline): Crawl 2 archive pages, fetch 4 articles, persist state & WARC.
    - Run 2 (Unchanged Rerun): Page 1 head sentinel matches, 0 article requests made,
      prior WARC record provenance carried forward.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
                <a class="next" href="/news/page/2">Next</a>
            </body></html>"""
        },
        "/news/page/2": {
            "body": """<html><body>
                <a class="post" href="/news/post-3">Post 3</a>
                <a class="post" href="/news/post-4">Post 4</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1 Content</h1><p>Body 1</p></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2 Content</h1><p>Body 2</p></body></html>"},
        "/news/post-3": {"body": "<html><body><h1>Post 3 Content</h1><p>Body 3</p></body></html>"},
        "/news/post-4": {"body": "<html><body><h1>Post 4 Content</h1><p>Body 4</p></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester = ResourcePreservationHarvester(delay_seconds=0.0)

        # Build fetchers using harvester
        def page_fetcher(page_url: str) -> Dict[str, Any]:
            res = harvester.fetch_url(page_url, resource_type="archive_page")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            posts = [urllib.parse.urljoin(page_url, a["href"]) for a in soup.find_all("a", class_="post")]
            next_a = soup.find("a", class_="next")
            next_url = urllib.parse.urljoin(page_url, next_a["href"]) if next_a else None
            return {
                "status_code": res.status_code,
                "article_urls": posts,
                "next_page_url": next_url,
                "html": res.payload.decode("utf-8"),
            }

        warc_path_run1 = str(tmp_path / "run1_content.warc.gz")

        def article_fetcher_run1(art_url: str) -> Dict[str, Any]:
            res = harvester.fetch_url(art_url, resource_type="html")
            import hashlib
            content_hash = hashlib.sha256(res.payload).hexdigest()
            return {
                "title": f"Title for {art_url}",
                "content_hash": content_hash,
                "raw_hash": content_hash,
                "warc_file": warc_path_run1,
                "warc_record_id": f"<urn:uuid:run1-{os.path.basename(art_url)}>",
                "content_capture_status": "CAPTURED",
            }

        # --- RUN 1: Baseline ---
        policy_run1 = IncrementalArchivePolicy(
            portal_id="controlled-portal",
            head_page_url=f"{base_url}/news/page/1",
        )
        state1, run1, diff1 = policy_run1.execute_incremental_crawl(
            page_fetcher=page_fetcher,
            article_fetcher=article_fetcher_run1,
        )

        # Save WARC for run 1
        harvester.save_to_warc(warc_path_run1)
        assert os.path.exists(warc_path_run1)

        # Assertions on Run 1
        assert len(state1.articles) == 4
        assert len(diff1.new_articles) == 4
        assert run1.mode == "BASELINE"
        # 2 pages + 4 articles = 6 requests
        assert policy_run1.requests_made == 6
        for u, art in state1.articles.items():
            assert art.content_capture_status == "CAPTURED"
            assert art.warc_file == warc_path_run1
            assert art.warc_record_id is not None

        # Verify WARC records readable
        records_run1 = _read_warc_records(warc_path_run1)
        assert len(records_run1) >= 4

        # --- RUN 2: Unchanged Rerun ---
        portal.request_log.clear()
        harvester_run2 = ResourcePreservationHarvester(delay_seconds=0.0)

        def page_fetcher_run2(page_url: str) -> Dict[str, Any]:
            res = harvester_run2.fetch_url(page_url, resource_type="archive_page")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            posts = [urllib.parse.urljoin(page_url, a["href"]) for a in soup.find_all("a", class_="post")]
            next_a = soup.find("a", class_="next")
            next_url = urllib.parse.urljoin(page_url, next_a["href"]) if next_a else None
            return {
                "status_code": res.status_code,
                "article_urls": posts,
                "next_page_url": next_url,
                "html": res.payload.decode("utf-8"),
            }

        policy_run2 = IncrementalArchivePolicy(
            portal_id="controlled-portal",
            head_page_url=f"{base_url}/news/page/1",
            previous_state=state1,
        )
        state2, run2, diff2 = policy_run2.execute_incremental_crawl(
            page_fetcher=page_fetcher_run2,
            article_fetcher=None,  # Not even needed when head sentinel is identical
        )

        # Assertions on Run 2
        assert policy_run2.requests_made == 1  # Only page 1 requested
        assert len(portal.request_log) == 1
        assert portal.request_log[0]["path"] == "/news/page/1"
        assert len(diff2.new_articles) == 0
        assert len(diff2.unchanged_articles) == 2
        assert len(diff2.not_observed_in_scope) == 2  # Post 3 and 4 were past head boundary
        assert run2.boundary_status == "BOUNDARY_RECONCILED"

        # Verify prior WARC record provenance was carried forward
        for u in diff2.unchanged_articles:
            art = state2.articles[u]
            assert art.content_capture_status == "REUSED"
            assert art.warc_file == warc_path_run1
            assert art.warc_record_id == state1.articles[u].warc_record_id
            assert art.content_hash == state1.articles[u].content_hash


def test_controlled_new_article_at_head(tmp_path):
    """
    Scenario 3:
    - Baseline: Post 1, Post 2 on page 1.
    - Run 2: New Post 0 appears at head of page 1, Post 2 pushed to page 2.
    - Policy detects head changed, fetches new Post 0, reconciles Post 1 and 2,
      stops traversal without false removal.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1</h1></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2</h1></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester = ResourcePreservationHarvester(delay_seconds=0.0)

        def make_page_fetcher(h):
            def pf(page_url: str):
                res = h.fetch_url(page_url, resource_type="archive_page")
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
                posts = [urllib.parse.urljoin(page_url, a["href"]) for a in soup.find_all("a", class_="post")]
                next_a = soup.find("a", class_="next")
                next_url = urllib.parse.urljoin(page_url, next_a["href"]) if next_a else None
                return {"status_code": res.status_code, "article_urls": posts, "next_page_url": next_url, "html": res.payload.decode("utf-8")}
            return pf

        def make_art_fetcher(h, w_path, prev_state=None):
            def af(art_url: str):
                res = h.fetch_url(art_url, resource_type="html")
                import hashlib
                h_val = hashlib.sha256(res.payload).hexdigest()
                prev_a = prev_state.articles.get(art_url) if prev_state else None
                if prev_a and prev_a.content_hash == h_val:
                    status = "REUSED"
                else:
                    status = "CAPTURED"
                return {
                    "title": art_url,
                    "content_hash": h_val,
                    "raw_hash": h_val,
                    "warc_file": w_path,
                    "warc_record_id": f"<urn:{art_url}>",
                    "content_capture_status": status,
                }
            return af

        warc1 = str(tmp_path / "base.warc.gz")
        policy1 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1")
        state1, _, _ = policy1.execute_incremental_crawl(make_page_fetcher(harvester), make_art_fetcher(harvester, warc1))
        harvester.save_to_warc(warc1)

        # Mutate portal: add Post 0 at head
        portal.update_routes({
            "/news/page/1": {
                "body": """<html><body>
                    <a class="post" href="/news/post-0">Post 0 (New)</a>
                    <a class="post" href="/news/post-1">Post 1</a>
                    <a class="next" href="/news/page/2">Next</a>
                </body></html>"""
            },
            "/news/page/2": {
                "body": """<html><body>
                    <a class="post" href="/news/post-2">Post 2 (Pushed)</a>
                </body></html>"""
            },
            "/news/post-0": {"body": "<html><body><h1>Post 0 Body</h1></body></html>"},
            "/news/post-1": {"body": "<html><body><h1>Post 1</h1></body></html>"},
            "/news/post-2": {"body": "<html><body><h1>Post 2</h1></body></html>"},
        })

        harvester2 = ResourcePreservationHarvester(delay_seconds=0.0)
        warc2 = str(tmp_path / "inc.warc.gz")
        policy2 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1", previous_state=state1)
        state2, run2, diff2 = policy2.execute_incremental_crawl(make_page_fetcher(harvester2), make_art_fetcher(harvester2, warc2, prev_state=state1))

        assert len(diff2.new_articles) == 1
        assert f"{base_url}/news/post-0" in diff2.new_articles
        assert len(diff2.missing_candidates) == 0
        assert state2.articles[f"{base_url}/news/post-0"].content_capture_status == "CAPTURED"
        assert state2.articles[f"{base_url}/news/post-1"].content_capture_status == "REUSED"


def test_controlled_confirmed_removal_with_targeted_probe(tmp_path):
    """
    Scenario 5:
    - An article disappears from listing and returns HTTP 404 upon confirmation probe.
    - Policy records status='REMOVED', content_capture_status='REMOVED' without deleting prior state or WARC.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1</h1></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2</h1></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester = ResourcePreservationHarvester(delay_seconds=0.0)

        def pf(u):
            res = harvester.fetch_url(u)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {"status_code": 200, "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")], "next_page_url": None}

        def af(u):
            res = harvester.fetch_url(u)
            import hashlib
            return {"title": u, "content_hash": hashlib.sha256(res.payload).hexdigest(), "warc_file": "base.warc.gz", "warc_record_id": "<urn:base>"}

        policy1 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1")
        state1, _, _ = policy1.execute_incremental_crawl(pf, af)

        # Mutate portal: Post 2 is removed from listing and returns 404
        portal.update_routes({
            "/news/page/1": {
                "body": """<html><body>
                    <a class="post" href="/news/post-1">Post 1</a>
                </body></html>"""
            },
            "/news/post-1": {"body": "<html><body><h1>Post 1</h1></body></html>"},
            "/news/post-2": {"status": 404},
        })

        portal.request_log.clear()

        # Confirmation probe function
        def confirmation_probe(art_url: str):
            res = harvester.fetch_url(art_url)
            return {"status_code": res.status_code}

        policy2 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1", previous_state=state1)
        state2, run2, diff2 = policy2.execute_incremental_crawl(
            page_fetcher=pf,
            article_fetcher=af,
            confirmation_probe=confirmation_probe,
        )

        assert f"{base_url}/news/post-2" in diff2.confirmed_removed
        assert state2.articles[f"{base_url}/news/post-2"].status == "REMOVED"
        assert state2.articles[f"{base_url}/news/post-2"].content_capture_status == "REMOVED"
        # Prior WARC pointer is preserved rather than wiped
        assert state2.articles[f"{base_url}/news/post-2"].warc_file == "base.warc.gz"
        assert state2.articles[f"{base_url}/news/post-2"].warc_record_id == "<urn:base>"


def test_controlled_transient_failure_and_recovery(tmp_path):
    """
    Scenario 6:
    - Run 1 (Successful Baseline): Crawl Post 1 and Post 2 successfully.
      Register as successful baseline in catalog, recording baseline run ID, state, and WARC records.
    - Run 2 (Failed Incremental Run): Post 3 appears at head. Fetching Post 3 returns HTTP 500.
      Register as unsuccessful in catalog.
      Verify:
        - catalog.latest_successful_run_id remains exactly equal to baseline run ID.
        - Baseline state and WARC payloads remain intact and retrievable.
        - Post 3 is recorded with FETCH_FAILED / FAILED status for subsequent retry.
        - Previously captured unchanged article bodies (Post 1, Post 2) are NOT refetched.
    - Run 3 (Recovery Incremental Run): Post 3 recovers to HTTP 200.
      Incremental crawl retries Post 3, succeeds, preserves payload in new WARC.
      Catalog advances latest_successful_run_id to Run 3 only upon success.
    - Fixture request accounting verified across all 3 runs.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1 Body</h1></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2 Body</h1></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        cat_file = str(tmp_path / "catalog.json")
        catalog = ArchiveCatalog.load_or_create(cat_file, portal_id="portal")

        # ---------------------------------------------------------
        # RUN 1: Successful Baseline
        # ---------------------------------------------------------
        harvester1 = ResourcePreservationHarvester(delay_seconds=0.0)

        def pf1(u):
            res = harvester1.fetch_url(u, resource_type="archive_page")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {
                "status_code": res.status_code,
                "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")],
                "next_page_url": None,
                "html": res.payload.decode("utf-8"),
            }

        warc1_path = str(tmp_path / "baseline.warc.gz")

        def af1(u):
            res = harvester1.fetch_url(u, resource_type="html")
            import hashlib
            h_val = hashlib.sha256(res.payload).hexdigest()
            return {
                "title": f"Title for {u}",
                "content_hash": h_val,
                "raw_hash": h_val,
                "warc_file": warc1_path,
                "warc_record_id": f"<urn:baseline:{os.path.basename(u)}>",
                "content_capture_status": "CAPTURED",
            }

        policy1 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1")
        state1, run1, diff1 = policy1.execute_incremental_crawl(pf1, af1)
        harvester1.save_to_warc(warc1_path)

        # Baseline request accounting: 1 page + 2 articles = 3 requests
        assert policy1.requests_made == 3
        assert len(portal.request_log) == 3
        req_paths_run1 = [r["path"] for r in portal.request_log]
        assert req_paths_run1 == ["/news/page/1", "/news/post-1", "/news/post-2"]

        entry1 = CatalogRunEntry(
            run_id=run1.run_id,
            previous_run_id=None,
            mode=run1.mode,
            started_at=run1.started_at,
            completed_at=run1.completed_at,
            status="CONVERGED",
            article_count=len(state1.articles),
            new_article_count=len(diff1.new_articles),
            changed_article_count=len(diff1.changed_articles),
            run_dir=str(tmp_path / "run1"),
        )
        catalog.register_run(entry1, is_successful=True)
        baseline_successful_run_id = run1.run_id
        assert catalog.latest_successful_run_id == baseline_successful_run_id

        # Verify baseline WARC records retrievable
        warc1_records = _read_warc_records(warc1_path)
        assert len(warc1_records) >= 2
        warc1_uris = {r["uri"] for r in warc1_records}
        assert f"{base_url}/news/post-1" in warc1_uris
        assert f"{base_url}/news/post-2" in warc1_uris

        # ---------------------------------------------------------
        # RUN 2: Failed Incremental Run (New Post 3 returns 500)
        # ---------------------------------------------------------
        portal.update_routes({
            "/news/page/1": {
                "body": """<html><body>
                    <a class="post" href="/news/post-3">Post 3 (New)</a>
                    <a class="post" href="/news/post-1">Post 1</a>
                    <a class="post" href="/news/post-2">Post 2</a>
                </body></html>"""
            },
            "/news/post-1": {"body": "<html><body><h1>Post 1 Body</h1></body></html>"},
            "/news/post-2": {"body": "<html><body><h1>Post 2 Body</h1></body></html>"},
            "/news/post-3": {"status": 500},  # Transient failure on new article
        })
        portal.request_log.clear()

        harvester2 = ResourcePreservationHarvester(delay_seconds=0.0)

        def pf2(u):
            res = harvester2.fetch_url(u, resource_type="archive_page")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {
                "status_code": res.status_code,
                "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")],
                "next_page_url": None,
                "html": res.payload.decode("utf-8"),
            }

        def af2_failing(u):
            # Only fetch new/unarchived items or if revalidation required
            res = harvester2.fetch_url(u, resource_type="html")
            if res.status_code != 200:
                raise RuntimeError(f"HTTP {res.status_code} on {u}")
            import hashlib
            h_val = hashlib.sha256(res.payload).hexdigest()
            return {
                "title": f"Title for {u}",
                "content_hash": h_val,
                "raw_hash": h_val,
                "warc_file": str(tmp_path / "run2.warc.gz"),
                "warc_record_id": f"<urn:run2:{os.path.basename(u)}>",
                "content_capture_status": "CAPTURED",
            }

        # Safe article fetcher for incremental run: articles successfully archived in baseline
        # are reused without issuing a network request.  Baseline articles have status="NEW"
        # (first-ever capture), so the reliable reuse predicate is content_capture_status in
        # ("CAPTURED", "REUSED") rather than status == "UNCHANGED".
        def incremental_art_fetcher_run2(u):
            prev_a = state1.articles.get(u)
            if prev_a and prev_a.content_capture_status in ("CAPTURED", "REUSED") and prev_a.content_hash:
                # Reuse without downloading article body
                return {
                    "title": prev_a.title,
                    "content_hash": prev_a.content_hash,
                    "raw_hash": prev_a.raw_hash,
                    "warc_file": prev_a.warc_file,
                    "warc_record_id": prev_a.warc_record_id,
                    "content_capture_status": "REUSED",
                }
            return af2_failing(u)

        policy2 = IncrementalArchivePolicy(
            portal_id="portal",
            head_page_url=f"{base_url}/news/page/1",
            previous_state=state1,
        )
        state2, run2, diff2 = policy2.execute_incremental_crawl(
            page_fetcher=pf2,
            article_fetcher=incremental_art_fetcher_run2,
        )

        # Requests made during Run 2: exactly 1 archive page + 1 article attempt (Post 3 returning 500)
        # Post 1 and Post 2 were reused without network download!
        assert len(portal.request_log) == 2
        req_paths_run2 = [r["path"] for r in portal.request_log]
        assert req_paths_run2 == ["/news/page/1", "/news/post-3"]

        # Post 3 is registered with failure state
        post3_url = f"{base_url}/news/post-3"
        assert state2.articles[post3_url].status == "FETCH_FAILED"
        assert state2.articles[post3_url].content_capture_status == "FAILED"

        # Register Run 2 as failed in catalog
        entry2 = CatalogRunEntry(
            run_id=run2.run_id,
            previous_run_id=baseline_successful_run_id,
            mode=run2.mode,
            started_at=run2.started_at,
            completed_at=run2.completed_at,
            status="PARTIAL_FAILURE",
            article_count=len(state2.articles),
            new_article_count=len(diff2.new_articles),
            changed_article_count=len(diff2.changed_articles),
            run_dir=str(tmp_path / "run2"),
        )
        catalog.register_run(entry2, is_successful=False)

        # CRUCIAL ASSERTION: catalog latest_successful_run_id remains exactly equal to baseline
        assert catalog.latest_successful_run_id == baseline_successful_run_id

        # Verify baseline state and WARC payloads remain intact and retrievable
        warc1_records_recheck = _read_warc_records(warc1_path)
        assert len(warc1_records_recheck) >= 2
        assert state1.articles[f"{base_url}/news/post-1"].warc_file == warc1_path

        # ---------------------------------------------------------
        # RUN 3: Recovery Incremental Run (Post 3 recovers to 200)
        # ---------------------------------------------------------
        portal.update_routes({
            "/news/page/1": {
                "body": """<html><body>
                    <a class="post" href="/news/post-3">Post 3 (Recovered)</a>
                    <a class="post" href="/news/post-1">Post 1</a>
                    <a class="post" href="/news/post-2">Post 2</a>
                </body></html>"""
            },
            "/news/post-1": {"body": "<html><body><h1>Post 1 Body</h1></body></html>"},
            "/news/post-2": {"body": "<html><body><h1>Post 2 Body</h1></body></html>"},
            "/news/post-3": {"body": "<html><body><h1>Post 3 Recovered Body</h1></body></html>"},
        })
        portal.request_log.clear()

        harvester3 = ResourcePreservationHarvester(delay_seconds=0.0)
        warc3_path = str(tmp_path / "recovery.warc.gz")

        def pf3(u):
            res = harvester3.fetch_url(u, resource_type="archive_page")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {
                "status_code": res.status_code,
                "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")],
                "next_page_url": None,
                "html": res.payload.decode("utf-8"),
            }

        def af3_recovered(u):
            # For Run 3, look up reuse eligibility from state2 (the previous state passed to
            # policy3).  Articles successfully carried over there have content_capture_status
            # in ("CAPTURED","REUSED") and do not need another network round-trip.
            prev_a = state2.articles.get(u)
            if prev_a and prev_a.content_capture_status in ("CAPTURED", "REUSED") and prev_a.content_hash:
                return {
                    "title": prev_a.title,
                    "content_hash": prev_a.content_hash,
                    "raw_hash": prev_a.raw_hash,
                    "warc_file": prev_a.warc_file,
                    "warc_record_id": prev_a.warc_record_id,
                    "content_capture_status": "REUSED",
                }
            # Otherwise (e.g. Post 3 which failed in Run 2), fetch from server
            res = harvester3.fetch_url(u, resource_type="html")
            import hashlib
            h_val = hashlib.sha256(res.payload).hexdigest()
            return {
                "title": f"Title for {u}",
                "content_hash": h_val,
                "raw_hash": h_val,
                "warc_file": warc3_path,
                "warc_record_id": f"<urn:recovery:{os.path.basename(u)}>",
                "content_capture_status": "CAPTURED",
            }

        policy3 = IncrementalArchivePolicy(
            portal_id="portal",
            head_page_url=f"{base_url}/news/page/1",
            previous_state=state2,  # Feed state2 which tracked Post 3 failure
        )
        state3, run3, diff3 = policy3.execute_incremental_crawl(
            page_fetcher=pf3,
            article_fetcher=af3_recovered,
        )
        harvester3.save_to_warc(warc3_path)

        # Requests in Run 3: 1 archive page + exactly 1 article download (Post 3)
        # Unchanged Post 1 and Post 2 were again not downloaded
        assert len(portal.request_log) == 2
        req_paths_run3 = [r["path"] for r in portal.request_log]
        assert req_paths_run3 == ["/news/page/1", "/news/post-3"]

        # Post 3 is now successfully captured
        assert state3.articles[post3_url].content_capture_status == "CAPTURED"
        assert state3.articles[post3_url].warc_file == warc3_path
        assert state3.articles[f"{base_url}/news/post-1"].content_capture_status == "REUSED"
        assert state3.articles[f"{base_url}/news/post-2"].content_capture_status == "REUSED"

        # Verify Post 3 payload exists in recovery WARC
        warc3_records = _read_warc_records(warc3_path)
        assert post3_url in [r["uri"] for r in warc3_records]

        # Register Run 3 as successful in catalog
        entry3 = CatalogRunEntry(
            run_id=run3.run_id,
            previous_run_id=baseline_successful_run_id,
            mode=run3.mode,
            started_at=run3.started_at,
            completed_at=run3.completed_at,
            status="CONVERGED",
            article_count=len(state3.articles),
            new_article_count=len(diff3.new_articles),
            changed_article_count=len(diff3.changed_articles),
            run_dir=str(tmp_path / "run3"),
        )
        catalog.register_run(entry3, is_successful=True)

        # CRUCIAL ASSERTION: Catalog advances to Run 3 only upon success
        assert catalog.latest_successful_run_id == run3.run_id
        assert catalog.latest_successful_run_id != baseline_successful_run_id

def test_controlled_changed_article_body(tmp_path):
    """
    Scenario 4:
    - Baseline: Post 1, Post 2. Both archived with original content hash.
    - Run 2: Post 1 body is modified on the server (updated paragraphs).
    - When revalidated, policy detects CONTENT_CHANGED, records the new hash,
      and writes the new article body into run 2's WARC file.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Original Post 1</h1></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Original Post 2</h1></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester1 = ResourcePreservationHarvester(delay_seconds=0.0)

        def pf1(u):
            res = harvester1.fetch_url(u)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {"status_code": 200, "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")], "next_page_url": None}

        warc1 = str(tmp_path / "run1.warc.gz")

        def af1(u):
            res = harvester1.fetch_url(u)
            import hashlib
            h_val = hashlib.sha256(res.payload).hexdigest()
            return {"title": u, "content_hash": h_val, "raw_hash": h_val, "warc_file": warc1, "warc_record_id": f"<urn:run1:{u}>"}

        policy1 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1")
        state1, run1, diff1 = policy1.execute_incremental_crawl(pf1, af1)
        harvester1.save_to_warc(warc1)

        # Mutate portal: Post 1 content body changed
        portal.update_routes({
            "/news/page/1": {
                "body": """<html><body>
                    <a class="post" href="/news/post-1">Post 1</a>
                    <a class="post" href="/news/post-2">Post 2</a>
                </body></html>"""
            },
            "/news/post-1": {"body": "<html><body><h1>Updated Post 1 with New Content Body</h1></body></html>"},
            "/news/post-2": {"body": "<html><body><h1>Original Post 2</h1></body></html>"},
        })

        harvester2 = ResourcePreservationHarvester(delay_seconds=0.0)
        warc2 = str(tmp_path / "run2.warc.gz")

        def pf2(u):
            res = harvester2.fetch_url(u)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            return {"status_code": 200, "article_urls": [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")], "next_page_url": None}

        def af2(u):
            res = harvester2.fetch_url(u)
            import hashlib
            h_val = hashlib.sha256(res.payload).hexdigest()
            return {"title": u, "content_hash": h_val, "raw_hash": h_val, "warc_file": warc2, "warc_record_id": f"<urn:run2:{u}>"}

        policy2 = IncrementalArchivePolicy(portal_id="portal", head_page_url=f"{base_url}/news/page/1", previous_state=state1)
        state2, run2, diff2 = policy2.execute_incremental_crawl(pf2, af2)
        harvester2.save_to_warc(warc2)

        # Check diff and state
        post1_url = f"{base_url}/news/post-1"
        post2_url = f"{base_url}/news/post-2"

        assert post1_url in diff2.changed_articles
        assert post2_url in diff2.unchanged_articles
        assert state2.articles[post1_url].status == "CONTENT_CHANGED"
        assert state2.articles[post1_url].content_capture_status == "CAPTURED"
        assert state2.articles[post1_url].warc_file == warc2
        assert state2.articles[post1_url].content_hash != state1.articles[post1_url].content_hash

        # Verify WARC 2 contains new record for Post 1
        records_warc2 = _read_warc_records(warc2)
        target_uris = [r["uri"] for r in records_warc2]
        assert post1_url in target_uris


def test_controlled_budget_exhaustion(tmp_path):
    """
    Scenario 7:
    - Portal has 3 archive pages.
    - IncrementalCrawlBudget has max_requests=2.
    - Policy halts before reaching page 3, marks budget_exhausted=True,
      records last_potentially_incomplete_page, and sets convergence to BUDGET_EXHAUSTED.
    """
    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="next" href="/news/page/2">Next</a>
            </body></html>"""
        },
        "/news/page/2": {
            "body": """<html><body>
                <a class="post" href="/news/post-2">Post 2</a>
                <a class="next" href="/news/page/3">Next</a>
            </body></html>"""
        },
        "/news/page/3": {
            "body": """<html><body>
                <a class="post" href="/news/post-3">Post 3</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1</h1></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2</h1></body></html>"},
        "/news/post-3": {"body": "<html><body><h1>Post 3</h1></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester = ResourcePreservationHarvester(delay_seconds=0.0)

        def pf(u):
            res = harvester.fetch_url(u)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            posts = [urllib.parse.urljoin(u, a["href"]) for a in soup.find_all("a", class_="post")]
            next_a = soup.find("a", class_="next")
            next_url = urllib.parse.urljoin(u, next_a["href"]) if next_a else None
            return {"status_code": 200, "article_urls": posts, "next_page_url": next_url}

        # Budget allows only 2 requests (will visit page 1, then either article or page 2)
        budget = IncrementalCrawlBudget(max_requests=2, max_archive_pages=2)
        policy = IncrementalArchivePolicy(
            portal_id="portal",
            head_page_url=f"{base_url}/news/page/1",
            budget=budget,
        )

        state, run, diff = policy.execute_incremental_crawl(page_fetcher=pf, article_fetcher=None)

        assert policy.budget_exhausted is True
        assert state.last_potentially_incomplete_page is not None
        assert run.convergence_status == "BUDGET_EXHAUSTED"

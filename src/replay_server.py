"""
CorpusAI Replay Server and Resource Preservation Engine.

Provides:
1. ReplayArchive: unified abstraction over WARC and WACZ files containing
   captured pages and their supporting assets (CSS, JS, images, fonts, media).
2. ArchiveReplayServer: an offline local HTTP server that faithfully serves
   archived content and resources without falling back to the live network.
3. Resource preservation and URL rewriting: rewrites HTML and CSS references
   so that a local browser session resolves resources through the local replay server,
   detecting and explicitly logging missing resources.
4. Live network barrier: strictly guarantees no live HTTP/HTTPS calls occur during replay.
"""

from dataclasses import dataclass, field
import hashlib
import http.server
import io
import json
import logging
import os
import re
import socketserver
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit, urlunsplit
import zipfile

from bs4 import BeautifulSoup
from warcio.archiveiterator import ArchiveIterator

logger = logging.getLogger("corpusai.replay_server")


def normalize_lookup_url(url: str) -> str:
    """
    Normalize URL for lookup in archive indexes:
    - decode percent-encoded characters where appropriate
    - strip fragment (#...)
    - preserve query strings
    - normalize scheme and netloc to lowercase
    """
    if not url:
        return ""
    # Strip fragment
    url_without_frag = url.split("#")[0].strip()
    parsed = urlsplit(url_without_frag)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    # Clean redundant port if standard
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


@dataclass
class ArchivedResource:
    """Metadata and raw payload of an archived HTTP response record."""
    uri: str
    normalized_uri: str
    status_code: int
    content_type: str
    payload: bytes
    headers: Dict[str, str] = field(default_factory=dict)
    record_id: Optional[str] = None
    warc_date: Optional[str] = None
    source_file: str = ""

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        if "text/html" in ct or "application/xhtml" in ct:
            # SVG and other XML documents served with text/html or application/xhtml should be excluded if .svg in URI
            if self.uri.lower().endswith(".svg"):
                return False
            return True
        return False

    @property
    def is_css(self) -> bool:
        return "text/css" in self.content_type.lower() or self.uri.endswith(".css")


class ReplayArchive:
    """
    In-memory index and accessor for archived resources from WARC and WACZ files.
    Supports single or multiple archive files.
    """

    def __init__(self):
        # normalized_url -> ArchivedResource
        self.resources: Dict[str, ArchivedResource] = {}
        # alternative lookups (e.g. without trailing slash, or path only)
        self._url_aliases: Dict[str, str] = {}
        # list of loaded archive paths
        self.source_archives: List[str] = []
        # primary page URLs
        self.page_urls: List[str] = []

    def load_archive(self, file_path: str) -> int:
        """
        Load resources from a .warc, .warc.gz, or .wacz file.
        Returns count of response records indexed.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Archive file not found: {file_path}")

        ext = file_path.lower()
        count = 0
        if ext.endswith(".wacz"):
            count = self._load_wacz(file_path)
        elif ext.endswith(".warc") or ext.endswith(".warc.gz"):
            count = self._load_warc(file_path)
        else:
            raise ValueError(f"Unsupported archive format: {file_path}")

        self.source_archives.append(os.path.abspath(file_path))
        return count

    def _load_warc(self, warc_path: str) -> int:
        count = 0
        with open(warc_path, "rb") as stream:
            for record in ArchiveIterator(stream):
                if record.rec_type == "response":
                    if self._index_warc_record(record, source_file=os.path.basename(warc_path)):
                        count += 1
        return count

    def _load_wacz(self, wacz_path: str) -> int:
        count = 0
        with zipfile.ZipFile(wacz_path, "r") as zf:
            # Check for pages list
            if "pages/pages.jsonl" in zf.namelist():
                try:
                    pages_text = zf.read("pages/pages.jsonl").decode("utf-8", errors="replace")
                    for line in pages_text.strip().split("\n"):
                        if not line.strip():
                            continue
                        try:
                            p_data = json.loads(line)
                            p_url = p_data.get("url")
                            if p_url and p_url not in self.page_urls:
                                self.page_urls.append(p_url)
                        except Exception:
                            pass
                except Exception:
                    pass

            # Iterate over WARCs inside archive/
            for name in zf.namelist():
                if name.startswith("archive/") and (name.endswith(".warc") or name.endswith(".warc.gz")):
                    warc_bytes = zf.read(name)
                    stream = io.BytesIO(warc_bytes)
                    for record in ArchiveIterator(stream):
                        if record.rec_type == "response":
                            if self._index_warc_record(record, source_file=f"{os.path.basename(wacz_path)}!{name}"):
                                count += 1
        return count

    def _index_warc_record(self, record: Any, source_file: str) -> bool:
        uri = record.rec_headers.get_header("WARC-Target-URI")
        if not uri:
            return False

        headers = {}
        status_code = 200
        content_type = "application/octet-stream"
        if record.http_headers:
            status_code = int(record.http_headers.get_statuscode() or 200)
            for k, v in record.http_headers.headers:
                headers[k] = v
            content_type = record.http_headers.get_header("Content-Type", content_type)

        record_id = record.rec_headers.get_header("WARC-Record-ID")
        warc_date = record.rec_headers.get_header("WARC-Date")

        raw_payload = record.raw_stream.read()
        norm_uri = normalize_lookup_url(uri)

        res = ArchivedResource(
            uri=uri,
            normalized_uri=norm_uri,
            status_code=status_code,
            content_type=content_type,
            payload=raw_payload,
            headers=headers,
            record_id=record_id,
            warc_date=warc_date,
            source_file=source_file,
        )

        self.resources[norm_uri] = res

        # Also index alternative variants
        # e.g., with or without trailing slash
        if norm_uri.endswith("/"):
            self._url_aliases[norm_uri[:-1]] = norm_uri
        else:
            self._url_aliases[norm_uri + "/"] = norm_uri

        # Track successful HTML pages
        if res.is_html and res.status_code == 200 and uri not in self.page_urls:
            self.page_urls.append(uri)

        return True

    def get_resource(self, url: str) -> Optional[ArchivedResource]:
        """Look up an archived resource by URL with fallback normalizations."""
        norm = normalize_lookup_url(url)
        if norm in self.resources:
            return self.resources[norm]

        alias = self._url_aliases.get(norm)
        if alias and alias in self.resources:
            return self.resources[alias]

        # Try case-insensitive netloc fallback
        parsed = urlsplit(norm)
        for cand_url, cand_res in self.resources.items():
            cand_parsed = urlsplit(cand_url)
            if (
                cand_parsed.netloc.lower() == parsed.netloc.lower()
                and cand_parsed.path == parsed.path
                and cand_parsed.query == parsed.query
            ):
                return cand_res

        return None

    def add_resource(
        self,
        uri: str,
        payload: bytes,
        content_type: str = "text/html; charset=utf-8",
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        source_file: str = "memory",
    ) -> ArchivedResource:
        """Add an in-memory resource directly into the archive index."""
        norm_uri = normalize_lookup_url(uri)
        res = ArchivedResource(
            uri=uri,
            normalized_uri=norm_uri,
            status_code=status_code,
            content_type=content_type,
            payload=payload,
            headers=headers or {},
            source_file=source_file,
        )
        self.resources[norm_uri] = res
        if norm_uri.endswith("/"):
            self._url_aliases[norm_uri[:-1]] = norm_uri
        else:
            self._url_aliases[norm_uri + "/"] = norm_uri

        if res.is_html and res.status_code == 200 and uri not in self.page_urls:
            self.page_urls.append(uri)
        return res

    def load_portal_state(self, portal_state_path: str) -> int:
        """
        Inspect a portal_state.json snapshot and index articles that have actual
        captured HTML payloads stored in their metadata (e.g. raw_html or full_html).
        Articles without stored HTML payloads are NOT fabricated as synthetic HTML pages.
        """
        if not os.path.exists(portal_state_path):
            raise FileNotFoundError(f"Portal state file not found: {portal_state_path}")

        count = 0
        with open(portal_state_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        articles = data.get("articles", {})
        for art_url, art_info in articles.items():
            meta = art_info.get("metadata") or {}
            # Only index if actual HTML content was preserved in metadata
            html_content = meta.get("raw_html") or meta.get("html") or art_info.get("html_content")
            if html_content:
                self.add_resource(
                    uri=art_url,
                    payload=html_content.encode("utf-8") if isinstance(html_content, str) else html_content,
                    content_type="text/html; charset=utf-8",
                    status_code=200,
                    source_file=f"portal_state:{os.path.basename(portal_state_path)}",
                )
                count += 1
        return count

    def list_resources(self) -> List[Dict[str, Any]]:
        """Return metadata summary for all indexed resources."""
        return [
            {
                "uri": r.uri,
                "normalized_uri": r.normalized_uri,
                "status_code": r.status_code,
                "content_type": r.content_type,
                "size_bytes": len(r.payload),
                "payload_sha256": r.payload_sha256,
                "source_file": r.source_file,
            }
            for r in self.resources.values()
        ]

    def has_url(self, url: str) -> bool:
        return self.get_resource(url) is not None


class ReplayRewriter:
    """
    Rewrites references in HTML and CSS so that asset URLs route through
    the local replay server.
    """

    def __init__(self, replay_base_url: str):
        # replay_base_url e.g. "http://127.0.0.1:8765"
        self.replay_base = replay_base_url.rstrip("/")

    def to_replay_url(self, original_url: str) -> str:
        """Map an original URL to the local replay server endpoint."""
        norm = normalize_lookup_url(original_url)
        # Use endpoint /replay?url=<encoded_url>
        from urllib.parse import quote
        return f"{self.replay_base}/replay?url={quote(norm, safe='')}"

    def rewrite_html(self, html: str, page_url: str, missing_collector: Optional[List[str]] = None) -> str:
        """
        Rewrite HTML asset and internal link references.
        Inserts a replay banner and diagnostic markers for missing resources.
        """
        soup = BeautifulSoup(html, "html.parser")

        # 1. Base tag management
        base_tag = soup.find("base")
        doc_base = page_url
        if base_tag and base_tag.get("href"):
            doc_base = urljoin(page_url, base_tag["href"])
            # Remove base tag so relative links resolve relative to replay server or are rewritten
            base_tag.decompose()

        def rewrite_attr(tag: Any, attr_name: str):
            val = tag.get(attr_name)
            if not val or val.startswith("data:") or val.startswith("blob:") or val.startswith("javascript:"):
                return
            full_orig = urljoin(doc_base, val.strip())
            tag[attr_name] = self.to_replay_url(full_orig)

        # 2. Stylesheet links
        for link in soup.find_all("link", href=True):
            rel = link.get("rel", [])
            rel_str = " ".join(rel) if isinstance(rel, list) else str(rel)
            if any(k in rel_str.lower() for k in ["stylesheet", "icon", "preload", "prefetch"]):
                rewrite_attr(link, "href")

        # 3. Scripts
        for script in soup.find_all("script", src=True):
            rewrite_attr(script, "src")

        # 4. Images
        for img in soup.find_all("img"):
            if img.get("src"):
                rewrite_attr(img, "src")
            if img.get("srcset"):
                # Rewrite srcset entries
                new_parts = []
                for entry in img["srcset"].split(","):
                    entry = entry.strip()
                    if not entry:
                        continue
                    tokens = entry.split()
                    raw_src = tokens[0]
                    descriptor = " ".join(tokens[1:]) if len(tokens) > 1 else ""
                    full_src = urljoin(doc_base, raw_src)
                    rewritten_src = self.to_replay_url(full_src)
                    new_parts.append(f"{rewritten_src} {descriptor}".strip())
                img["srcset"] = ", ".join(new_parts)

        # 5. Media (audio, video, source)
        for media in soup.find_all(["audio", "video", "source"]):
            if media.get("src"):
                rewrite_attr(media, "src")

        # 6. Anchors (internal navigation between archived pages)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
                continue
            full_orig = urljoin(doc_base, href)
            # Route through replay
            a["href"] = self.to_replay_url(full_orig)

        # 7. Inline styles with url(...)
        for tag in soup.find_all(style=True):
            tag["style"] = self.rewrite_css(tag["style"], doc_base)

        for style_tag in soup.find_all("style"):
            if style_tag.string:
                style_tag.string = self.rewrite_css(style_tag.string, doc_base)

        # 8. Inject non-intrusive replay header banner
        banner_html = f"""
<!-- CorpusAI Offline Replay Banner -->
<div id="corpusai-replay-banner" style="position: sticky; top: 0; left: 0; right: 0; background: #1d2327; color: #fff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; line-height: 20px; padding: 6px 12px; z-index: 999999; display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #2271b1; box-shadow: 0 2px 4px rgba(0,0,0,0.2);">
  <div>
    <strong style="color: #72aee6;">CorpusAI Offline Replay</strong>
    <span style="margin: 0 8px; color: #8c8f94;">|</span>
    <span>Source: <a href="{page_url}" target="_blank" style="color: #c3c4c7; text-decoration: underline;">{page_url}</a></span>
  </div>
  <div style="font-size: 12px; color: #dcdcde;">
    <span>Offline Preservation Mode (Zero Live Network)</span>
  </div>
</div>
"""
        if soup.body:
            banner_soup = BeautifulSoup(banner_html, "html.parser")
            soup.body.insert(0, banner_soup)

        return str(soup)

    def rewrite_css(self, css_text: str, base_url: str) -> str:
        """Rewrite url(...) references inside CSS text."""
        url_pattern = re.compile(r'url\(\s*([\'"]?)(.*?)\1\s*\)', re.IGNORECASE)

        def replacer(match: Any) -> str:
            raw_url = match.group(2).strip()
            if not raw_url or raw_url.startswith("data:") or raw_url.startswith("blob:"):
                return match.group(0)
            full_orig = urljoin(base_url, raw_url)
            rewritten = self.to_replay_url(full_orig)
            quote = match.group(1) or '"'
            return f"url({quote}{rewritten}{quote})"

        return url_pattern.sub(replacer, css_text)


class ReplayHTTPHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP request handler that serves archived content from ReplayArchive.
    Enforces the live network barrier: any missing resource returns HTTP 404
    and is logged to missing_resources list. Never falls back to the live internet.
    """

    archive: ReplayArchive
    rewriter: ReplayRewriter
    request_log: List[Dict[str, Any]]
    missing_resources: List[Dict[str, Any]]

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default stderr logging to keep test output clean
        pass

    def do_GET(self) -> None:
        parsed_req = urlparse(self.path)
        path = parsed_req.path
        query = parse_qs(parsed_req.query)

        # 1. Health check endpoint
        if path == "/healthz" or path == "/":
            if not query.get("url"):
                self._serve_index()
                return

        # 2. Main replay endpoint: /replay?url=<target_url>
        target_url = None
        if path == "/replay" and "url" in query:
            target_url = query["url"][0]
        elif path.startswith("/"):
            # Support direct path lookup if requested directly by relative assets
            # Try to resolve against referer
            referer = self.headers.get("Referer", "")
            if referer and "/replay?url=" in referer:
                ref_target = parse_qs(urlparse(referer).query).get("url", [""])[0]
                if ref_target:
                    target_url = urljoin(ref_target, self.path)

        if not target_url:
            self._send_not_found(
                requested_url=self.path,
                reason="Invalid replay request: missing target 'url' parameter and no resolvable Referer.",
            )
            return

        # Lookup in archive
        resource = self.archive.get_resource(target_url)

        log_entry = {
            "url": target_url,
            "requested_url": target_url,
            "resolved": resource is not None,
            "status_code": resource.status_code if resource else 404,
            "content_type": resource.content_type if resource else None,
            "size_bytes": len(resource.payload) if resource else None,
            "timestamp": "now",
        }
        self.request_log.append(log_entry)

        if not resource:
            self.missing_resources.append({
                "url": target_url,
                "referer": self.headers.get("Referer", ""),
                "timestamp": "now",
            })
            self._send_not_found(
                requested_url=target_url,
                reason=f"Resource not found in archived collection: {target_url}",
            )
            return

        # Serve archived resource
        payload = resource.payload
        content_type = resource.content_type

        # Rewrite HTML and CSS for browser replay
        if resource.is_html:
            try:
                html_str = payload.decode("utf-8", errors="replace")
                rewritten_html = self.rewriter.rewrite_html(html_str, page_url=resource.uri)
                payload = rewritten_html.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            except Exception as e:
                logger.warning(f"Failed to rewrite HTML for {target_url}: {e}")

        elif resource.is_css:
            try:
                css_str = payload.decode("utf-8", errors="replace")
                rewritten_css = self.rewriter.rewrite_css(css_str, base_url=resource.uri)
                payload = rewritten_css.encode("utf-8")
                content_type = "text/css; charset=utf-8"
            except Exception as e:
                logger.warning(f"Failed to rewrite CSS for {target_url}: {e}")

        self.send_response(resource.status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-CorpusAI-Replay-Source", resource.source_file)
        self.send_header("X-CorpusAI-Target-URI", resource.uri)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_index(self) -> None:
        """Serve simple directory index of re-playable pages in the archive."""
        pages = self.archive.page_urls
        items = ""
        for p in pages:
            rep_url = self.rewriter.to_replay_url(p)
            items += f"<li><a href='{rep_url}'>{p}</a></li>\n"

        html = f"""<!DOCTYPE html>
<html>
<head><title>CorpusAI Replay Server</title></head>
<body style="font-family: sans-serif; padding: 20px;">
  <h1>CorpusAI Offline Replay Server</h1>
  <p>Status: <strong>Active (Zero Live Network Fallback)</strong></p>
  <p>Indexed Resources: {len(self.archive.resources)}</p>
  <h3>Available Pages:</h3>
  <ul>
    {items or '<li>No HTML pages detected in archive</li>'}
  </ul>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _send_not_found(self, requested_url: str, reason: str) -> None:
        """Explicit 404 response stating missing archive resource without live network fallback."""
        body = json.dumps({
            "error": "RESOURCE_NOT_IN_ARCHIVE",
            "requested_url": requested_url,
            "reason": reason,
            "live_network_fallback": False,
            "message": "CorpusAI Replay Server strictly prohibits live network fallback. All missing resources are reported rather than retrieved from the source website.",
        }, indent=2).encode("utf-8")

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-CorpusAI-Replay-Error", "NOT_FOUND_NO_LIVE_FALLBACK")
        self.end_headers()
        self.wfile.write(body)


class ArchiveReplayServer:
    """
    Controller for running the local HTTP replay server in a background thread.
    """

    def __init__(self, archive: ReplayArchive, host: str = "127.0.0.1", port: int = 0):
        self.archive = archive
        self.host = host
        self.port = port
        self.server: Optional[socketserver.TCPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.request_log: List[Dict[str, Any]] = []
        self.missing_resources: List[Dict[str, Any]] = []

    def start(self) -> str:
        """Start server on background thread and return base URL."""
        # Create custom handler with bound archive and logs
        archive = self.archive
        request_log = self.request_log
        missing_resources = self.missing_resources

        class BoundHandler(ReplayHTTPHandler):
            pass

        BoundHandler.archive = archive
        BoundHandler.request_log = request_log
        BoundHandler.missing_resources = missing_resources

        # Bind server to assigned or dynamic port (port=0 picks available free port)
        self.server = socketserver.TCPServer((self.host, self.port), BoundHandler)
        actual_port = self.server.server_address[1]
        self.port = actual_port
        base_url = f"http://{self.host}:{actual_port}"

        # Initialize rewriter with bound base URL
        BoundHandler.rewriter = ReplayRewriter(base_url)

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        logger.info(f"CorpusAI Replay Server running at {base_url}")
        return base_url

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def get_replay_url(self, original_url: str) -> str:
        from urllib.parse import quote
        norm = normalize_lookup_url(original_url)
        return f"{self.base_url}/replay?url={quote(norm, safe='')}"

    def stop(self) -> None:
        """Stop server and release socket."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None

    def __enter__(self) -> "ArchiveReplayServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


def find_portable_browser(custom_path: Optional[str] = None) -> str:
    """
    Locate a portable Chromium-family or compatible browser executable.

    Priority order:
    1. custom_path argument (if provided and exists).
    2. CORPUSAI_BROWSER_PATH environment variable (if set and exists).
    3. Standard PATH executables via shutil.which ('chromium', 'google-chrome', 'chrome', 'msedge').
    4. Common OS-specific installation paths (Windows Program Files / LocalAppData, Linux /usr/bin, macOS).

    Raises RuntimeError if no suitable browser binary can be found.
    """
    import shutil

    # 1. Explicit argument
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path)

    # 2. Environment variable
    env_browser = os.environ.get("CORPUSAI_BROWSER_PATH", "").strip()
    if env_browser and os.path.isfile(env_browser):
        return os.path.abspath(env_browser)

    # 3. Executable names in system PATH
    candidate_names = [
        "chromium",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "msedge",
    ]
    for name in candidate_names:
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return os.path.abspath(which_path)

    # 4. Standard platform installation locations
    candidate_locations = [
        # Windows Chrome & Edge standard paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        # Linux standard paths
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        # macOS standard paths
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]

    for loc in candidate_locations:
        if loc and os.path.isfile(loc):
            return os.path.abspath(loc)

    raise RuntimeError(
        "No Chromium-compatible browser binary found. "
        "Please set the CORPUSAI_BROWSER_PATH environment variable to a valid chrome or msedge executable."
    )


def get_browser_isolation_flags(
    allowed_host: str = "127.0.0.1",
    allowed_port: Optional[int] = None,
) -> List[str]:
    """
    Return Chromium command-line flags that strictly isolate browser network access:
    - routes all external network requests to an unroutable blackhole address (127.0.0.1:9 discard port)
    - bypasses the proxy exclusively for local loopback addresses (127.0.0.1 / localhost)
    - maps external DNS lookups to a non-routable address to prevent external leakage.
    """
    bypass = "127.0.0.1;localhost"
    if allowed_host and allowed_host not in ("127.0.0.1", "localhost"):
        bypass = f"{allowed_host};127.0.0.1;localhost"

    return [
        "--proxy-server=http://127.0.0.1:9",
        f"--proxy-bypass-list={bypass}",
        "--host-rules=MAP * 127.0.0.1:9, EXCLUDE 127.0.0.1, EXCLUDE localhost",
    ]



def compare_screenshots(
    image1_path: str,
    image2_path: str,
    diff_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compare two screenshots using Pillow (PIL) and return quantitative similarity metrics.

    Metrics returned:
    - match_ratio: float between 0.0 (completely different) and 1.0 (identical)
    - total_pixels: int total number of pixels evaluated
    - diff_pixel_count: int count of pixels that differ
    - dimensions_match: bool whether images have the same (width, height)
    - image1_dimensions: Tuple[int, int]
    - image2_dimensions: Tuple[int, int]
    - diff_bounding_box: Optional[Tuple[int, int, int, int]] (left, upper, right, lower)
    """
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise ImportError("Pillow is required for screenshot comparison. Run: pip install Pillow") from e

    if not os.path.isfile(image1_path):
        raise FileNotFoundError(f"Screenshot image1 not found: {image1_path}")
    if not os.path.isfile(image2_path):
        raise FileNotFoundError(f"Screenshot image2 not found: {image2_path}")

    with Image.open(image1_path) as im1, Image.open(image2_path) as im2:
        img1 = im1.convert("RGB")
        img2 = im2.convert("RGB")

    dim1 = img1.size
    dim2 = img2.size
    dimensions_match = (dim1 == dim2)

    # If dimensions do not match, normalize or evaluate intersection / canvas
    if not dimensions_match:
        # Resize or pad into common canvas for comparison, or evaluate common overlap
        common_w = max(dim1[0], dim2[0])
        common_h = max(dim1[1], dim2[1])
        base1 = Image.new("RGB", (common_w, common_h), (255, 255, 255))
        base2 = Image.new("RGB", (common_w, common_h), (255, 255, 255))
        base1.paste(img1, (0, 0))
        base2.paste(img2, (0, 0))
        img1 = base1
        img2 = base2

    diff = ImageChops.difference(img1, img2)
    bbox = diff.getbbox()

    total_pixels = img1.size[0] * img1.size[1]

    if bbox is None:
        # Identical images
        diff_pixel_count = 0
        match_ratio = 1.0
    else:
        # Count non-zero pixels in grayscale diff
        diff_gray = diff.convert("L")
        # diff_gray pixels > 0 are differences; use get_flattened_data if available (Pillow >= 10.1) else getdata
        if hasattr(diff_gray, "get_flattened_data"):
            diff_data = diff_gray.get_flattened_data()
        else:
            diff_data = diff_gray.getdata()
        diff_pixel_count = sum(1 for p in diff_data if p > 0)
        match_ratio = max(0.0, 1.0 - (diff_pixel_count / float(total_pixels)))

        if diff_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(diff_output_path)), exist_ok=True)
            diff.save(diff_output_path)

    return {
        "match_ratio": round(match_ratio, 6),
        "total_pixels": total_pixels,
        "diff_pixel_count": diff_pixel_count,
        "dimensions_match": dimensions_match,
        "image1_dimensions": dim1,
        "image2_dimensions": dim2,
        "diff_bounding_box": bbox,
    }

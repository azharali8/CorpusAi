"""
Deterministic WARC fixture generator for CI / offline testing.

Generates:
  data/warc/synthetic_portal.warc
  data/warc/mixed_content_test.warc

These files are .gitignore-d (*.warc) and must be regenerated before pytest
runs on any clean checkout (including GitHub Actions).

Usage:
  python tests/fixtures/generate_warc_fixtures.py

The generator is byte-stable: given the same source HTML files in
data/modified_html/, it always produces the same WARC bytes and the same
SHA-256 digest. Fixed UUIDs and a fixed date are embedded so no randomness
is introduced.
"""

import hashlib
import io
import os
import sys

# ---------------------------------------------------------------------------
# Project root on sys.path so we can import research helpers
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_WARC_DIR = os.path.join(_REPO_ROOT, "data", "warc")
DATA_HTML_DIR = os.path.join(_REPO_ROOT, "data", "modified_html")

SYNTHETIC_PORTAL_PATH = os.path.join(DATA_WARC_DIR, "synthetic_portal.warc")
MIXED_CONTENT_PATH = os.path.join(DATA_WARC_DIR, "mixed_content_test.warc")


# ---------------------------------------------------------------------------
# Low-level raw WARC writer (no warcio) — deterministic header order
# ---------------------------------------------------------------------------

def _crlf(s: str) -> bytes:
    """Encode a string replacing \\n with \\r\\n, then encode to bytes."""
    return s.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def _sha1_b64(data: bytes) -> str:
    """Return warcio-style 'sha1:<base64>' digest."""
    import base64
    digest = hashlib.sha1(data).digest()
    return "sha1:" + base64.b32encode(digest).decode("ascii")


def _build_http_response_block(content_type: str, body: bytes) -> bytes:
    """Build the HTTP/1.1 response block (status line + headers + blank + body)."""
    http_header = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Server: TestServer/1.0\r\n"
        f"\r\n"
    )
    return http_header.encode("utf-8") + body


def _write_warc_record(
    out: io.RawIOBase,
    record_type: str,
    target_uri: str,
    record_id: str,
    date: str,
    block: bytes,
    content_type: str = "application/http; msgtype=response",
) -> None:
    """
    Write a single WARC/1.0 record with a fixed, canonical header order:
      WARC-Type
      WARC-Record-ID
      WARC-Target-URI
      WARC-Date
      WARC-Payload-Digest
      WARC-Block-Digest
      Content-Type
      Content-Length
    """
    payload_digest = _sha1_b64(block)      # payload = block for response records
    block_digest = _sha1_b64(block)

    header_lines = [
        "WARC/1.0",
        f"WARC-Type: {record_type}",
        f"WARC-Record-ID: <{record_id}>",
        f"WARC-Target-URI: {target_uri}",
        f"WARC-Date: {date}",
        f"WARC-Payload-Digest: {payload_digest}",
        f"WARC-Block-Digest: {block_digest}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(block)}",
        "",
        "",  # blank line after headers
    ]
    header_bytes = "\r\n".join(header_lines).encode("utf-8")
    out.write(header_bytes)
    out.write(block)
    out.write(b"\r\n\r\n")  # record terminator


# ---------------------------------------------------------------------------
# synthetic_portal.warc
# ---------------------------------------------------------------------------

# Fixed record metadata — must not change between runs
_PORTAL_DATE = "2026-09-29T06:19:55Z"

_PORTAL_RECORDS = [
    # (page_filename, url, uuid)
    ("page01_changed.html", "https://example.org/articles/page01", "urn:uuid:93bb53ce-c528-42be-a910-0f7e0b926dc7"),
    ("page02_changed.html", "https://example.org/articles/page02", "urn:uuid:4451fd67-0bfd-4c89-be4d-8d8f4e1bbf08"),
    ("page03_changed.html", "https://example.org/articles/page03", "urn:uuid:f0fb9ef2-80f3-4cd1-b6fd-6b0ff5f7c393"),
    ("page04_changed.html", "https://example.org/articles/page04", "urn:uuid:fc49af0c-4d42-4109-94b1-7ae34de36ff1"),
    ("page05_changed.html", "https://example.org/articles/page05", "urn:uuid:df546d88-ba67-4d94-9202-d793e5948d12"),
    ("page06_changed.html", "https://example.org/articles/page06", "urn:uuid:259b72b5-eee1-4427-875d-223378bd3606"),
]

_PORTAL_ROBOTS_UUID = "urn:uuid:ff3d348c-b52f-41db-be94-9ac4d0944a2e"
_PORTAL_PNG_UUID    = "urn:uuid:c0522b9a-c248-42c9-9acf-bf702f42c072"

# Minimal 1x1 transparent PNG (16 bytes PNG signature + IHDR chunk header)
_PNG_BYTES = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
])

_ROBOTS_BODY = b"User-agent: *\nDisallow: /admin\n"


def generate_synthetic_portal_warc(output_path: str) -> str:
    """Write synthetic_portal.warc deterministically. Returns the file path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    buf = io.BytesIO()

    # --- HTML pages ---
    for filename, url, uuid in _PORTAL_RECORDS:
        src = os.path.join(DATA_HTML_DIR, filename)
        with open(src, "rb") as fh:
            raw = fh.read()
        # Normalise CRLF → LF for determinism across editors/OS
        body = raw.replace(b"\r\n", b"\n")
        block = _build_http_response_block("text/html; charset=utf-8", body)
        _write_warc_record(buf, "response", url, uuid, _PORTAL_DATE, block)

    # --- robots.txt ---
    block = _build_http_response_block("text/plain", _ROBOTS_BODY)
    _write_warc_record(
        buf, "response",
        "https://example.org/robots.txt",
        _PORTAL_ROBOTS_UUID,
        _PORTAL_DATE, block,
    )

    # --- PNG badge ---
    block = _build_http_response_block("image/png", _PNG_BYTES)
    _write_warc_record(
        buf, "response",
        "https://example.org/images/badge.png",
        _PORTAL_PNG_UUID,
        _PORTAL_DATE, block,
    )

    with open(output_path, "wb") as fh:
        fh.write(buf.getvalue())

    return os.path.abspath(output_path)


# ---------------------------------------------------------------------------
# mixed_content_test.warc (deterministic, fixed UUIDs and date)
# ---------------------------------------------------------------------------

_MIXED_DATE = "2026-09-29T12:00:00Z"
_MIXED_INFO_UUID = "urn:uuid:00000000-0000-0000-0000-000000000001"

_PNG_FULL_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_MIXED_RESPONSES = [
    # (url, content_type, body_bytes, uuid)
    (
        "https://example.test/articles/science-today.html",
        "text/html; charset=utf-8",
        b"<!DOCTYPE html><html><head><title>Science Today</title></head><body><h1>Science Breakthrough</h1><p>Researchers discovered novel quantum properties.</p></body></html>",
        "urn:uuid:10000000-0000-0000-0000-000000000001",
    ),
    (
        "https://example.test/article/123",
        "text/html; charset=utf-8",
        b"<!DOCTYPE html><html><head><title>Article 123</title></head><body><h1>Indigenous Knowledge Systems</h1><p>Traditional agricultural practices enhance biodiversity.</p></body></html>",
        "urn:uuid:10000000-0000-0000-0000-000000000002",
    ),
    (
        "https://example.test/news?id=45",
        "text/html; charset=utf-8",
        b"<!DOCTYPE html><html><head><title>News Item 45</title></head><body><h1>Global Climate Agreement</h1><p>Nations ratify landmark emissions treaty.</p></body></html>",
        "urn:uuid:10000000-0000-0000-0000-000000000003",
    ),
    (
        "https://example.test/assets/style.css",
        "text/css",
        b"body { font-family: sans-serif; color: #333; } h1 { color: darkblue; }",
        "urn:uuid:10000000-0000-0000-0000-000000000004",
    ),
    (
        "https://example.test/assets/app.js",
        "application/javascript",
        b"console.log('App initialized'); document.addEventListener('DOMContentLoaded', () => {});",
        "urn:uuid:10000000-0000-0000-0000-000000000005",
    ),
    (
        "https://example.test/images/logo.png",
        "image/png",
        _PNG_FULL_BYTES,
        "urn:uuid:10000000-0000-0000-0000-000000000006",
    ),
    (
        "https://example.test/image.html",
        "image/png",
        _PNG_FULL_BYTES,
        "urn:uuid:10000000-0000-0000-0000-000000000007",
    ),
    (
        "https://example.test/api/metadata.json",
        "application/json",
        b'{"portal": "example.test", "total_articles": 1500, "status": "active"}',
        "urn:uuid:10000000-0000-0000-0000-000000000008",
    ),
    (
        "https://example.test/robots.txt",
        "text/plain",
        b"User-agent: *\nDisallow: /admin/\nAllow: /articles/\n",
        "urn:uuid:10000000-0000-0000-0000-000000000009",
    ),
]

_MIXED_RESOURCE_UUID = "urn:uuid:10000000-0000-0000-0000-000000000010"


def generate_mixed_content_warc(output_path: str) -> str:
    """Write mixed_content_test.warc deterministically. Returns the file path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    buf = io.BytesIO()

    # 1. warcinfo record
    warcinfo_payload = (
        b"software: CorpusAI Mixed Content Generator 1.0\r\n"
        b"format: WARC File Format 1.0\r\n"
        b"conformance: ISO 28500:2009\r\n"
    )
    _write_warc_record(
        buf,
        "warcinfo",
        "urn:warcinfo:mixed_content_test.warc",
        _MIXED_INFO_UUID,
        _MIXED_DATE,
        warcinfo_payload,
        content_type="application/warc-fields",
    )

    # 2. 9 HTTP response records
    for url, content_type, body_bytes, uuid in _MIXED_RESPONSES:
        block = _build_http_response_block(content_type, body_bytes)
        _write_warc_record(buf, "response", url, uuid, _MIXED_DATE, block)

    # 3. 1 resource record (testing non-response filtering)
    resource_bytes = b"urn:uuid:test-metadata-resource-data"
    _write_warc_record(
        buf,
        "resource",
        "urn:example:metadata:resource01",
        _MIXED_RESOURCE_UUID,
        _MIXED_DATE,
        resource_bytes,
        content_type="text/plain",
    )

    with open(output_path, "wb") as fh:
        fh.write(buf.getvalue())

    return os.path.abspath(output_path)


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Generating WARC fixtures ...")

    path1 = generate_synthetic_portal_warc(SYNTHETIC_PORTAL_PATH)
    sha1 = sha256_file(path1)
    print(f"  synthetic_portal.warc  -> {path1}")
    print(f"  SHA-256: {sha1}")

    path2 = generate_mixed_content_warc(MIXED_CONTENT_PATH)
    sha2 = sha256_file(path2)
    print(f"  mixed_content_test.warc -> {path2}")
    print(f"  SHA-256: {sha2}")

    print("Done.")

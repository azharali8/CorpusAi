"""
Script to generate the controlled mixed-content WARC fixture.
Saves to data/warc/mixed_content_test.warc

Fixture contents:
- 3 HTML response records:
  1. Standard URL: https://example.test/articles/science-today.html (text/html; charset=utf-8)
  2. Extensionless URL: https://example.test/article/123 (text/html; charset=utf-8)
  3. Query-param URL: https://example.test/news?id=45 (text/html; charset=utf-8)
- 1 CSS response record:
  4. https://example.test/assets/style.css (text/css)
- 1 JavaScript response record:
  5. https://example.test/assets/app.js (application/javascript)
- 2 Image response records:
  6. Standard PNG: https://example.test/images/logo.png (image/png)
  7. Misleading URL (.html extension, but image payload): https://example.test/image.html (image/png)
- 1 JSON response record:
  8. https://example.test/api/metadata.json (application/json)
- 1 Plain-text response record:
  9. https://example.test/robots.txt (text/plain)
- 1 WARC Info / Resource record (non-response, testing record-type filtering):
  10. Non-response record or resource record
"""

import io
import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


def create_mixed_content_warc(output_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # 1x1 transparent PNG bytes
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
        b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    records = [
        # 1. HTML standard
        (
            "https://example.test/articles/science-today.html",
            "text/html; charset=utf-8",
            "<!DOCTYPE html><html><head><title>Science Today</title></head><body><h1>Science Breakthrough</h1><p>Researchers discovered novel quantum properties.</p></body></html>".encode("utf-8"),
            "response"
        ),
        # 2. HTML extensionless
        (
            "https://example.test/article/123",
            "text/html; charset=utf-8",
            "<!DOCTYPE html><html><head><title>Article 123</title></head><body><h1>Indigenous Knowledge Systems</h1><p>Traditional agricultural practices enhance biodiversity.</p></body></html>".encode("utf-8"),
            "response"
        ),
        # 3. HTML query-param
        (
            "https://example.test/news?id=45",
            "text/html; charset=utf-8",
            "<!DOCTYPE html><html><head><title>News Item 45</title></head><body><h1>Global Climate Agreement</h1><p>Nations ratify landmark emissions treaty.</p></body></html>".encode("utf-8"),
            "response"
        ),
        # 4. CSS
        (
            "https://example.test/assets/style.css",
            "text/css",
            "body { font-family: sans-serif; color: #333; } h1 { color: darkblue; }".encode("utf-8"),
            "response"
        ),
        # 5. JavaScript
        (
            "https://example.test/assets/app.js",
            "application/javascript",
            "console.log('App initialized'); document.addEventListener('DOMContentLoaded', () => {});".encode("utf-8"),
            "response"
        ),
        # 6. Image PNG
        (
            "https://example.test/images/logo.png",
            "image/png",
            png_bytes,
            "response"
        ),
        # 7. Misleading URL: .html extension but image/png MIME
        (
            "https://example.test/image.html",
            "image/png",
            png_bytes,
            "response"
        ),
        # 8. JSON API
        (
            "https://example.test/api/metadata.json",
            "application/json",
            '{"portal": "example.test", "total_articles": 1500, "status": "active"}'.encode("utf-8"),
            "response"
        ),
        # 9. Plain text
        (
            "https://example.test/robots.txt",
            "text/plain",
            "User-agent: *\nDisallow: /admin/\nAllow: /articles/\n".encode("utf-8"),
            "response"
        ),
    ]

    with open(output_path, "wb") as output:
        writer = WARCWriter(output, gzip=False)

        # First, write a warcinfo record
        warcinfo_payload = b"software: CorpusAI Mixed Content Generator 1.0\r\nformat: WARC File Format 1.0\r\nconformance: ISO 28500:2009\r\n"
        warcinfo_record = writer.create_warcinfo_record(
            filename=os.path.basename(output_path),
            info={
                "software": "CorpusAI Mixed Content Generator 1.0",
                "format": "WARC File Format 1.0",
                "conformance": "ISO 28500:2009",
            }
        )
        writer.write_record(warcinfo_record)

        # Next, write all HTTP response records
        for url, content_type, body_bytes, rec_type in records:
            http_headers = StatusAndHeaders(
                "200 OK",
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body_bytes))),
                    ("Server", "TestServer/1.0"),
                ],
                protocol="HTTP/1.1",
            )
            record = writer.create_warc_record(
                uri=url,
                record_type=rec_type,
                http_headers=http_headers,
                payload=io.BytesIO(body_bytes),
                length=len(body_bytes),
            )
            writer.write_record(record)

        # Also write 1 resource record (e.g. metadata or screenshot resource) to test non-response filtering
        resource_bytes = b"urn:uuid:test-metadata-resource-data"
        resource_record = writer.create_warc_record(
            uri="urn:example:metadata:resource01",
            record_type="resource",
            warc_content_type="text/plain",
            payload=io.BytesIO(resource_bytes),
            length=len(resource_bytes),
        )
        writer.write_record(resource_record)

    return os.path.abspath(output_path)


if __name__ == "__main__":
    target = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "warc", "mixed_content_test.warc"
    )
    res = create_mixed_content_warc(target)
    print(f"Created controlled mixed WARC fixture at: {res}")

"""
Record Inspector and Classifier for WARC Archives.

Inspects every record in a WARC file, extracts record headers and HTTP headers,
and classifies records by resource type (HTML, CSS, JAVASCRIPT, IMAGE, JSON, TEXT, OTHER)
primarily using HTTP Content-Type rather than solely URL extensions.
"""

import os
import sys
from typing import Any, Dict, List, Optional

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from warcio.archiveiterator import ArchiveIterator


def classify_record_content(
    rec_type: str,
    http_content_type: Optional[str],
    warc_content_type: Optional[str],
    url: Optional[str] = None
) -> str:
    """
    Classify a WARC record into a resource category.

    Priority:
    1. Check record type: if not 'response', categorize as NON_RESPONSE / OTHER
    2. Check HTTP Content-Type header (standard MIME type check)
    3. If HTTP Content-Type is missing or generic (e.g. application/octet-stream),
       fall back to URL extension hints or WARC Content-Type.
    """
    if rec_type != "response":
        return "OTHER"

    ct = (http_content_type or "").strip().lower()

    # Extract base MIME type (before ';')
    mime_base = ct.split(";")[0].strip() if ct else ""

    if mime_base in ("text/html", "application/xhtml+xml"):
        return "HTML"
    elif mime_base == "text/css":
        return "CSS"
    elif mime_base in (
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
    ):
        return "JAVASCRIPT"
    elif mime_base.startswith("image/"):
        return "IMAGE"
    elif mime_base in ("application/json", "text/json"):
        return "JSON"
    elif mime_base.startswith("font/") or mime_base in (
        "application/font-woff",
        "font/woff",
        "font/woff2",
        "application/font-woff2",
        "application/x-font-ttf",
    ):
        return "FONT"
    elif mime_base in ("text/plain", "text/markdown"):
        return "TEXT"
    elif mime_base in ("application/xml", "text/xml"):
        return "XML"
    elif mime_base in ("application/pdf",):
        return "PDF"
    else:
        # If MIME was empty or unknown, check URL extension as a low-priority fallback
        if url:
            clean_url = url.split("?")[0].lower()
            if clean_url.endswith(".css"):
                return "CSS"
            elif clean_url.endswith(".js"):
                return "JAVASCRIPT"
            elif clean_url.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")):
                return "IMAGE"
            elif clean_url.endswith(".json"):
                return "JSON"
            elif clean_url.endswith((".txt", ".md")):
                return "TEXT"
        return "OTHER"


def inspect_warc_file(warc_path: str) -> List[Dict[str, Any]]:
    """
    Inspect every record in a WARC file and return structured record metadata.
    """
    if not os.path.isfile(warc_path):
        raise FileNotFoundError(f"WARC file not found: {warc_path}")

    inspected_records = []

    with open(warc_path, "rb") as stream:
        for index, record in enumerate(ArchiveIterator(stream), start=1):
            warc_type = record.rec_type
            warc_uri = record.rec_headers.get_header("WARC-Target-URI", "")
            warc_record_id = record.rec_headers.get_header("WARC-Record-ID", "")
            warc_date = record.rec_headers.get_header("WARC-Date", "")
            warc_content_type = record.rec_headers.get_header("Content-Type", "")

            http_status = None
            http_content_type = None

            if record.http_headers:
                try:
                    http_status = int(record.http_headers.get_statuscode())
                except (ValueError, TypeError):
                    http_status = None
                http_content_type = record.http_headers.get_header("Content-Type", "")

            # Read payload length safely without consuming unrecoverable streams
            payload_bytes = record.raw_stream.read()
            payload_size = len(payload_bytes)

            category = classify_record_content(
                rec_type=warc_type,
                http_content_type=http_content_type,
                warc_content_type=warc_content_type,
                url=warc_uri,
            )

            inspected_records.append({
                "index": index,
                "warc_record_id": warc_record_id,
                "warc_type": warc_type,
                "warc_target_uri": warc_uri,
                "warc_date": warc_date,
                "warc_content_type": warc_content_type,
                "http_status": http_status,
                "http_content_type": http_content_type,
                "payload_size_bytes": payload_size,
                "classified_category": category,
            })

    return inspected_records


def filter_usable_html_records(warc_path: str) -> List[Dict[str, Any]]:
    """
    Filter and extract only usable HTML documents from a WARC file.

    Criteria:
    - WARC record type == 'response'
    - HTTP status == 200 (or None if not parsed, but non-error 2xx)
    - HTTP Content-Type starts with 'text/html' or 'application/xhtml+xml'
    - Non-empty payload (> 0 bytes)
    """
    all_inspected = inspect_warc_file(warc_path)
    html_records = []

    for rec in all_inspected:
        if rec["warc_type"] != "response":
            continue

        # Status check
        if rec["http_status"] is not None and not (200 <= rec["http_status"] < 300):
            continue

        # Content-Type check
        http_ct = (rec["http_content_type"] or "").lower()
        mime_base = http_ct.split(";")[0].strip()
        if mime_base not in ("text/html", "application/xhtml+xml"):
            continue

        # Payload check
        if rec["payload_size_bytes"] <= 0:
            continue

        html_records.append(rec)

    return html_records


def print_inspection_summary(records: List[Dict[str, Any]]) -> None:
    print("-" * 100)
    print(f"{'Idx':<4} {'WARC-Type':<12} {'HTTP':<6} {'MIME Type':<30} {'Category':<12} {'Size':<8} {'Target URI'}")
    print("-" * 100)
    for r in records:
        mime = (r['http_content_type'] or r['warc_content_type'] or 'None')[:28]
        status_str = str(r['http_status']) if r['http_status'] is not None else "N/A"
        print(f"{r['index']:<4} {r['warc_type']:<12} {status_str:<6} {mime:<30} {r['classified_category']:<12} {r['payload_size_bytes']:<8} {r['warc_target_uri']}")
    print("-" * 100)


if __name__ == "__main__":
    warc_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "warc", "mixed_content_test.warc"
    )
    if not os.path.isfile(warc_file):
        print(f"Fixture not found at {warc_file}. Run create_mixed_warc.py first.")
        sys.exit(1)

    print(f"Inspecting WARC: {warc_file}\n")
    records = inspect_warc_file(warc_file)
    print_inspection_summary(records)

    htmls = filter_usable_html_records(warc_file)
    print(f"\nFiltered Usable HTML Records ({len(htmls)} found):")
    for h in htmls:
        print(f"  [{h['http_status']}] {h['warc_target_uri']} (MIME: {h['http_content_type']})")

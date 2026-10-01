"""
WARC Utilities module for CorpusAI.

Provides isolated utilities for reading, validating, decoding, and partitioning
Web ARChive (WARC) files using warcio.
Preserves URL provenance, record IDs, and SHA-256 checksums.
"""

import hashlib
import io
import os
from typing import Any, Dict, List, Optional, Tuple

from warcio.archiveiterator import ArchiveIterator
from warcio.warcwriter import WARCWriter
from warcio.statusandheaders import StatusAndHeaders


def calculate_warc_sha256(warc_path: str) -> str:
    """
    Calculate the SHA-256 checksum of a WARC file.
    """
    if not os.path.exists(warc_path):
        raise FileNotFoundError(f"WARC file not found: {warc_path}")

    sha256 = hashlib.sha256()
    with open(warc_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def validate_warc_input(warc_path: str) -> Dict[str, Any]:
    """
    Validate that a WARC file exists, is non-empty, and can be read by ArchiveIterator.

    Returns:
        Dict with total_records, html_records, valid, sha256, and file_size.
    """
    if not os.path.exists(warc_path):
        return {
            "valid": False,
            "error": f"WARC file does not exist: {warc_path}",
            "total_records": 0,
            "html_records": 0,
        }

    file_size = os.path.getsize(warc_path)
    if file_size == 0:
        return {
            "valid": False,
            "error": "WARC file is empty (0 bytes)",
            "total_records": 0,
            "html_records": 0,
        }

    total_records = 0
    html_records = 0

    try:
        with open(warc_path, "rb") as stream:
            for record in ArchiveIterator(stream):
                total_records += 1
                if record.rec_type == "response":
                    content_type = record.http_headers.get_header("Content-Type", "") if record.http_headers else ""
                    if "text/html" in content_type.lower() or "xhtml" in content_type.lower():
                        html_records += 1
    except Exception as e:
        return {
            "valid": False,
            "error": f"Failed to parse WARC stream: {str(e)}",
            "total_records": total_records,
            "html_records": html_records,
        }

    return {
        "valid": True,
        "error": None,
        "file_size_bytes": file_size,
        "sha256": calculate_warc_sha256(warc_path),
        "total_records": total_records,
        "html_records": html_records,
    }


def list_html_records(warc_path: str) -> List[Dict[str, Any]]:
    """
    List all HTML response records in a WARC file without loading full payloads into memory.

    Returns:
        List of dicts containing url, record_id, content_type, date, and http_status.
    """
    if not os.path.exists(warc_path):
        raise FileNotFoundError(f"WARC file not found: {warc_path}")

    records_meta = []
    with open(warc_path, "rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type != "response":
                continue

            http_headers = record.http_headers
            if not http_headers:
                continue

            content_type = http_headers.get_header("Content-Type", "")
            if not ("text/html" in content_type.lower() or "xhtml" in content_type.lower()):
                continue

            url = record.rec_headers.get_header("WARC-Target-URI", "")
            record_id = record.rec_headers.get_header("WARC-Record-ID", "")
            date = record.rec_headers.get_header("WARC-Date", "")
            status_code = http_headers.get_statuscode()

            records_meta.append({
                "url": url,
                "record_id": record_id,
                "content_type": content_type,
                "date": date,
                "http_status": status_code,
                "warc_file": os.path.basename(warc_path),
            })

    return records_meta


def extract_html_samples(warc_path: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Extract decoded HTML document contents and metadata from response records in a WARC file.

    Args:
        warc_path: Path to the WARC file.
        limit: Optional maximum number of records to return.

    Returns:
        List of dicts: {"url": str, "record_id": str, "content_type": str, "html": str, "warc_file": str}
    """
    if not os.path.exists(warc_path):
        raise FileNotFoundError(f"WARC file not found: {warc_path}")

    extracted: List[Dict[str, Any]] = []

    with open(warc_path, "rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type != "response":
                continue

            http_headers = record.http_headers
            if not http_headers:
                continue

            content_type = http_headers.get_header("Content-Type", "")
            if not ("text/html" in content_type.lower() or "xhtml" in content_type.lower()):
                continue

            url = record.rec_headers.get_header("WARC-Target-URI", "")
            record_id = record.rec_headers.get_header("WARC-Record-ID", "")
            date = record.rec_headers.get_header("WARC-Date", "")

            # Read and decode body
            raw_bytes = record.raw_stream.read()
            html_content = ""
            for enc in ["utf-8", "latin-1", "cp1252"]:
                try:
                    html_content = raw_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue

            if not html_content:
                html_content = raw_bytes.decode("utf-8", errors="replace")

            extracted.append({
                "url": url,
                "record_id": record_id,
                "content_type": content_type,
                "date": date,
                "html": html_content,
                "warc_file": os.path.basename(warc_path),
            })

            if limit is not None and len(extracted) >= limit:
                break

    return extracted


def read_html_record(warc_path: str, target_url: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a specific HTML record matching target_url from a WARC file.
    """
    records = extract_html_samples(warc_path)
    for rec in records:
        if rec["url"] == target_url:
            return rec
    return None


def split_warc_records(
    records: List[Dict[str, Any]],
    gen_count: int = 3,
    val_count: int = 2,
    test_count: int = 1,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Partition WARC records deterministically into disjoint generation, validation, and held-out sets.

    Ensures that URLs between the three splits are strictly disjoint.
    """
    total_required = gen_count + val_count + test_count
    if len(records) < total_required:
        raise ValueError(
            f"Insufficient records ({len(records)}) for split: "
            f"needs {gen_count} gen + {val_count} val + {test_count} test = {total_required}"
        )

    # Sort deterministically by URL
    sorted_records = sorted(records, key=lambda r: r.get("url", ""))

    gen_records = sorted_records[:gen_count]
    val_records = sorted_records[gen_count : gen_count + val_count]
    test_records = sorted_records[gen_count + val_count : gen_count + val_count + test_count]

    # Verify disjointness
    gen_urls = {r["url"] for r in gen_records}
    val_urls = {r["url"] for r in val_records}
    test_urls = {r["url"] for r in test_records}

    assert gen_urls.isdisjoint(val_urls), "Generation and Validation URL sets overlap!"
    assert gen_urls.isdisjoint(test_urls), "Generation and Held-Out URL sets overlap!"
    assert val_urls.isdisjoint(test_urls), "Validation and Held-Out URL sets overlap!"

    return gen_records, val_records, test_records


def write_warc_fixture(
    output_warc_path: str,
    records_data: List[Tuple[str, str, str]],
) -> str:
    """
    Create a standard WARC file fixture containing response records.

    Args:
        output_warc_path: Target path for the .warc or .warc.gz file.
        records_data: List of tuples: (url, content_type, content_string_or_bytes)

    Returns:
        Absolute path to the created WARC file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_warc_path)), exist_ok=True)

    with open(output_warc_path, "wb") as output:
        writer = WARCWriter(output, gzip=False)
        for url, content_type, body_data in records_data:
            if isinstance(body_data, str):
                body_bytes = body_data.encode("utf-8")
            else:
                body_bytes = body_data

            http_headers = StatusAndHeaders(
                "200 OK",
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body_bytes))),
                ],
                protocol="HTTP/1.1",
            )

            record = writer.create_warc_record(
                uri=url,
                record_type="response",
                http_headers=http_headers,
                payload=io.BytesIO(body_bytes),
                length=len(body_bytes),
            )
            writer.write_record(record)

    return os.path.abspath(output_warc_path)


def create_warc_response_file(
    output_warc_path: str,
    records: List[Dict[str, Any]],
    gzip: bool = True,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Write HTTP response records to a WARC (or .warc.gz) file, returning path and record details.

    Each record in `records` should contain:
    - uri (str)
    - status_code (int or str, default 200)
    - content_type (str)
    - payload (bytes or str)
    - warc_date (optional str)
    - custom_warc_headers (optional Dict[str, str])

    Returns:
        Tuple of (absolute_warc_path, list_of_record_provenance_dicts)
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_warc_path)), exist_ok=True)

    written_records_info = []

    with open(output_warc_path, "wb") as output:
        writer = WARCWriter(output, gzip=gzip)
        for rec in records:
            uri = rec["uri"]
            status_code = rec.get("status_code", 200)
            status_line = f"{status_code} OK" if str(status_code) == "200" else f"{status_code} Response"
            content_type = rec.get("content_type", "text/html; charset=utf-8")
            payload = rec["payload"]

            if isinstance(payload, str):
                payload_bytes = payload.encode("utf-8")
            else:
                payload_bytes = payload

            payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()

            http_headers = StatusAndHeaders(
                status_line,
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(payload_bytes))),
                ],
                protocol="HTTP/1.1",
            )

            warc_headers = rec.get("custom_warc_headers", {})

            warc_record = writer.create_warc_record(
                uri=uri,
                record_type="response",
                http_headers=http_headers,
                payload=io.BytesIO(payload_bytes),
                length=len(payload_bytes),
                warc_headers_dict=warc_headers,
            )

            record_id = warc_record.rec_headers.get_header("WARC-Record-ID")
            record_date = warc_record.rec_headers.get_header("WARC-Date")

            writer.write_record(warc_record)

            written_records_info.append({
                "warc_record_id": record_id,
                "warc_target_uri": uri,
                "warc_date": record_date,
                "payload_sha256": payload_sha256,
                "payload_length_bytes": len(payload_bytes),
                "content_type": content_type,
                "http_status": status_code,
            })

    return os.path.abspath(output_warc_path), written_records_info


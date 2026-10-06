"""
Tests for tests/fixtures/generate_warc_fixtures.py

Verifies:
1. Generator runs successfully and creates both WARC fixture files.
2. synthetic_portal.warc matches the expected reproducible SHA-256.
3. mixed_content_test.warc matches the expected reproducible SHA-256.
4. Second generation run produces byte-identical files (idempotence).
5. Generated files are valid WARCs readable by ArchiveIterator.
"""

import os
import pytest
from warcio.archiveiterator import ArchiveIterator

from tests.fixtures.generate_warc_fixtures import (
    generate_synthetic_portal_warc,
    generate_mixed_content_warc,
    sha256_file,
    SYNTHETIC_PORTAL_PATH,
    MIXED_CONTENT_PATH,
)
from tests.test_warc_utils import KNOWN_SHA256 as PORTAL_EXPECTED_SHA256

# Expected SHA-256 for mixed_content_test.warc produced deterministically
MIXED_EXPECTED_SHA256 = "fe50a05d16ac3a0ce23df96d908bef05edca4aba3ee8f6c7895516a2aa0de259"


def test_synthetic_portal_reproducible_sha(tmp_path):
    """Generating synthetic_portal.warc twice yields identical SHA-256 matching KNOWN_SHA256."""
    out1 = str(tmp_path / "test1.warc")
    out2 = str(tmp_path / "test2.warc")

    generate_synthetic_portal_warc(out1)
    generate_synthetic_portal_warc(out2)

    sha1 = sha256_file(out1)
    sha2 = sha256_file(out2)

    assert sha1 == sha2, "Generator is not deterministic across runs"
    assert sha1 == PORTAL_EXPECTED_SHA256, (
        f"SHA-256 mismatch.\n  Expected: {PORTAL_EXPECTED_SHA256}\n  Got:      {sha1}"
    )


def test_mixed_content_reproducible_sha(tmp_path):
    """Generating mixed_content_test.warc twice yields identical SHA-256 matching MIXED_EXPECTED_SHA256."""
    out1 = str(tmp_path / "test_mixed1.warc")
    out2 = str(tmp_path / "test_mixed2.warc")

    generate_mixed_content_warc(out1)
    generate_mixed_content_warc(out2)

    sha1 = sha256_file(out1)
    sha2 = sha256_file(out2)

    assert sha1 == sha2, "Mixed content generator is not deterministic across runs"
    assert sha1 == MIXED_EXPECTED_SHA256, (
        f"SHA-256 mismatch.\n  Expected: {MIXED_EXPECTED_SHA256}\n  Got:      {sha1}"
    )


def test_generated_portal_valid_warc_records(tmp_path):
    """Generated synthetic_portal.warc must contain 8 total records (6 HTML + 1 robots + 1 PNG)."""
    out = str(tmp_path / "portal_test.warc")
    generate_synthetic_portal_warc(out)

    records = []
    with open(out, "rb") as stream:
        for record in ArchiveIterator(stream):
            records.append((
                record.rec_type,
                record.rec_headers.get_header("WARC-Target-URI"),
            ))

    assert len(records) == 8, f"Expected 8 records, found {len(records)}"
    urls = [u for _, u in records]
    assert "https://example.org/articles/page01" in urls
    assert "https://example.org/robots.txt" in urls
    assert "https://example.org/images/badge.png" in urls


def test_generated_mixed_valid_warc_records(tmp_path):
    """Generated mixed_content_test.warc must contain 11 total records."""
    out = str(tmp_path / "mixed_test.warc")
    generate_mixed_content_warc(out)

    records = []
    with open(out, "rb") as stream:
        for record in ArchiveIterator(stream):
            records.append(record.rec_type)

    assert len(records) == 11, f"Expected 11 records, found {len(records)}"
    assert records.count("response") == 9
    assert records.count("warcinfo") == 1
    assert records.count("resource") == 1

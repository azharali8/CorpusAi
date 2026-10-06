"""
Tests for src/warc_utils.py

All tests use the pre-built synthetic_portal.warc fixture.
No network access required.
"""

import os
import pytest

WARC_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "data", "warc", "synthetic_portal.warc"
)
WARC_FIXTURE = os.path.normpath(WARC_FIXTURE)

# Known SHA-256 of the fixture file (generated deterministically by
# tests/fixtures/generate_warc_fixtures.py from tracked source HTML files)
KNOWN_SHA256 = "0e33f0cccaa1c4935a0ed0b6516610d7957e8e136b2d564f7ebe80c08b7f5f7b"

# Expected HTML URLs inside the fixture
EXPECTED_HTML_URLS = [
    "https://example.org/articles/page01",
    "https://example.org/articles/page02",
    "https://example.org/articles/page03",
    "https://example.org/articles/page04",
    "https://example.org/articles/page05",
    "https://example.org/articles/page06",
]


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from src.warc_utils import (
    calculate_warc_sha256,
    validate_warc_input,
    list_html_records,
    extract_html_samples,
    read_html_record,
    split_warc_records,
)
from src.html_preprocessor import preprocess_html


# ---------------------------------------------------------------------------
# 1. Valid WARC opens correctly (ArchiveIterator does not raise)
# ---------------------------------------------------------------------------

def test_fixture_file_exists():
    """The synthetic WARC fixture must be present on disk before tests run."""
    assert os.path.isfile(WARC_FIXTURE), f"Fixture not found: {WARC_FIXTURE}"


# ---------------------------------------------------------------------------
# 2. HTML records can be listed and count is 6
# ---------------------------------------------------------------------------

def test_list_html_records_count():
    records = list_html_records(WARC_FIXTURE)
    assert len(records) == 6, f"Expected 6 HTML records, got {len(records)}"


# ---------------------------------------------------------------------------
# 3. URLs are preserved correctly
# ---------------------------------------------------------------------------

def test_list_html_records_urls():
    records = list_html_records(WARC_FIXTURE)
    found_urls = {r["url"] for r in records}
    for expected_url in EXPECTED_HTML_URLS:
        assert expected_url in found_urls, f"Expected URL not found: {expected_url}"


# ---------------------------------------------------------------------------
# 4. HTML payload is decoded and non-empty
# ---------------------------------------------------------------------------

def test_extract_html_samples_payloads_nonempty():
    samples = extract_html_samples(WARC_FIXTURE)
    assert len(samples) == 6
    for sample in samples:
        assert isinstance(sample["html"], str), "html field must be a string"
        assert len(sample["html"]) > 50, f"HTML payload suspiciously short for {sample['url']}"


# ---------------------------------------------------------------------------
# 5. Non-HTML records are filtered out (text/plain and image/png excluded)
# ---------------------------------------------------------------------------

def test_non_html_records_filtered():
    """
    The fixture has 6 HTML records + 1 text/plain + 1 image/png.
    list_html_records and extract_html_samples must return only the 6 HTML ones.
    """
    records = list_html_records(WARC_FIXTURE)
    for r in records:
        ct = r["content_type"].lower()
        assert "text/html" in ct or "xhtml" in ct, (
            f"Non-HTML record slipped through: {r['url']} ({r['content_type']})"
        )


# ---------------------------------------------------------------------------
# 6. read_html_record() retrieves a specific URL
# ---------------------------------------------------------------------------

def test_read_html_record_by_url():
    target = "https://example.org/articles/page03"
    record = read_html_record(WARC_FIXTURE, target)
    assert record is not None, f"Expected to find record for {target}"
    assert record["url"] == target
    assert len(record["html"]) > 0


# ---------------------------------------------------------------------------
# 7. extract_html_samples(limit=3) is deterministic
# ---------------------------------------------------------------------------

def test_extract_html_samples_limit_deterministic():
    run1 = extract_html_samples(WARC_FIXTURE, limit=3)
    run2 = extract_html_samples(WARC_FIXTURE, limit=3)
    assert len(run1) == 3
    assert len(run2) == 3
    for r1, r2 in zip(run1, run2):
        assert r1["url"] == r2["url"]
        assert r1["html"] == r2["html"]


# ---------------------------------------------------------------------------
# 8. Invalid WARC path raises FileNotFoundError
# ---------------------------------------------------------------------------

def test_missing_warc_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        list_html_records("/nonexistent/path/to/file.warc")


def test_missing_warc_extract_raises():
    with pytest.raises(FileNotFoundError):
        extract_html_samples("/nonexistent/file.warc")


# ---------------------------------------------------------------------------
# 9. validate_warc_input() returns valid:True for fixture
# ---------------------------------------------------------------------------

def test_validate_warc_input_valid():
    result = validate_warc_input(WARC_FIXTURE)
    assert result["valid"] is True, f"Expected valid=True, got: {result}"
    assert result["total_records"] > 0
    assert result["html_records"] == 6


# ---------------------------------------------------------------------------
# 10. SHA-256 is correct and reproducible
# ---------------------------------------------------------------------------

def test_sha256_correct():
    sha = calculate_warc_sha256(WARC_FIXTURE)
    assert sha == KNOWN_SHA256, (
        f"SHA-256 mismatch.\n  Expected: {KNOWN_SHA256}\n  Got:      {sha}"
    )


def test_sha256_reproducible():
    sha1 = calculate_warc_sha256(WARC_FIXTURE)
    sha2 = calculate_warc_sha256(WARC_FIXTURE)
    assert sha1 == sha2


# ---------------------------------------------------------------------------
# 11. split_warc_records() produces disjoint URL sets
# ---------------------------------------------------------------------------

def test_split_warc_records_disjoint():
    all_records = extract_html_samples(WARC_FIXTURE)
    gen, val, held_out = split_warc_records(all_records, gen_count=3, val_count=2, test_count=1)

    gen_urls = {r["url"] for r in gen}
    val_urls = {r["url"] for r in val}
    held_urls = {r["url"] for r in held_out}

    assert gen_urls.isdisjoint(val_urls), "Gen and Val URL sets overlap"
    assert gen_urls.isdisjoint(held_urls), "Gen and Held-Out URL sets overlap"
    assert val_urls.isdisjoint(held_urls), "Val and Held-Out URL sets overlap"
    assert len(gen) == 3
    assert len(val) == 2
    assert len(held_out) == 1


# ---------------------------------------------------------------------------
# 12. split_warc_records() with insufficient records raises ValueError
# ---------------------------------------------------------------------------

def test_split_warc_records_insufficient_raises():
    tiny = [{"url": "https://example.org/a", "html": "<html/>"}]
    with pytest.raises(ValueError, match="Insufficient records"):
        split_warc_records(tiny, gen_count=3, val_count=2, test_count=1)


# ---------------------------------------------------------------------------
# 13. HTML from WARC flows through preprocess_html() without error
# ---------------------------------------------------------------------------

def test_html_from_warc_preprocessable():
    samples = extract_html_samples(WARC_FIXTURE, limit=2)
    for sample in samples:
        cleaned = preprocess_html(sample["html"])
        assert isinstance(cleaned, str)
        assert len(cleaned) > 0, f"Preprocessed HTML is empty for {sample['url']}"

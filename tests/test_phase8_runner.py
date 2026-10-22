"""
Phase 8 Tests for CorpusAI Daily Run Orchestration, Recovery, and Operational Logging.

Validates:
1. CLI argument parsing, validation, invalid portal handling (exit 2).
2. Dry-run mode without network calls or file writes (exit 0).
3. Incremental run requirement for prior successful baseline (exit 4).
4. Successful baseline crawl, verified packaging, catalog advancement (exit 0).
5. Unchanged incremental crawl with zero redundant requests (exit 0).
6. Unrecoverable fetch failure and catalog protection (exit 1).
7. Crawl budget exhaustion and partial checkpointing (exit 6).
8. Structural anomaly / empty head page detection (exit 7).
9. Bounded intra-run retry on transient HTTP 5xx with exponential backoff and recovery.
10. Exhausted retries producing FETCH_FAILED with complete retry log.
11. Non-retriable HTTP 404/410 classification as removed without retry.
12. Non-retriable HTTP 401/403 access gate anomaly classification.
13. Strict request budget accounting for all retry attempts.
14. Operational logging file production for both successful and failed runs.
15. Mandatory key fields present in structured operational log file.
16. Package verification failure and catalog safety (exit 5).
17. Unchanged incremental runs producing zero new WARC artifacts.
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List
import unittest.mock as mock

import pytest

from src.archive_catalog import ArchiveCatalog
from src.archive_package import PackageVerificationResult, PackageVerifier
from src.downloader import DownloadedResource
from src.incremental_archive_policy import (
    IncrementalArchivePolicy,
    IncrementalCrawlBudget,
)
from src.incremental_run import IncrementalRun
from src.portal_state import ArticleState, ArchivePageState, PortalState
import src.corpusai_run as corpusai_run


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------

def _make_html_resource(url: str, html: str, status_code: int = 200) -> DownloadedResource:
    """Create a mock DownloadedResource containing HTML."""
    payload = html.encode("utf-8")
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()
    return DownloadedResource(
        url=url,
        status_code=status_code,
        content_type="text/html; charset=utf-8",
        payload=payload,
        sha256=digest,
        resource_type="html",
    )


SAMPLE_HEAD_HTML = """
<!DOCTYPE html>
<html>
<head><title>WordPress News</title></head>
<body>
  <div class="posts-group">
    <article class="post">
      <h2 class="entry-title"><a href="https://wordpress.org/news/2026/10/post-1/">Post 1 Title</a></h2>
    </article>
    <article class="post">
      <h2 class="entry-title"><a href="https://wordpress.org/news/2026/10/post-2/">Post 2 Title</a></h2>
    </article>
  </div>
</body>
</html>
"""

SAMPLE_ARTICLE_HTML_1 = """
<!DOCTYPE html>
<html>
<head><title>Post 1 Title</title></head>
<body>
  <header><h1>Post 1 Heading</h1></header>
  <time class="entry-date" datetime="2026-10-01T12:00:00Z">October 1, 2026</time>
  <span class="author">Alice</span>
  <div class="entry-content"><p>Body content for article 1.</p></div>
</body>
</html>
"""

SAMPLE_ARTICLE_HTML_2 = """
<!DOCTYPE html>
<html>
<head><title>Post 2 Title</title></head>
<body>
  <header><h1>Post 2 Heading</h1></header>
  <time class="entry-date" datetime="2026-10-02T12:00:00Z">October 2, 2026</time>
  <span class="author">Bob</span>
  <div class="entry-content"><p>Body content for article 2.</p></div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Test Cases 1-8: CLI Runner & Lifecycle
# ---------------------------------------------------------------------------

def test_cli_invalid_portal():
    """1. test_cli_invalid_portal: --portal unknown-portal should exit 2."""
    with pytest.raises(SystemExit) as exc_info:
        corpusai_run.main(["--portal", "unknown-portal", "--mode", "baseline"])
    assert exc_info.value.code == 2


def test_cli_dry_run():
    """2. test_cli_dry_run: --dry-run for a known portal exits 0 without making HTTP calls."""
    with mock.patch("urllib.request.urlopen") as mock_url:
        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--dry-run"])
        assert exc_info.value.code == 0
        mock_url.assert_not_called()


def test_cli_incremental_no_baseline(tmp_path):
    """3. test_cli_incremental_no_baseline: incremental mode when catalog has no successful run -> exits 4."""
    results_dir = str(tmp_path / "results")
    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")):
        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "incremental",
                "--results-dir", results_dir,
            ])
        assert exc_info.value.code == 4


def test_runner_baseline_converged_exits_0(tmp_path):
    """
    4. test_runner_baseline_converged_exits_0:
    Successful baseline run with mocked fetchers: exit code 0; catalog advances;
    log file produced; run_summary.json has CONVERGED status.
    """
    results_dir = str(tmp_path / "results")

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, SAMPLE_HEAD_HTML)
        elif "post-1" in url:
            return _make_html_resource(url, SAMPLE_ARTICLE_HTML_1)
        elif "post-2" in url:
            return _make_html_resource(url, SAMPLE_ARTICLE_HTML_2)
        return _make_html_resource(url, "<html><body>Default</body></html>")

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", results_dir,
            ])
        assert exc_info.value.code == 0

    catalog_path = os.path.join(results_dir, "catalog.json")
    assert os.path.exists(catalog_path)
    catalog = ArchiveCatalog.load_from_json(catalog_path)
    assert catalog.latest_successful_run_id is not None
    assert len(catalog.runs) == 1

    entry = catalog.runs[catalog.latest_successful_run_id]
    assert entry.status == "CONVERGED"

    # Verify run_summary.json and run.log
    run_dir = os.path.join(results_dir, entry.run_dir)
    summary_path = os.path.join(run_dir, "run_summary.json")
    assert os.path.exists(summary_path)
    with open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["convergence_status"] == "CONVERGED"
    assert summary["article_count"] == 2

    log_path = os.path.join(run_dir, "run.log")
    assert os.path.exists(log_path)
    assert os.path.getsize(log_path) > 0


def test_runner_incremental_converged_exits_0(tmp_path):
    """
    5. test_runner_incremental_converged_exits_0:
    Incremental after baseline; head unchanged; exit 0; no article body requests;
    prior provenance carried forward.
    """
    results_dir = str(tmp_path / "results")

    article_fetch_calls = []

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, SAMPLE_HEAD_HTML)
        elif "post-1" in url or "post-2" in url:
            article_fetch_calls.append(url)
            return _make_html_resource(url, SAMPLE_ARTICLE_HTML_1 if "post-1" in url else SAMPLE_ARTICLE_HTML_2)
        return _make_html_resource(url, "<html><body>Default</body></html>")

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        # Baseline run
        with pytest.raises(SystemExit) as exc1:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])
        assert exc1.value.code == 0

        baseline_fetches = len(article_fetch_calls)
        assert baseline_fetches == 2

        # Incremental run (head unchanged)
        with pytest.raises(SystemExit) as exc2:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "incremental", "--results-dir", results_dir])
        assert exc2.value.code == 0

        # No new article body requests made during unchanged incremental run!
        assert len(article_fetch_calls) == baseline_fetches

    catalog = ArchiveCatalog.load_from_json(os.path.join(results_dir, "catalog.json"))
    assert len(catalog.runs) == 2
    inc_entry = catalog.runs[catalog.latest_successful_run_id]
    assert inc_entry.mode == "INCREMENTAL"
    assert inc_entry.new_article_count == 0


def test_runner_fetch_failure_exits_1(tmp_path):
    """
    6. test_runner_fetch_failure_exits_1:
    Page fetcher raises exception on every call (exhausting retries); exit 1;
    catalog does NOT advance.
    """
    results_dir = str(tmp_path / "results")

    def failing_fetch(url, resource_type="asset"):
        raise ConnectionError(f"Simulated network drop on {url}")

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=failing_fetch), \
         mock.patch("time.sleep", return_value=None):

        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", results_dir,
            ])
        assert exc_info.value.code == 1

    catalog_path = os.path.join(results_dir, "catalog.json")
    if os.path.exists(catalog_path):
        catalog = ArchiveCatalog.load_from_json(catalog_path)
        assert catalog.latest_successful_run_id is None


def test_runner_budget_exhausted_exits_6(tmp_path):
    """
    7. test_runner_budget_exhausted_exits_6:
    Budget too small, run exits 6; catalog not advanced.
    """
    results_dir = str(tmp_path / "results")

    multi_page_head = """
    <html><body>
      <article class="post"><a href="/news/post-1">Post 1</a></article>
      <a class="wp-block-query-pagination-next" href="https://wordpress.org/news/all-posts/page/2/">Next</a>
    </body></html>
    """

    def mock_fetch(url, resource_type="asset"):
        return _make_html_resource(url, multi_page_head)

    # Patch config to have max_requests = 1 so budget is exhausted immediately after page 1
    custom_configs = dict(corpusai_run.PORTAL_CONFIGS)
    custom_configs["wordpress-news"] = dict(custom_configs["wordpress-news"])
    custom_configs["wordpress-news"]["max_requests"] = 1
    custom_configs["wordpress-news"]["max_archive_pages"] = 3

    with mock.patch.dict(corpusai_run.PORTAL_CONFIGS, custom_configs), \
         mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", results_dir,
            ])
        assert exc_info.value.code == 6

    catalog = ArchiveCatalog.load_from_json(os.path.join(results_dir, "catalog.json"))
    assert catalog.latest_successful_run_id is None


def test_runner_anomaly_exits_7(tmp_path):
    """
    8. test_runner_anomaly_exits_7:
    Empty head page triggers ARCHIVE_EMPTY_UNEXPECTEDLY anomaly; exits 7; catalog not advanced.
    """
    results_dir = str(tmp_path / "results")
    empty_head = "<html><body><p>No posts available</p></body></html>"

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", return_value=_make_html_resource("https://wordpress.org/news/all-posts/", empty_head)):

        with pytest.raises(SystemExit) as exc_info:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", results_dir,
            ])
        assert exc_info.value.code == 7

    catalog = ArchiveCatalog.load_from_json(os.path.join(results_dir, "catalog.json"))
    assert catalog.latest_successful_run_id is None


# ---------------------------------------------------------------------------
# Test Cases 9-13: Intra-Run Retry Policy
# ---------------------------------------------------------------------------

def test_retry_transient_5xx_then_success():
    """
    9. test_retry_transient_5xx_then_success:
    Article fetcher returns 500 twice then 200; article captured;
    retry events recorded in IncrementalRun.retry_log.
    """
    call_counts = {"article": 0}

    def mock_page_fetcher(url: str):
        return {
            "status_code": 200,
            "article_urls": ["https://example.com/art-1"],
            "next_page_url": None,
        }

    def mock_art_fetcher(url: str):
        call_counts["article"] += 1
        if call_counts["article"] <= 2:
            return {"status_code": 500}
        return {
            "status_code": 200,
            "title": "Recovered Article",
            "content_hash": "hash-success",
            "content_capture_status": "CAPTURED",
        }

    policy = IncrementalArchivePolicy(
        portal_id="test-portal",
        head_page_url="https://example.com/archive",
        budget=IncrementalCrawlBudget(max_retries=2, max_requests=10),
        sleep_fn=lambda _: None,
    )

    state, run, diff = policy.execute_incremental_crawl(mock_page_fetcher, mock_art_fetcher)

    assert call_counts["article"] == 3
    assert state.articles["https://example.com/art-1"].status != "FETCH_FAILED"
    assert state.articles["https://example.com/art-1"].content_capture_status == "CAPTURED"
    assert len(run.retry_log) == 2
    assert run.retry_log[0]["attempt"] == 1
    assert run.retry_log[0]["delay_seconds"] == 2.0
    assert run.retry_log[1]["attempt"] == 2
    assert run.retry_log[1]["delay_seconds"] == 4.0


def test_retry_exhausted_produces_fetch_failed():
    """
    10. test_retry_exhausted_produces_fetch_failed:
    Article fetcher always returns 500; max_retries=2 -> 3 attempts,
    then FETCH_FAILED; retry_log has 3 entries.
    """
    attempts = 0

    def mock_page_fetcher(url: str):
        return {
            "status_code": 200,
            "article_urls": ["https://example.com/art-failed"],
            "next_page_url": None,
        }

    def mock_art_fetcher(url: str):
        nonlocal attempts
        attempts += 1
        return {"status_code": 500}

    policy = IncrementalArchivePolicy(
        portal_id="test-portal",
        head_page_url="https://example.com/archive",
        budget=IncrementalCrawlBudget(max_retries=2, max_requests=10),
        sleep_fn=lambda _: None,
    )

    state, run, diff = policy.execute_incremental_crawl(mock_page_fetcher, mock_art_fetcher)

    assert attempts == 3
    art = state.articles["https://example.com/art-failed"]
    assert art.status == "FETCH_FAILED"
    assert art.content_capture_status == "FAILED"
    assert len(run.retry_log) == 3
    assert run.retry_log[2]["attempt"] == 3
    assert run.retry_log[2]["delay_seconds"] == 0.0
    assert any("Article fetch failed" in w for w in run.warnings)


def test_retry_404_not_retried():
    """
    11. test_retry_404_not_retried:
    404 response is NOT retried; recorded as missing/removed, not FETCH_FAILED;
    retry_log remains empty.
    """
    call_counts = {"count": 0}

    def mock_page_fetcher(url: str):
        return {
            "status_code": 200,
            "article_urls": ["https://example.com/art-removed"],
            "next_page_url": None,
        }

    def mock_art_fetcher(url: str):
        call_counts["count"] += 1
        return {"status_code": 404}

    policy = IncrementalArchivePolicy(
        portal_id="test-portal",
        head_page_url="https://example.com/archive",
        budget=IncrementalCrawlBudget(max_retries=2, max_requests=10),
        sleep_fn=lambda _: None,
    )

    state, run, diff = policy.execute_incremental_crawl(mock_page_fetcher, mock_art_fetcher)

    assert call_counts["count"] == 1
    art = state.articles["https://example.com/art-removed"]
    assert art.status == "REMOVED"
    assert art.content_capture_status == "REMOVED"
    assert len(run.retry_log) == 0


def test_retry_403_not_retried_as_transient():
    """
    12. test_retry_403_not_retried_as_transient:
    403 response triggers access-gate anomaly, not retry; retry_log empty.
    """
    page_calls = 0

    def mock_page_fetcher(url: str):
        nonlocal page_calls
        page_calls += 1
        return {"status_code": 403, "article_urls": []}

    policy = IncrementalArchivePolicy(
        portal_id="test-portal",
        head_page_url="https://example.com/archive",
        budget=IncrementalCrawlBudget(max_retries=2, max_requests=10),
        sleep_fn=lambda _: None,
    )

    state, run, diff = policy.execute_incremental_crawl(mock_page_fetcher)

    assert page_calls == 1
    assert "ACCESS_GATE_DETECTED" in run.anomalies_detected
    assert run.convergence_status == "MANUAL_REVIEW_REQUIRED"
    assert len(run.retry_log) == 0


def test_retry_budget_accounting():
    """
    13. test_retry_budget_accounting:
    With max_requests=6 and 2 retries on one article, total requests accounted correctly.
    """
    def mock_page_fetcher(url: str):
        return {
            "status_code": 200,
            "article_urls": ["https://example.com/art-retried", "https://example.com/art-clean"],
            "next_page_url": None,
        }

    art_retried_calls = 0

    def mock_art_fetcher(url: str):
        nonlocal art_retried_calls
        if "art-retried" in url:
            art_retried_calls += 1
            if art_retried_calls <= 2:
                return {"status_code": 500}
            return {"status_code": 200, "content_hash": "h1"}
        return {"status_code": 200, "content_hash": "h2"}

    policy = IncrementalArchivePolicy(
        portal_id="test-portal",
        head_page_url="https://example.com/archive",
        budget=IncrementalCrawlBudget(max_requests=6, max_retries=2),
        sleep_fn=lambda _: None,
    )

    state, run, diff = policy.execute_incremental_crawl(mock_page_fetcher, mock_art_fetcher)

    # 1 page request + 3 requests for art-retried (1 initial + 2 retries) + 1 request for art-clean = 5
    assert policy.requests_made == 5
    assert run.requests_made == 5


# ---------------------------------------------------------------------------
# Test Cases 14-17: Logging, Verification & Storage Integrity
# ---------------------------------------------------------------------------

def test_logging_file_produced(tmp_path):
    """
    14. test_logging_file_produced:
    After any run (even failed), run.log exists in run directory with non-zero content.
    """
    results_dir = str(tmp_path / "results")

    def failing_fetch(url, resource_type="asset"):
        raise ConnectionError("Network down")

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=failing_fetch), \
         mock.patch("time.sleep", return_value=None):

        with pytest.raises(SystemExit):
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])

    # Find the run directory created
    runs_dir = os.path.join(results_dir, "runs")
    assert os.path.exists(runs_dir)
    run_folders = os.listdir(runs_dir)
    assert len(run_folders) >= 1

    log_file = os.path.join(runs_dir, run_folders[0], "run.log")
    assert os.path.exists(log_file)
    assert os.path.getsize(log_file) > 0


def test_logging_contains_key_fields(tmp_path):
    """
    15. test_logging_contains_key_fields:
    Log file contains run_id, convergence_status, portal_id strings.
    """
    results_dir = str(tmp_path / "results")

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, SAMPLE_HEAD_HTML)
        return _make_html_resource(url, SAMPLE_ARTICLE_HTML_1)

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])
        assert exc.value.code == 0

    runs_dir = os.path.join(results_dir, "runs")
    run_folders = os.listdir(runs_dir)
    log_file = os.path.join(runs_dir, run_folders[0], "run.log")

    with open(log_file, "r", encoding="utf-8") as f:
        log_content = f.read()

    assert "wordpress-news" in log_content
    assert "Run ID:" in log_content or "run-" in log_content
    assert "CONVERGED" in log_content


def test_package_verification_failure_exits_5(tmp_path):
    """
    16. test_package_verification_failure_exits_5:
    Mock PackageVerifier.verify to return failure; exit 5; catalog not advanced.
    """
    results_dir = str(tmp_path / "results")

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, SAMPLE_HEAD_HTML)
        return _make_html_resource(url, SAMPLE_ARTICLE_HTML_1)

    invalid_result = PackageVerificationResult(
        is_valid=False,
        run_id="mock-run",
        portal_id="wordpress-news",
        package_dir="mock-pkg-dir",
        metadata_errors=["Mocked tampering in manifest checksum"],
    )

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch), \
         mock.patch("src.archive_package.PackageVerifier.verify_package", return_value=invalid_result), \
         mock.patch("src.archive_package.PackageVerifier.verify", return_value=invalid_result):

        with pytest.raises(SystemExit) as exc:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])
        assert exc.value.code == 5

    catalog = ArchiveCatalog.load_from_json(os.path.join(results_dir, "catalog.json"))
    assert catalog.latest_successful_run_id is None


def test_unchanged_run_no_new_warc_artifacts(tmp_path):
    """
    17. test_unchanged_run_no_new_warc_artifacts:
    In an unchanged incremental run, new_article_count == 0 and
    no new WARC provenance entries are created.
    """
    results_dir = str(tmp_path / "results")

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, SAMPLE_HEAD_HTML)
        return _make_html_resource(url, SAMPLE_ARTICLE_HTML_1)

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        # Baseline run
        with pytest.raises(SystemExit) as exc1:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])
        assert exc1.value.code == 0

        # Incremental run (unchanged)
        with pytest.raises(SystemExit) as exc2:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "incremental", "--results-dir", results_dir])
        assert exc2.value.code == 0

    catalog = ArchiveCatalog.load_from_json(os.path.join(results_dir, "catalog.json"))
    assert len(catalog.runs) == 2

    latest_run_id = catalog.latest_successful_run_id
    inc_entry = catalog.runs[latest_run_id]
    assert inc_entry.mode == "INCREMENTAL"
    assert inc_entry.new_article_count == 0

    inc_run_dir = os.path.join(results_dir, inc_entry.run_dir)
    inc_warc = os.path.join(inc_run_dir, "content_html.warc.gz")
    assert not os.path.exists(inc_warc)

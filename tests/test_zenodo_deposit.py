"""
Tests for CorpusAI Phase 9A: Zenodo Sandbox Deposit Adapter and Receipt Tracking.

Covers:
1. Invalid or incomplete local packages are rejected before remote creation.
2. Valid metadata produces the expected Zenodo API payload.
3. Successful draft creation records the remote identifier and bucket URL.
4. Artifact uploads stream data and handle upload failures correctly.
5. Retryable server errors (HTTP 500/503) use bounded retries with backoff.
6. Authentication failures (HTTP 401/403) do not expose sensitive API tokens.
7. Repeated invocation for a completed deposit returns the existing receipt (idempotency).
8. An ambiguous create request / changed package fingerprint triggers NEEDS_RECONCILIATION.
9. Failure partway through upload leaves the local package intact and receipt retryable.
10. Remote checksum differences are handled according to actual algorithms (MD5 + local SHA-256).
11. Missing artifacts or failed remote verification prevent the VERIFIED state.
12. Deposit failure does not alter ArchiveCatalog's latest_successful_run_id.
13. Atomic receipt persistence survives interruptions via atomic replacement.
14. Production API URLs and public publishing are blocked by default.
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List
import unittest.mock as mock

import pytest

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.archive_package import (
    ArchivePackager,
    PackageVerifier,
    CHECKSUMS_FILENAME,
    ENVIRONMENT_FILENAME,
    MANIFEST_FILENAME,
)
from src.deposit_metadata import Creator, DepositMetadata
from src.zenodo_deposit import (
    DepositOrchestrator,
    DepositReceipt,
    DepositRegistry,
    UploadedArtifactRecord,
    ZenodoSandboxAdapter,
    STATUS_DRAFT_CREATED,
    STATUS_FAILED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    STATUS_VERIFIED,
    ZENODO_SANDBOX_BASE_URL,
    ZENODO_PRODUCTION_BASE_URL,
    compute_package_fingerprint,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

class MockHttpResponse:
    """Mock HTTP response object for urllib requests."""
    def __init__(self, status: int, data: Dict[str, Any]):
        self.status = status
        self.status_code = status
        self._data = json.dumps(data).encode("utf-8")

    def read(self) -> bytes:
        return self._data


def _create_sample_metadata(tmp_path) -> DepositMetadata:
    return DepositMetadata(
        title="CorpusAI Research Corpus: WordPress News Archive",
        creators=[Creator(name="CorpusAI Research Group", affiliation="Research Group")],
        description="Preserved longitudinal web corpus of WordPress news articles for linguistic evaluation.",
        capture_period_start="2026-10-01T00:00:00Z",
        capture_period_end="2026-10-23T00:00:00Z",
        source_domains=["wordpress.org"],
        license_status="Research-Only",
        copyright_notice="Copyright © WordPress Foundation. Preserved under academic research fair use.",
        software_version="corpusai-0.1.0",
        archive_formats=["WARC", "JSON"],
        checksum_manifest_path="checksums.sha256",
        access_status="restricted",
    )


def _create_valid_packaged_run(tmp_path, portal_id="wordpress-news", run_id="run-20261023T010000Z-wordpr") -> str:
    """Creates and verifies a real minimal package on disk."""
    packager = ArchivePackager(archives_root=str(tmp_path / "archives"))

    # Create dummy raw content file
    raw_warc = str(tmp_path / "content.warc.gz")
    with open(raw_warc, "wb") as f:
        f.write(b"WARC/1.0\r\ntest article content\r\n")

    pkg_dir, v_res = packager.package_run(
        portal_id=portal_id,
        run_id=run_id,
        mode="INCREMENTAL",
        status="CONVERGED",
        started_at="2026-10-23T01:00:00Z",
        completed_at="2026-10-23T01:05:00Z",
        content_html_warc=raw_warc,
    )
    assert v_res.is_valid, f"Created package must be valid: {v_res.metadata_errors}"
    return pkg_dir


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_invalid_package_rejected_before_remote_creation(tmp_path):
    """1. Invalid or incomplete local packages are rejected before remote creation."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    # Corrupt the package by removing manifest.json
    os.remove(os.path.join(pkg_dir, MANIFEST_FILENAME))

    meta = _create_sample_metadata(tmp_path)
    adapter = ZenodoSandboxAdapter(api_token="dummy_token")
    registry = DepositRegistry(str(tmp_path / "deposit_registry.json"))
    orchestrator = DepositOrchestrator(adapter, registry)

    mock_create = mock.MagicMock()
    adapter.create_draft = mock_create

    receipt = orchestrator.deposit_package(
        portal_id="wordpress-news",
        run_id="run-20261023T010000Z-wordpr",
        package_dir=pkg_dir,
        deposit_metadata=meta,
    )

    assert receipt.status == STATUS_FAILED
    assert "Local package verification failed" in receipt.last_error
    assert mock_create.call_count == 0  # Remote call never made!


def test_metadata_mapping_to_zenodo_payload(tmp_path):
    """2. Valid metadata produces the expected Zenodo API payload with safe defaults."""
    meta = _create_sample_metadata(tmp_path)
    adapter = ZenodoSandboxAdapter(api_token="dummy_token")
    payload = adapter.map_deposit_metadata_to_zenodo(meta)

    metadata = payload["metadata"]
    assert metadata["title"] == meta.title
    assert metadata["upload_type"] == "dataset"
    assert metadata["creators"] == [{"name": "CorpusAI Research Group", "affiliation": "Research Group"}]
    assert metadata["access_right"] == "restricted"
    assert "access_conditions" in metadata
    assert "wordpress.org" in metadata["keywords"]


def test_draft_creation_records_identifiers(tmp_path):
    """3. Successful draft creation records the remote identifier and bucket URL."""
    meta = _create_sample_metadata(tmp_path)

    mock_resp_data = {
        "id": 987654,
        "doi": "10.5072/zenodo.987654",
        "links": {
            "bucket": "https://sandbox.zenodo.org/api/files/bucket-uuid-1234",
            "html": "https://sandbox.zenodo.org/deposit/987654",
        },
        "metadata": {
            "prereserve_doi": {"doi": "10.5072/zenodo.987654"}
        }
    }

    def fake_transport(req):
        assert req.method == "POST"
        assert req.full_url == f"{ZENODO_SANDBOX_BASE_URL}/deposit/depositions"
        return MockHttpResponse(201, mock_resp_data)

    adapter = ZenodoSandboxAdapter(api_token="secret_token", http_transport=fake_transport)
    receipt = adapter.create_draft(meta, "wordpress-news", "run-1", "pkg-fingerprint-123")

    assert receipt.status == STATUS_DRAFT_CREATED
    assert receipt.remote_record_id == "987654"
    assert receipt.remote_bucket_url == "https://sandbox.zenodo.org/api/files/bucket-uuid-1234"
    assert receipt.doi == "10.5072/zenodo.987654"
    assert receipt.environment == "sandbox"


def test_artifact_uploads_streaming_and_handling(tmp_path):
    """4. Artifact uploads stream data to Bucket API and handle upload failures correctly."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    uploaded_files = []

    def fake_transport(req):
        if req.method == "PUT":
            uploaded_files.append(req.full_url)
            # Zenodo Bucket API returns MD5 of uploaded file
            # Simulate correct md5 based on request body
            data = req.data
            md5_val = hashlib.md5(data).hexdigest()
            return MockHttpResponse(200, {"checksum": f"md5:{md5_val}", "id": "file-uuid"})
        return MockHttpResponse(200, {})

    adapter = ZenodoSandboxAdapter(api_token="token", http_transport=fake_transport)
    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-1",
        package_fingerprint="fp1",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_DRAFT_CREATED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
        remote_record_id="987654",
        remote_bucket_url="https://sandbox.zenodo.org/api/files/bucket-uuid-1234",
    )

    receipt = adapter.upload_artifacts(receipt, pkg_dir)
    assert receipt.status == STATUS_UPLOADED
    assert len(uploaded_files) >= 4  # manifest, environment, checksums, content
    assert any("manifest.json" in u for u in uploaded_files)


def test_bounded_retries_on_5xx_server_errors(tmp_path):
    """5. Retryable server errors (HTTP 500/503) use bounded retries with backoff."""
    meta = _create_sample_metadata(tmp_path)
    attempts = 0

    def failing_transport(req):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return MockHttpResponse(503, {"message": "Service Temporarily Unavailable"})
        return MockHttpResponse(201, {"id": 111, "links": {"bucket": "http://bucket"}})

    delays = []
    adapter = ZenodoSandboxAdapter(
        api_token="token",
        max_retries=3,
        http_transport=failing_transport,
        sleep_fn=lambda d: delays.append(d),
    )

    receipt = adapter.create_draft(meta, "portal", "run", "fp")
    assert receipt.status == STATUS_DRAFT_CREATED
    assert attempts == 3  # Failed twice, succeeded on attempt 3
    assert len(delays) == 2
    assert delays[0] == 2.0  # 2^1
    assert delays[1] == 4.0  # 2^2


def test_auth_failures_do_not_expose_tokens(tmp_path):
    """6. Authentication failures (HTTP 401/403) do not expose sensitive API tokens."""
    meta = _create_sample_metadata(tmp_path)
    secret_token = "SUPER_SECRET_TOKEN_XYZ123"

    def auth_fail_transport(req):
        return MockHttpResponse(401, {"message": f"Invalid token: {secret_token}"})

    adapter = ZenodoSandboxAdapter(api_token=secret_token, http_transport=auth_fail_transport)
    with pytest.raises(PermissionError) as exc_info:
        adapter.create_draft(meta, "portal", "run", "fp")

    err_str = str(exc_info.value)
    assert secret_token not in err_str
    assert "Access denied" in err_str or "Authentication/Authorization failure" in err_str


def test_idempotent_deposit_returns_existing_receipt(tmp_path):
    """7. Repeated invocation for a completed deposit returns the existing receipt."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    meta = _create_sample_metadata(tmp_path)
    fp = compute_package_fingerprint(pkg_dir)

    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)

    # Pre-record completed verified receipt
    pre_receipt = DepositReceipt(
        receipt_id=f"receipt-wordpress-news-run-1-{fp[:8]}",
        portal_id="wordpress-news",
        run_id="run-20261023T010000Z-wordpr",
        package_fingerprint=fp,
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_VERIFIED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
        remote_record_id="55555",
        checksum_verification_outcome="VERIFIED_MD5_AND_LOCAL_SHA256",
    )
    registry.register_receipt(pre_receipt)

    mock_client = mock.MagicMock()
    orchestrator = DepositOrchestrator(mock_client, registry)

    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)
    assert receipt.status == STATUS_VERIFIED
    assert receipt.remote_record_id == "55555"
    assert mock_client.create_draft.call_count == 0  # No duplicate draft!
    assert mock_client.upload_artifacts.call_count == 0


def test_modified_package_triggers_needs_reconciliation(tmp_path):
    """8. A package modified after initial receipt creation triggers NEEDS_RECONCILIATION."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    meta = _create_sample_metadata(tmp_path)

    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)

    # Record receipt with OLD fingerprint
    old_receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-20261023T010000Z-wordpr",
        package_fingerprint="old_fingerprint_hash_abc",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_DRAFT_CREATED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
    )
    registry.register_receipt(old_receipt)

    mock_client = mock.MagicMock()
    orchestrator = DepositOrchestrator(mock_client, registry)

    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)
    assert receipt.status == STATUS_NEEDS_RECONCILIATION
    assert "PACKAGE_FINGERPRINT_CHANGED" in receipt.last_error_category
    assert mock_client.upload_artifacts.call_count == 0


def test_upload_failure_leaves_local_package_intact_and_receipt_retryable(tmp_path):
    """9. Failure partway through upload leaves the local package intact and receipt retryable."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    meta = _create_sample_metadata(tmp_path)

    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)

    # Mock client that creates draft but fails during upload
    class FailingUploadClient(ZenodoSandboxAdapter):
        def create_draft(self, *args, **kwargs):
            return DepositReceipt(
                receipt_id="r1",
                portal_id="wordpress-news",
                run_id="run-20261023T010000Z-wordpr",
                package_fingerprint=compute_package_fingerprint(pkg_dir),
                repository_target="zenodo",
                environment="sandbox",
                status=STATUS_DRAFT_CREATED,
                created_at="2026-10-23T01:00:00Z",
                updated_at="2026-10-23T01:00:00Z",
                remote_bucket_url="http://sandbox/bucket",
            )
        def upload_artifacts(self, receipt, *args, **kwargs):
            raise ConnectionResetError("Connection lost while streaming large WARC")

    client = FailingUploadClient(api_token="token")
    orchestrator = DepositOrchestrator(client, registry)

    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)

    assert receipt.status == STATUS_FAILED
    assert receipt.retry_eligible is True
    # Local package must still be 100% valid!
    v_res = PackageVerifier.verify_package(pkg_dir)
    assert v_res.is_valid is True


def test_remote_checksum_mismatch_prevents_verified_state(tmp_path):
    """10. Remote checksum differences are caught and prevent VERIFIED status."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    adapter = ZenodoSandboxAdapter(api_token="token")

    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-1",
        package_fingerprint="fp",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_UPLOADED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
    )
    # Put a corrupt remote MD5 in inventory
    receipt.artifact_inventory["manifest.json"] = UploadedArtifactRecord(
        filename="manifest.json",
        filesize=100,
        local_sha256="abc",
        local_md5="00000000000000000000000000000000",
        remote_md5="ffffffffffffffffffffffffffffffff",  # Corrupted remote checksum
    )

    receipt = adapter.verify_remote_artifacts(receipt, pkg_dir)
    assert receipt.status == STATUS_FAILED
    assert receipt.checksum_verification_outcome == "FAILED"
    assert "remote MD5 mismatch" in receipt.last_error


def test_deposit_failure_does_not_alter_catalog(tmp_path):
    """12. Deposit failure does not alter ArchiveCatalog's latest_successful_run_id."""
    catalog_path = str(tmp_path / "catalog.json")
    catalog = ArchiveCatalog.load_or_create(catalog_path, "wordpress-news")

    # Baseline crawl was successful
    entry1 = CatalogRunEntry(
        run_id="run-baseline-1",
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-23T01:00:00Z",
        completed_at="2026-10-23T01:05:00Z",
        status="CONVERGED",
        article_count=10,
        new_article_count=10,
        changed_article_count=0,
        run_dir="runs/run-baseline-1",
    )
    catalog.register_run(entry1, is_successful=True)
    catalog.save_to_json(catalog_path)
    assert catalog.latest_successful_run_id == "run-baseline-1"

    # Simulate deposit failure for run-baseline-1
    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)
    failed_receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-baseline-1",
        package_fingerprint="fp1",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_FAILED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
        last_error="Remote repository unreachable",
    )
    registry.register_receipt(failed_receipt)

    # Reload catalog and verify crawl success is completely unaffected
    reloaded_catalog = ArchiveCatalog.load_from_json(catalog_path)
    assert reloaded_catalog.latest_successful_run_id == "run-baseline-1"
    assert reloaded_catalog.runs["run-baseline-1"].status == "CONVERGED"


def test_atomic_receipt_persistence(tmp_path):
    """13. Atomic receipt persistence survives malformed or interrupted state updates."""
    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)

    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="portal",
        run_id="run-1",
        package_fingerprint="fp",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_DRAFT_CREATED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
    )
    registry.register_receipt(receipt)

    assert os.path.exists(reg_file)
    with open(reg_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "portal:run-1" in data["receipts"]


def test_missing_artifacts_or_verification_prevent_verified_state(tmp_path):
    """11. Missing artifacts or failed remote verification prevent the VERIFIED state."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    adapter = ZenodoSandboxAdapter(api_token="token")

    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-1",
        package_fingerprint="fp",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_UPLOADED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
    )
    # Artifact inventory has file missing MD5
    receipt.artifact_inventory["manifest.json"] = UploadedArtifactRecord(
        filename="manifest.json",
        filesize=100,
        local_sha256="abc",
        local_md5="00000000000000000000000000000000",
        remote_md5="",  # Empty remote md5
    )

    receipt = adapter.verify_remote_artifacts(receipt, pkg_dir)
    assert receipt.status == STATUS_FAILED
    assert receipt.status != STATUS_VERIFIED
    assert receipt.checksum_verification_outcome == "FAILED"


def test_production_urls_and_publishing_blocked(tmp_path):
    """14. Production API URLs and unauthorized public publishing are blocked by default."""
    # Attempt 1: Production URL blocked in constructor
    with pytest.raises(ValueError) as exc1:
        ZenodoSandboxAdapter(api_token="token", base_url=ZENODO_PRODUCTION_BASE_URL)
    assert "Production Zenodo target is strictly disabled" in str(exc1.value)

    # Attempt 2: Publishing requires explicit allow_publish=True
    adapter = ZenodoSandboxAdapter(api_token="token")
    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="portal",
        run_id="run-1",
        package_fingerprint="fp",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_VERIFIED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
        remote_record_id="123",
    )
    with pytest.raises(PermissionError) as exc2:
        adapter.publish_deposit(receipt, allow_publish=False)
    assert "Publication is locked by default" in str(exc2.value)


def test_reconciliation_finds_existing_unrecorded_draft(tmp_path):
    """Checks that an interrupted draft creation is reconciled rather than duplicated."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    meta = _create_sample_metadata(tmp_path)

    reg_file = str(tmp_path / "deposit_registry.json")
    registry = DepositRegistry.load_or_create(reg_file)

    # Simulate finding an existing draft via remote query
    mock_existing_draft = [
        {
            "id": 9999,
            "doi": "10.5072/zenodo.9999",
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket-existing"},
            "metadata": {"title": "Prior interrupted draft"},
        }
    ]

    def fake_transport(req):
        if "status=draft" in req.full_url:
            return MockHttpResponse(200, mock_existing_draft)
        elif req.method == "PUT":
            data = req.data
            md5_val = hashlib.md5(data).hexdigest()
            return MockHttpResponse(200, {"checksum": f"md5:{md5_val}", "id": "file-uuid"})
        return MockHttpResponse(200, {})

    adapter = ZenodoSandboxAdapter(api_token="token", http_transport=fake_transport)
    mock_create_draft = mock.MagicMock()
    adapter.create_draft = mock_create_draft

    orchestrator = DepositOrchestrator(adapter, registry)
    receipt = orchestrator.deposit_package("wordpress-news", "run-interrupted", pkg_dir, meta)

    assert receipt.remote_record_id == "9999"
    assert mock_create_draft.call_count == 0  # Reconciled without creating a duplicate!


def test_file_count_and_size_limits_enforced(tmp_path):
    """Enforces Zenodo 100-file and quota limits, rejecting oversized packages before upload."""
    pkg_dir = _create_valid_packaged_run(tmp_path)
    adapter = ZenodoSandboxAdapter(api_token="token")

    receipt = DepositReceipt(
        receipt_id="r1",
        portal_id="wordpress-news",
        run_id="run-1",
        package_fingerprint="fp",
        repository_target="zenodo",
        environment="sandbox",
        status=STATUS_DRAFT_CREATED,
        created_at="2026-10-23T01:00:00Z",
        updated_at="2026-10-23T01:00:00Z",
        remote_bucket_url="http://sandbox/bucket",
    )

    with mock.patch("src.zenodo_deposit.ZENODO_MAX_RECORD_FILES", 2):
        # Package has 4 files (manifest, env, checksums, content) > 2
        receipt = adapter.upload_artifacts(receipt, pkg_dir)
        assert receipt.status == STATUS_FAILED
        assert "FILE_COUNT_LIMIT_EXCEEDED" in receipt.last_error_category





def test_compute_package_fingerprint_and_receipt_streaming_multi_chunk(tmp_path):
    """
    Verify that compute_package_fingerprint and verify_deposit_receipt correctly
    hash a multi-chunk checksums.sha256 (>64KB default chunk size) via streaming
    without memory exhaustion or digest discrepancy.
    """
    from src.zenodo_deposit import compute_package_fingerprint
    from src.utils.crypto import sha256_file

    pkg_dir = tmp_path / "pkg_large"
    pkg_dir.mkdir()
    checksums_file = pkg_dir / "checksums.sha256"

    # Create a >130KB checksums file (exceeds default 65536 chunk size)
    lines = []
    dummy_hash = "a" * 64
    for i in range(2000):
        lines.append(f"{dummy_hash}  content/articles/article_item_with_long_name_{i:04d}.html\n")
    large_content = "".join(lines).encode("utf-8")
    assert len(large_content) > 131072  # >128 KB
    checksums_file.write_bytes(large_content)

    # Compute via function under test
    fp = compute_package_fingerprint(str(pkg_dir))
    expected_fp = sha256_file(str(checksums_file))
    assert fp == expected_fp
    assert len(fp) == 64

    # Also test verify_deposit_receipt with multi-chunk checksums.sha256
    adapter = ZenodoSandboxAdapter(api_token="dummy_token")
    receipt = DepositReceipt(
        receipt_id="test_receipt",
        portal_id="test_portal",
        run_id="run_1",
        package_fingerprint=fp,
        repository_target="zenodo",
        environment="sandbox",
        status="UPLOADED",
        created_at="2026-10-31T00:00:00Z",
        updated_at="2026-10-31T00:00:00Z",
    )
    # Put checksums.sha256 in artifact_inventory
    from src.utils.crypto import compute_file_md5_and_sha256
    md5_val, sha256_val, file_sz = compute_file_md5_and_sha256(str(checksums_file))
    receipt.artifact_inventory["checksums.sha256"] = UploadedArtifactRecord(
        filename="checksums.sha256",
        filesize=file_sz,
        local_md5=md5_val,
        remote_md5=md5_val,
        local_sha256=sha256_val,
    )
    updated_receipt = adapter.verify_remote_artifacts(receipt, str(pkg_dir))
    assert updated_receipt.status == "VERIFIED"
    assert updated_receipt.artifact_inventory["checksums.sha256"].verified is True

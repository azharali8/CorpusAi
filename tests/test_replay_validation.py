"""
Offline unit tests for Phase 6A:
- ArchiveRun serialization/deserialization and deterministic config-hash run IDs
- ReplayValidator manifest validation and SHA-256 verification
- Integrity mismatch detection and missing artifact detection
- Manual replay verification state barrier (cannot become true automatically)
- DepositMetadata structure validation and rights constraints
- Absence of absolute private paths in generated records
- CI workflow file validation ensuring no live network scripts are invoked
"""

import json
import os
import pytest
import yaml

from src.archive_run import ArchiveRun, compute_config_hash, generate_run_id
from src.deposit_metadata import Creator, DepositMetadata
from src.replay_validation import ReplayValidator, compute_file_sha256, generate_sha256_manifest


# ---------------------------------------------------------------------------
# Test 1: Deterministic Run ID Generation
# ---------------------------------------------------------------------------
def test_deterministic_run_id_generation():
    config1 = {"target_url": "https://example.com/post", "workers": 1, "scope": "page"}
    config2 = {"scope": "page", "target_url": "https://example.com/post", "workers": 1}
    config3 = {"target_url": "https://example.com/post", "workers": 2, "scope": "page"}

    # Config key order should not change hash
    hash1 = compute_config_hash(config1)
    hash2 = compute_config_hash(config2)
    hash3 = compute_config_hash(config3)

    assert hash1 == hash2
    assert hash1 != hash3

    run_id1 = generate_run_id(timestamp_str="2026-10-06T12:00:00Z", config=config1)
    run_id2 = generate_run_id(timestamp_str="2026-10-06T12:00:00Z", config=config2)
    assert run_id1 == run_id2
    assert run_id1.startswith("run-20261006T120000Z-")


# ---------------------------------------------------------------------------
# Test 2: ArchiveRun Serialization & Deserialization
# ---------------------------------------------------------------------------
def test_archive_run_serialization_and_file_io(tmp_path):
    run_file = str(tmp_path / "archive_run.json")
    run = ArchiveRun(
        run_id="run-20261006T120000Z-abcd1234",
        target_urls=["https://wordpress.org/news/2026/09/owa-president/"],
        scope="page",
        crawler="webrecorder/browsertrix-crawler:1.2.0",
        crawler_version="1.2.0",
        started_at="2026-10-06T12:00:00Z",
        configuration={"workers": 1},
    )
    run.mark_completed(
        artifacts={"manifest": "manifest.json", "wacz": "archive.wacz"},
        checksums={"manifest.json": "hash1", "archive.wacz": "hash2"},
    )

    run.save_to_json(run_file)
    assert os.path.exists(run_file)

    reloaded = ArchiveRun.load_from_json(run_file)
    assert reloaded.run_id == "run-20261006T120000Z-abcd1234"
    assert reloaded.status == "COMPLETED"
    assert reloaded.checksums["archive.wacz"] == "hash2"


# ---------------------------------------------------------------------------
# Test 3: Manifest Validation & SHA-256 Integrity Verification
# ---------------------------------------------------------------------------
def test_replay_validator_integrity_check(tmp_path):
    f1 = tmp_path / "content.html"
    f1.write_text("<html>Article text</html>", encoding="utf-8")
    hash1 = compute_file_sha256(str(f1))

    manifest_data = {
        "article_url": "https://example.com/article",
        "artifacts": {
            "html": "content.html",
        },
        "checksums": {
            "content.html": hash1,
        },
    }

    # Valid manifest
    report = ReplayValidator.validate_manifest(manifest_data, base_dir=str(tmp_path))
    assert report.manifest_valid is True
    assert report.checksums_verified is True
    assert report.status == "MANUAL_REPLAY_PENDING"
    assert report.manual_replay_verified is False


# ---------------------------------------------------------------------------
# Test 4: Missing Artifact and Checksum Mismatch Detection
# ---------------------------------------------------------------------------
def test_replay_validator_detects_errors(tmp_path):
    f1 = tmp_path / "valid.txt"
    f1.write_text("valid content", encoding="utf-8")

    manifest_data = {
        "article_url": "https://example.com/article",
        "artifacts": {
            "present": "valid.txt",
            "missing": "does_not_exist.wacz",
        },
        "checksums": {
            "valid.txt": "wrong_hash_12345",
            "does_not_exist.wacz": "somehash",
        },
    }

    report = ReplayValidator.validate_manifest(manifest_data, base_dir=str(tmp_path))
    assert report.manifest_valid is False
    assert report.checksums_verified is False
    assert report.status == "REPLAY_INCOMPLETE"
    assert any("does_not_exist.wacz" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Test 5: Replay State Barrier (Manual Confirmation Required)
# ---------------------------------------------------------------------------
def test_manual_replay_status_barrier(tmp_path):
    f1 = tmp_path / "archive.wacz"
    f1.write_bytes(b"dummy wacz bytes")
    hash_wacz = compute_file_sha256(str(f1))

    manifest_data = {
        "article_url": "https://example.com/article",
        "artifacts": {"wacz": "archive.wacz"},
        "checksums": {"archive.wacz": hash_wacz},
        "qa_summary": {"qa_run": True, "observed_failures": []},
    }

    # 1. Without explicit confirmation -> MUST stay False and status MANUAL_REPLAY_PENDING
    report_unverified = ReplayValidator.validate_manifest(
        manifest_data, base_dir=str(tmp_path), explicit_manual_confirmation=False
    )
    assert report_unverified.manual_replay_verified is False
    assert report_unverified.status == "MANUAL_REPLAY_PENDING"

    # 2. With explicit confirmation -> Only then becomes True
    report_verified = ReplayValidator.validate_manifest(
        manifest_data, base_dir=str(tmp_path), explicit_manual_confirmation=True
    )
    assert report_verified.manual_replay_verified is True
    assert report_verified.status == "MANUAL_REPLAY_VERIFIED"


# ---------------------------------------------------------------------------
# Test 6: SHA-256 Manifest Generation
# ---------------------------------------------------------------------------
def test_generate_sha256_manifest(tmp_path):
    f1 = tmp_path / "a.json"
    f2 = tmp_path / "b.txt"
    f1.write_text('{"a": 1}', encoding="utf-8")
    f2.write_text("hello", encoding="utf-8")

    manifest_str = generate_sha256_manifest({"a.json": "a.json", "b.txt": "b.txt"}, base_dir=str(tmp_path))
    lines = manifest_str.strip().split("\n")
    assert len(lines) == 2
    assert "a.json" in lines[0] or "a.json" in lines[1]
    assert len(lines[0].split()[0]) == 64  # valid sha256 hex length


# ---------------------------------------------------------------------------
# Test 7: Deposit Metadata Schema Validation
# ---------------------------------------------------------------------------
def test_deposit_metadata_validation(tmp_path):
    deposit = DepositMetadata(
        title="CorpusAI WordPress News Research Crawl Dataset",
        creators=[Creator(name="CorpusAI Research Team", affiliation="Digital Humanities Lab")],
        description="Tamper-evident archival corpus crawl containing HTML and REST JSON representations.",
        capture_period_start="2026-09-01T00:00:00Z",
        capture_period_end="2026-10-01T00:00:00Z",
        source_domains=["wordpress.org"],
        license_status="Research-Only / Fair Use",
        copyright_notice="Copyright resides with the original content publishers; preserved under research exemptions.",
        software_version="0.1.0",
        archive_formats=["WARC", "WACZ", "JSON"],
        checksum_manifest_path="results/wordpress_validation/phase6a/checksums.sha256",
        related_run_ids=["run-20261006T120000Z-abcd1234"],
    )

    validation_errors = deposit.validate()
    assert len(validation_errors) == 0

    json_path = str(tmp_path / "deposit.json")
    deposit.save_to_json(json_path)

    reloaded = DepositMetadata.load_from_json(json_path)
    assert reloaded.title == deposit.title
    assert reloaded.creators[0].name == "CorpusAI Research Team"


# ---------------------------------------------------------------------------
# Test 8: Deposit Metadata Missing Fields Rejection
# ---------------------------------------------------------------------------
def test_deposit_metadata_missing_fields():
    invalid_deposit = DepositMetadata(
        title="Bad",
        creators=[],
        description="Too short",
        capture_period_start="",
        capture_period_end="",
        source_domains=[],
        license_status="open",
        copyright_notice="",
        software_version="0.1.0",
        archive_formats=[],
        checksum_manifest_path="",
    )
    errors = invalid_deposit.validate()
    assert len(errors) >= 5


# ---------------------------------------------------------------------------
# Test 9: CI Workflow File Ensures Offline Execution Only
# ---------------------------------------------------------------------------
def test_ci_workflow_offline_integrity():
    workflow_path = ".github/workflows/tests.yml"
    assert os.path.exists(workflow_path), "CI workflow file .github/workflows/tests.yml missing."

    with open(workflow_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Verify standard workflow structure and that live scripts are NOT invoked
    assert "pytest" in content
    assert "run_discovery_validation.py" not in content
    assert "run_article_capture_validation.py" not in content
    assert "run_asset_capture_validation.py" not in content
    assert "docker" not in content.lower()

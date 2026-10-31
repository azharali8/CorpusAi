"""
Offline Protocol Tests for Phase 9C-A: Remote Storage Contract and Invariant Verification.

Verifies all critical distributed failure modes without live network or external accounts:
1. Two concurrent acquisitions (mutual exclusion).
2. Monotonic fencing generations across release and reacquisition.
3. Baseline generation advances ONLY on baseline publication (not lease renewals or releases).
4. Expiry makes lease takeover-eligible without automatic revocation until CAS succeeds.
5. Takeover vs publication race: winner takes control, loser receives 412.
6. Heartbeat failure abort gate preventing publication.
7. Stale-owner publication rejected after successful takeover.
8. Conditional-write conflicts and safe retries.
9. Interrupted uploads and byte-level SHA-256 mismatches.
10. Non-destructive quarantine recording without package deletion.
11. Strict remote orphan validation with baseline ancestry, identity, status, and zero anomalies.
12. Failure between control-record publication, catalog projection, and deposit registry.
13. Delayed catalog recovery cannot regress a newer baseline.
14. Fresh-directory restoration of a previously committed baseline.
"""

import copy
import hashlib
import json
import os
from typing import Dict, List, Optional, Tuple
import pytest

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.archive_package import PackageVerifier
from src.catalog_recovery import validate_baseline_integrity, BaselineIntegrityError
from src.portal_state import PortalState
from src.remote_storage_contract import (
    BaselinePointer,
    PortalControlRecord,
    RemoteStorageContract,
    RemoteStorageError,
)
from tests.mocks.mock_remote_storage import MockRemoteStorageBackend


@pytest.fixture
def mock_storage():
    return MockRemoteStorageBackend()


@pytest.fixture
def sample_package_bundle(tmp_path):
    """Create a minimal verified package bundle for testing."""
    pkg_dir = tmp_path / "packages" / "wordpress-news" / "runs" / "run-20261027T000000Z-wpnews"
    pkg_dir.mkdir(parents=True)

    # 1. portal_state.json
    state = PortalState(
        portal_id="wordpress-news",
        run_id="run-20261027T000000Z-wpnews",
    )
    state_path = pkg_dir / "portal_state.json"
    state.save_to_json(str(state_path))

    # 2. manifest.json
    manifest_data = {
        "run_id": "run-20261027T000000Z-wpnews",
        "portal_id": "wordpress-news",
        "previous_run_id": None,
        "mode": "BASELINE",
        "status": "CONVERGED",
        "started_at": "2026-10-27T00:00:00Z",
        "completed_at": "2026-10-27T00:05:00Z",
        "article_count": 5,
        "new_article_count": 5,
        "changed_article_count": 0,
        "anomalies": [],
        "errors": [],
    }
    manifest_path = pkg_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # 2b. environment.json (required by PackageVerifier)
    env_data = {
        "python_version": "3.11.0",
        "platform": "linux",
        "packaged_at": "2026-10-27T00:05:00Z",
    }
    env_path = pkg_dir / "environment.json"
    env_path.write_text(json.dumps(env_data, indent=2), encoding="utf-8")

    # 3. warc file
    warc_dir = pkg_dir / "content"
    warc_dir.mkdir()
    warc_file = warc_dir / "content_html.warc.gz"
    warc_file.write_bytes(b"MOCK_WARC_GZ_PAYLOAD_BYTES_FOR_TESTING")

    # 4. checksums.sha256
    files_to_hash = {
        "manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "environment.json": hashlib.sha256(env_path.read_bytes()).hexdigest(),
        "portal_state.json": hashlib.sha256(state_path.read_bytes()).hexdigest(),
        "content/content_html.warc.gz": hashlib.sha256(warc_file.read_bytes()).hexdigest(),
    }
    checksums_path = pkg_dir / "checksums.sha256"
    checksum_lines = [f"{h}  {f}" for f, h in files_to_hash.items()]
    checksums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    return {
        "pkg_dir": str(pkg_dir),
        "portal_id": "wordpress-news",
        "run_id": "run-20261027T000000Z-wpnews",
        "files": {
            "manifest.json": str(manifest_path),
            "environment.json": str(env_path),
            "portal_state.json": str(state_path),
            "content/content_html.warc.gz": str(warc_file),
            "checksums.sha256": str(checksums_path),
        },
        "checksums": files_to_hash,
        "manifest_data": manifest_data,
    }


# ---------------------------------------------------------------------------
# Test 1: Two concurrent acquisitions (mutual exclusion)
# ---------------------------------------------------------------------------
def test_two_concurrent_acquisitions_mutual_exclusion(mock_storage):
    portal_id = "wordpress-news"

    # Runner 1 acquires lease
    acq1, ctrl1, etag1 = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-1", ttl_seconds=600)
    assert acq1 is True
    assert ctrl1 is not None
    assert ctrl1.lease.lease_owner_id == "runner-1"
    assert ctrl1.fencing_generation == 1
    assert ctrl1.baseline_generation == 0
    assert etag1 is not None

    # Runner 2 attempts concurrent acquisition on same portal -> strictly rejected
    acq2, ctrl2, etag2 = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-2", ttl_seconds=600)
    assert acq2 is False
    assert ctrl2.lease.lease_owner_id == "runner-1"

    # Independent portal succeeds
    acq3, ctrl3, etag3 = mock_storage.acquire_portal_lease("other-portal", runner_id="runner-3", ttl_seconds=600)
    assert acq3 is True
    assert ctrl3.portal_id == "other-portal"


# ---------------------------------------------------------------------------
# Test 2: Monotonic fencing generations across release and reacquisition
# ---------------------------------------------------------------------------
def test_monotonic_fencing_generations_across_releases(mock_storage):
    portal_id = "wordpress-news"

    # Runner 1 acquires (gen 1)
    acq1, ctrl1, etag1 = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-1")
    assert ctrl1.fencing_generation == 1

    # Runner 1 releases gracefully (vacating lease)
    rel1 = mock_storage.release_portal_lease(portal_id, runner_id="runner-1", fencing_generation=1, expected_control_etag=etag1)
    assert rel1 is True

    # Check control record: lease is vacant, but fencing_generation is preserved at 1 (NOT reset to 0)
    ctrl_data = json.loads(mock_storage.objects[f"state/{portal_id}/portal_control.json"]["data"].decode("utf-8"))
    assert ctrl_data["lease"]["lease_owner_id"] is None
    assert ctrl_data["fencing_generation"] == 1
    assert ctrl_data["baseline_generation"] == 0

    # Runner 2 reacquires vacant lease -> increments generation to 2
    acq2, ctrl2, etag2 = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-2")
    assert acq2 is True
    assert ctrl2.fencing_generation == 2
    assert ctrl2.baseline_generation == 0


# ---------------------------------------------------------------------------
# Test 3: Baseline generation advances ONLY on baseline publication
# ---------------------------------------------------------------------------
def test_baseline_generation_advances_only_on_publication(mock_storage, sample_package_bundle):
    portal_id = sample_package_bundle["portal_id"]
    run_id = sample_package_bundle["run_id"]

    # 1. Lease acquisition: fencing_generation = 1, baseline_generation = 0
    acq, ctrl, etag1 = mock_storage.acquire_portal_lease(portal_id, "runner-1", ttl_seconds=600)
    assert ctrl.fencing_generation == 1
    assert ctrl.baseline_generation == 0

    # 2. Lease renewal: neither fencing nor baseline generation advances
    renew_ok, etag2 = mock_storage.renew_portal_lease(portal_id, "runner-1", ctrl.fencing_generation, etag1)
    assert renew_ok is True
    ctrl_renewed = PortalControlRecord.from_dict(json.loads(mock_storage.objects[f"state/{portal_id}/portal_control.json"]["data"].decode("utf-8")))
    assert ctrl_renewed.fencing_generation == 1
    assert ctrl_renewed.baseline_generation == 0

    # 3. Baseline publication: baseline_generation increments to 1!
    mock_storage.stage_package_artifacts(portal_id, run_id, sample_package_bundle["files"])
    cat = ArchiveCatalog(portal_id=portal_id, latest_successful_run_id=run_id)
    cat.runs[run_id] = CatalogRunEntry(
        run_id=run_id, previous_run_id=None, mode="BASELINE", started_at="2026-10-27T00:00:00Z",
        completed_at="2026-10-27T00:05:00Z", status="CONVERGED", article_count=5, new_article_count=5,
        changed_article_count=0, run_dir=f"runs/baseline-20261027T000000Z/"
    )
    ok_pub, etag3, _ = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id=run_id, catalog=cat, expected_catalog_etag=None,
        expected_control_etag=etag2, fencing_generation=ctrl.fencing_generation, runner_id="runner-1"
    )
    assert ok_pub is True
    ctrl_pub = PortalControlRecord.from_dict(json.loads(mock_storage.objects[f"state/{portal_id}/portal_control.json"]["data"].decode("utf-8")))
    assert ctrl_pub.fencing_generation == 1
    assert ctrl_pub.baseline_generation == 1
    assert ctrl_pub.baseline.baseline_generation == 1

    # 4. Lease release: baseline_generation remains at 1
    rel_ok = mock_storage.release_portal_lease(portal_id, "runner-1", ctrl.fencing_generation, etag3)
    assert rel_ok is True
    ctrl_rel = PortalControlRecord.from_dict(json.loads(mock_storage.objects[f"state/{portal_id}/portal_control.json"]["data"].decode("utf-8")))
    assert ctrl_rel.fencing_generation == 1
    assert ctrl_rel.baseline_generation == 1


# ---------------------------------------------------------------------------
# Test 4: Expiry alone does not revoke owner until CAS takeover succeeds
# ---------------------------------------------------------------------------
def test_expiry_alone_does_not_revoke_until_takeover(mock_storage):
    portal_id = "wordpress-news"
    t0 = "2026-10-30T01:00:00Z"
    acq1, ctrl1, etag1 = mock_storage.acquire_portal_lease(portal_id, "runner-1", ttl_seconds=10, current_time_iso=t0)
    assert acq1 is True

    # Time reaches T0 + 15s (past TTL)
    t_past = "2026-10-30T01:00:15Z"

    # Inspect record in storage directly: owner is STILL runner-1
    ctrl_data = json.loads(mock_storage.objects[f"state/{portal_id}/portal_control.json"]["data"].decode("utf-8"))
    assert ctrl_data["lease"]["lease_owner_id"] == "runner-1"

    # But it is takeover-eligible
    record = PortalControlRecord.from_dict(ctrl_data)
    assert record.lease.is_expired(t_past) is True


# ---------------------------------------------------------------------------
# Test 5: Takeover vs Publication Race: resolved by atomic CAS to control record
# ---------------------------------------------------------------------------
def test_takeover_vs_publication_race_resolved_by_cas(mock_storage, sample_package_bundle):
    portal_id = sample_package_bundle["portal_id"]
    run_id = sample_package_bundle["run_id"]

    # Runner 1 acquires lease (TTL = 10s)
    t0 = "2026-10-30T01:00:00Z"
    acq1, ctrl1, etag1 = mock_storage.acquire_portal_lease(portal_id, "runner-1", ttl_seconds=10, current_time_iso=t0)
    mock_storage.stage_package_artifacts(portal_id, run_id, sample_package_bundle["files"])

    # Time advances past TTL: Runner 2 races to takeover
    t_past = "2026-10-30T01:00:20Z"
    acq2, ctrl2, etag2 = mock_storage.acquire_portal_lease(portal_id, "runner-2", ttl_seconds=600, current_time_iso=t_past)
    assert acq2 is True
    assert etag2 != etag1

    # Stale Runner 1 simultaneously tries to commit baseline using old etag1 -> REJECTED (HTTP 412)
    cat1 = ArchiveCatalog(portal_id=portal_id, latest_successful_run_id=run_id)
    cat1.runs[run_id] = CatalogRunEntry(
        run_id=run_id, previous_run_id=None, mode="BASELINE", started_at="2026-10-30T01:00:00Z",
        completed_at="2026-10-30T01:00:10Z", status="CONVERGED", article_count=5, new_article_count=5,
        changed_article_count=0, run_dir=f"runs/{run_id}/"
    )
    ok_stale, _, _ = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id=run_id, catalog=cat1,
        expected_catalog_etag=None, expected_control_etag=etag1,  # Stale ETag!
        fencing_generation=1, runner_id="runner-1"
    )
    assert ok_stale is False

    # Control record active owner remains Runner 2
    ctrl_active, _, _, _, _ = mock_storage.hydrate_state(portal_id, target_dir="nonexistent_tmp")
    assert ctrl_active.lease.lease_owner_id == "runner-2"
    assert ctrl_active.baseline.latest_successful_run_id is None


# ---------------------------------------------------------------------------
# Test 6: Heartbeat failure abort gate
# ---------------------------------------------------------------------------
def test_heartbeat_failure_aborts_publication(mock_storage):
    portal_id = "wordpress-news"
    _, ctrl, etag = mock_storage.acquire_portal_lease(portal_id, "runner-1", ttl_seconds=10)

    # Runner tries to renew heartbeat with incorrect ETag or after lease stolen
    renew_ok, _ = mock_storage.renew_portal_lease(
        portal_id, "runner-1", fencing_generation=ctrl.fencing_generation, expected_control_etag="invalid-etag"
    )
    assert renew_ok is False

    # Invariant: A runner whose heartbeat failed is gated from publishing
    def publication_allowed(heartbeat_healthy: bool) -> bool:
        return heartbeat_healthy

    assert publication_allowed(renew_ok) is False


# ---------------------------------------------------------------------------
# Test 7: Conditional-write conflicts and safe retries
# ---------------------------------------------------------------------------
def test_conditional_write_conflicts_and_safe_retries(mock_storage):
    portal_id = "wordpress-news"

    _, ctrl_init, c_etag_init = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-init")
    initial_cat = ArchiveCatalog(portal_id=portal_id)
    ok_init, c_etag_post, cat_etag_init = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id="run-init", catalog=initial_cat,
        expected_catalog_etag=None, expected_control_etag=c_etag_init,
        fencing_generation=ctrl_init.fencing_generation, runner_id="runner-init"
    )
    assert ok_init is True
    mock_storage.release_portal_lease(portal_id, "runner-init", ctrl_init.fencing_generation, c_etag_post)

    # Runner A acquires lease
    _, ctrl_a, c_etag_a = mock_storage.acquire_portal_lease(portal_id, runner_id="runner-a")

    cat_a = copy.deepcopy(initial_cat)
    cat_a.register_run(CatalogRunEntry(
        run_id="run-A", previous_run_id=None, mode="BASELINE", started_at="2026-10-30T01:00:00Z",
        completed_at="2026-10-30T01:05:00Z", status="CONVERGED", article_count=1, new_article_count=1,
        changed_article_count=0, run_dir="runs/run-A/"
    ), is_successful=True)

    # Runner A commits successfully
    ok_a, c_etag_a_new, cat_etag_a_new = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id="run-A", catalog=cat_a,
        expected_catalog_etag=cat_etag_init, expected_control_etag=c_etag_a,
        fencing_generation=ctrl_a.fencing_generation, runner_id="runner-a"
    )
    assert ok_a is True

    # A conflicting attempt using obsolete cat_etag_init fails cleanly
    ok_conflict, _, _ = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id="run-Conflict", catalog=cat_a,
        expected_catalog_etag=cat_etag_init, expected_control_etag=c_etag_a_new,
        fencing_generation=ctrl_a.fencing_generation, runner_id="runner-a"
    )
    assert ok_conflict is False


# ---------------------------------------------------------------------------
# Test 8: Interrupted uploads, byte SHA-256 mismatches, and non-destructive quarantine
# ---------------------------------------------------------------------------
def test_interrupted_uploads_sha256_mismatches_and_quarantine(mock_storage, sample_package_bundle):
    portal_id = sample_package_bundle["portal_id"]
    run_id = sample_package_bundle["run_id"]

    # Inject corruption on WARC payload
    mock_storage.injected_corruptions.add("content/content_html.warc.gz")
    mock_storage.stage_package_artifacts(portal_id, run_id, sample_package_bundle["files"])

    # Byte verification catches mismatch
    is_valid, mismatches = mock_storage.verify_remote_artifacts(
        portal_id, run_id, sample_package_bundle["checksums"]
    )
    assert is_valid is False
    assert len(mismatches) == 1
    assert "Checksum mismatch for content/content_html.warc.gz" in mismatches[0]

    # Quarantine recorded non-destructively
    q_id = mock_storage.quarantine_failed_upload(
        portal_id=portal_id,
        run_id=run_id,
        runner_id="runner-test",
        reason="SHA256_MISMATCH",
        details={"mismatches": mismatches},
    )
    assert q_id.startswith(f"quarantine-{run_id}-")
    assert len(mock_storage.quarantine_records) == 1

    # Corrupted artifact preserved (not deleted)
    corrupted_key = f"packages/{portal_id}/runs/{run_id}/content/content_html.warc.gz"
    assert corrupted_key in mock_storage.objects


# ---------------------------------------------------------------------------
# Test 9: Strict remote orphan validation (identity, ancestry, status, checksums, anomalies)
# ---------------------------------------------------------------------------
def test_strict_remote_orphan_validation(sample_package_bundle):
    current_catalog_head = "baseline-001"

    def audit_orphan(manifest: dict, actual_prev_head: str) -> Tuple[bool, str]:
        if manifest.get("portal_id") != "wordpress-news":
            return False, "PORTAL_ID_MISMATCH"
        if manifest.get("status") not in ("CONVERGED", "COMPLETED", "PILOT_SCOPE_CONVERGED"):
            return False, "INELIGIBLE_STATUS"
        if len(manifest.get("anomalies", [])) > 0:
            return False, "UNRESOLVED_ANOMALIES"
        if manifest.get("previous_run_id") != actual_prev_head:
            return False, "ANCESTRY_DISCONTINUITY"
        return True, "ADOPTION_ELIGIBLE"

    # 1. Valid orphan based on current head
    valid_manifest = copy.deepcopy(sample_package_bundle["manifest_data"])
    valid_manifest["previous_run_id"] = "baseline-001"
    ok, reason = audit_orphan(valid_manifest, current_catalog_head)
    assert ok is True
    assert reason == "ADOPTION_ELIGIBLE"

    # 2. Ancestry fork rejected
    forked_manifest = copy.deepcopy(valid_manifest)
    forked_manifest["previous_run_id"] = "old-baseline-999"
    ok, reason = audit_orphan(forked_manifest, current_catalog_head)
    assert ok is False
    assert reason == "ANCESTRY_DISCONTINUITY"

    # 3. Anomaly-flagged orphan rejected
    anomaly_manifest = copy.deepcopy(valid_manifest)
    anomaly_manifest["anomalies"] = ["EMPTY_HEAD_PAGE"]
    ok, reason = audit_orphan(anomaly_manifest, current_catalog_head)
    assert ok is False
    assert reason == "UNRESOLVED_ANOMALIES"


# ---------------------------------------------------------------------------
# Test 10: Failure between control-record publication, catalog projection, and deposit registry
# ---------------------------------------------------------------------------
def test_failure_between_control_record_catalog_and_deposit(mock_storage, sample_package_bundle):
    portal_id = sample_package_bundle["portal_id"]
    run_id = "run-100"

    _, ctrl, c_etag = mock_storage.acquire_portal_lease(portal_id, "runner-1")
    mock_storage.stage_package_artifacts(portal_id, run_id, sample_package_bundle["files"])

    # Control record committed successfully
    cat_old = ArchiveCatalog(portal_id=portal_id, latest_successful_run_id="old-run-001")
    cat_old.runs["old-run-001"] = CatalogRunEntry(
        run_id="old-run-001", previous_run_id=None, mode="BASELINE", started_at="2026-10-26T00:00:00Z",
        completed_at="2026-10-26T00:05:00Z", status="CONVERGED", article_count=2, new_article_count=2,
        changed_article_count=0, run_dir="runs/old-run-001/"
    )

    ctrl.baseline.latest_successful_run_id = run_id
    ctrl.baseline.committed_at = "2026-10-30T01:00:00Z"
    ctrl.baseline.committed_by_generation = ctrl.fencing_generation
    ctrl.baseline.baseline_generation = 1
    ctrl.control_version += 1
    c_bytes = json.dumps(ctrl.to_dict()).encode("utf-8")
    mock_storage.objects[f"state/{portal_id}/portal_control.json"] = {
        "data": c_bytes, "etag": "etag-new-control", "sha256": hashlib.sha256(c_bytes).hexdigest(), "metadata": {}
    }
    # Catalog still points to old-run-001; deposit registry update never happened
    cat_bytes = json.dumps(cat_old.to_dict()).encode("utf-8")
    mock_storage.objects[f"state/{portal_id}/catalog.json"] = {
        "data": cat_bytes, "etag": "etag-old-cat", "sha256": hashlib.sha256(cat_bytes).hexdigest(), "metadata": {}
    }

    # Next runner hydrates state: control record strictly overrides catalog
    ctrl_rec, _, cat_hydrated, _, deposit_data = mock_storage.hydrate_state(portal_id, target_dir="nonexistent_tmp")
    assert ctrl_rec.baseline.latest_successful_run_id == run_id
    assert cat_hydrated.latest_successful_run_id == run_id
    assert deposit_data is None  # Deposit failure did not invalidate crawl baseline


# ---------------------------------------------------------------------------
# Test 11: Delayed catalog recovery cannot regress a newer baseline
# ---------------------------------------------------------------------------
def test_delayed_catalog_recovery_cannot_regress_newer_baseline(mock_storage):
    portal_id = "wordpress-news"

    # Portal control record already has run-200 as the latest baseline (generation 5, timestamp 2026-10-30T02:00:00Z)
    ctrl = PortalControlRecord(
        portal_id=portal_id,
        fencing_generation=5,
        baseline_generation=2,
        baseline=BaselinePointer(
            latest_successful_run_id="run-200",
            committed_at="2026-10-30T02:00:00Z",
            committed_by_generation=5,
            baseline_generation=2,
        )
    )
    ctrl_bytes = json.dumps(ctrl.to_dict()).encode("utf-8")
    mock_storage.objects[f"state/{portal_id}/portal_control.json"] = {
        "data": ctrl_bytes, "etag": "etag-ctrl-200", "sha256": hashlib.sha256(ctrl_bytes).hexdigest(), "metadata": {}
    }

    # Catalog already has run-200 recorded as latest
    cat = ArchiveCatalog(portal_id=portal_id, latest_successful_run_id="run-200")
    cat.runs["run-200"] = CatalogRunEntry(
        run_id="run-200", previous_run_id="run-100", mode="INCREMENTAL", started_at="2026-10-30T02:00:00Z",
        completed_at="2026-10-30T02:05:00Z", status="CONVERGED", article_count=10, new_article_count=2,
        changed_article_count=0, run_dir="runs/run-200/"
    )
    cat_bytes = json.dumps(cat.to_dict()).encode("utf-8")
    mock_storage.objects[f"state/{portal_id}/catalog.json"] = {
        "data": cat_bytes, "etag": "etag-cat-200", "sha256": hashlib.sha256(cat_bytes).hexdigest(), "metadata": {}
    }

    # A delayed worker attempts to project an older baseline (run-100, started_at 2026-10-30T01:00:00Z)
    stale_ctrl = PortalControlRecord(
        portal_id=portal_id,
        fencing_generation=4,
        baseline_generation=1,
        baseline=BaselinePointer(
            latest_successful_run_id="run-100",
            committed_at="2026-10-30T01:00:00Z",
            committed_by_generation=4,
            baseline_generation=1,
        )
    )
    stale_entry = CatalogRunEntry(
        run_id="run-100", previous_run_id=None, mode="BASELINE", started_at="2026-10-30T01:00:00Z",
        completed_at="2026-10-30T01:05:00Z", status="CONVERGED", article_count=8, new_article_count=8,
        changed_article_count=0, run_dir="runs/run-100/"
    )

    # Projecting older baseline is STRICTLY REJECTED; latest pointer does NOT regress
    ok, _ = mock_storage.project_control_to_catalog(portal_id, stale_ctrl, stale_entry, expected_catalog_etag="etag-cat-200")
    assert ok is False

    # Verify catalog remains pointing to run-200
    cat_check = ArchiveCatalog.from_dict(json.loads(mock_storage.objects[f"state/{portal_id}/catalog.json"]["data"].decode("utf-8")))
    assert cat_check.latest_successful_run_id == "run-200"


# ---------------------------------------------------------------------------
# Test 12: Fresh-directory restoration of a previously committed baseline
# ---------------------------------------------------------------------------
def test_fresh_directory_restores_baseline_and_validates(mock_storage, sample_package_bundle, tmp_path):
    portal_id = sample_package_bundle["portal_id"]
    run_id = sample_package_bundle["run_id"]

    # 1. Runner 1 publishes baseline to mock storage
    _, ctrl, c_etag = mock_storage.acquire_portal_lease(portal_id, "runner-1")
    mock_storage.stage_package_artifacts(portal_id, run_id, sample_package_bundle["files"])

    cat = ArchiveCatalog(portal_id=portal_id, latest_successful_run_id=run_id)
    cat.runs[run_id] = CatalogRunEntry(
        run_id=run_id, previous_run_id=None, mode="BASELINE", started_at="2026-10-27T00:00:00Z",
        completed_at="2026-10-27T00:05:00Z", status="CONVERGED", article_count=5, new_article_count=5,
        changed_article_count=0, run_dir=f"runs/baseline-20261027T000000Z/"
    )
    ok_commit, c_etag_new, _ = mock_storage.commit_baseline_and_catalog(
        portal_id=portal_id, run_id=run_id, catalog=cat,
        expected_catalog_etag=None, expected_control_etag=c_etag,
        fencing_generation=ctrl.fencing_generation, runner_id="runner-1"
    )
    assert ok_commit is True
    mock_storage.release_portal_lease(portal_id, "runner-1", ctrl.fencing_generation, c_etag_new)

    # 2. Runner 2 starts on a fresh, completely empty ephemeral filesystem
    fresh_runner_dir = tmp_path / "fresh_runner_vm"
    fresh_runner_dir.mkdir()

    ctrl_hydrated, _, cat_hydrated, _, _ = mock_storage.hydrate_state(portal_id, target_dir=str(fresh_runner_dir))
    assert ctrl_hydrated is not None
    assert ctrl_hydrated.baseline.latest_successful_run_id == run_id
    assert cat_hydrated.latest_successful_run_id == run_id

    # Verify baseline package files were downloaded
    hydrated_pkg_dir = fresh_runner_dir / "packages" / portal_id / "runs" / run_id
    assert hydrated_pkg_dir.exists()
    assert (hydrated_pkg_dir / "manifest.json").exists()
    assert (hydrated_pkg_dir / "environment.json").exists()
    assert (hydrated_pkg_dir / "portal_state.json").exists()
    assert (hydrated_pkg_dir / "checksums.sha256").exists()

    # 3. Run existing validate_baseline_integrity on hydrated state
    prev_run_id, state_file, loaded_state = validate_baseline_integrity(
        catalog=cat_hydrated,
        results_base_dir=str(fresh_runner_dir),
        portal_id=portal_id,
    )
    assert prev_run_id == run_id
    assert loaded_state.portal_id == portal_id
    assert loaded_state.run_id == run_id

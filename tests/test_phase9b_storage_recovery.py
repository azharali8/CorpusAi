"""
Unit and integration tests for CorpusAI Phase 9B:
- PortalLock OS advisory locking and concurrency protection.
- StateStorageBackend and LocalStateStorageBackend resolution.
- Baseline integrity verification before network crawling.
- CatalogReconciler dry-run audits and non-destructive repairs.
- StorageRetentionManager conservative retention and staging cleanup.
"""

import json
import os
import subprocess
import sys
import time
from unittest import mock
import pytest

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.archive_package import ArchivePackager, PackageVerifier
from src.catalog_recovery import (
    BaselineIntegrityError,
    CatalogReconciler,
    ReconciliationReport,
    RetentionPlan,
    StorageRetentionManager,
    validate_baseline_integrity,
)
from src.portal_state import ArticleState, PortalState
from src.storage_backend import (
    LocalStateStorageBackend,
    LockAcquisitionError,
    PortalLock,
    StateStorageBackend,
)
import src.corpusai_run as corpusai_run


# ---------------------------------------------------------------------------
# 1. PortalLock Concurrency & OS Advisory Lock Tests
# ---------------------------------------------------------------------------

def test_portal_lock_acquire_and_release(tmp_path):
    """Test standard single-process lock acquire and release lifecycle."""
    lock_file = str(tmp_path / "test.lock")
    lock = PortalLock(lock_file, portal_id="portal-1")
    assert not lock.is_acquired

    assert lock.acquire() is True
    assert lock.is_acquired is True

    # Re-entrant in same instance returns True
    assert lock.acquire() is True

    lock.release()
    assert lock.is_acquired is False


def test_portal_lock_context_manager(tmp_path):
    """Test PortalLock as a context manager."""
    lock_file = str(tmp_path / "test.lock")
    lock = PortalLock(lock_file, portal_id="portal-1")
    with lock:
        assert lock.is_acquired is True
    assert lock.is_acquired is False


def test_portal_lock_contention_same_process(tmp_path):
    """A second PortalLock instance on the same file cannot acquire while the first holds it."""
    lock_file = str(tmp_path / "test.lock")
    lock1 = PortalLock(lock_file, portal_id="portal-1")
    lock2 = PortalLock(lock_file, portal_id="portal-1")

    assert lock1.acquire() is True
    try:
        # Second instance fails non-blocking acquisition
        assert lock2.acquire(blocking=False) is False
        assert lock2.is_acquired is False

        with pytest.raises(LockAcquisitionError):
            with lock2:
                pass
    finally:
        lock1.release()

    # Once lock1 is released, lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()


def test_portal_lock_cross_process_exclusion_and_kernel_cleanup(tmp_path):
    """
    Subprocess acquires lock. Parent process cannot acquire.
    When subprocess terminates, OS kernel automatically releases advisory lock,
    allowing parent to immediately acquire without manual file deletion.
    """
    lock_file = str(tmp_path / "cross_proc.lock")

    child_code = f"""
import sys, time
from src.storage_backend import PortalLock
lock = PortalLock({repr(lock_file)}, portal_id="portal-1")
if not lock.acquire():
    sys.exit(1)
sys.stdout.write("LOCKED\\n")
sys.stdout.flush()
time.sleep(10)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for child to lock
        line = proc.stdout.readline()
        assert "LOCKED" in line

        # Parent attempts to acquire lock on same file -> MUST fail
        parent_lock = PortalLock(lock_file, portal_id="portal-1")
        assert parent_lock.acquire(blocking=False) is False

    finally:
        # Kill the child process (simulating unhandled crash or termination)
        proc.kill()
        proc.wait()

    # Now parent MUST be able to acquire lock immediately because the OS kernel
    # automatically reclaimed the lock descriptor upon process death.
    parent_lock = PortalLock(lock_file, portal_id="portal-1")
    assert parent_lock.acquire(blocking=False) is True
    parent_lock.release()


def test_portal_lock_different_portals_independent(tmp_path):
    """Different portals have independent locks and do not block each other."""
    backend = LocalStateStorageBackend(str(tmp_path))
    lock_a = backend.get_lock("portal-a")
    lock_b = backend.get_lock("portal-b")

    assert lock_a.acquire() is True
    assert lock_b.acquire() is True

    lock_a.release()
    lock_b.release()


# ---------------------------------------------------------------------------
# 2. LocalStateStorageBackend Contract Tests
# ---------------------------------------------------------------------------

def test_local_storage_backend_paths(tmp_path):
    """Verify backend path resolution conforms to repository conventions."""
    base_dir = str(tmp_path / "results")
    backend = LocalStateStorageBackend(base_dir)

    assert backend.resolve_portal_root("portal-x") == os.path.abspath(base_dir)
    assert backend.get_catalog_path("portal-x") == os.path.join(os.path.abspath(base_dir), "catalog.json")
    assert backend.get_deposit_registry_path("portal-x") == os.path.join(os.path.abspath(base_dir), "deposit_registry.json")
    assert backend.get_packages_root() == os.path.join(os.path.abspath(base_dir), "packages")

    lock = backend.get_lock("portal-x")
    assert lock.portal_id == "portal-x"
    assert lock.lock_file_path == os.path.join(os.path.abspath(base_dir), "portal-x.lock")


# ---------------------------------------------------------------------------
# 3. Baseline Integrity Verification Pre-Flight Gate Tests
# ---------------------------------------------------------------------------

def _create_mock_converged_package(tmp_path, portal_id, run_id):
    """Helper to assemble a valid, verified package with extra/portal_state.json."""
    results_base = str(tmp_path / "results")
    packages_root = os.path.join(results_base, "packages")
    packager = ArchivePackager(archives_root=packages_root)

    # Create dummy staging portal_state.json
    state = PortalState(
        portal_id=portal_id,
        run_id=run_id,
        articles={
            "https://example.com/p1": ArticleState(
                canonical_url="https://example.com/p1",
                first_seen_run_id=run_id,
                last_seen_run_id=run_id,
                status="CAPTURED",
                content_hash="hash1",
            )
        },
    )
    staging_dir = tmp_path / "staging" / run_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    state_file = str(staging_dir / "portal_state.json")
    state.save_to_json(state_file)

    pkg_dir, ver = packager.package_run(
        portal_id=portal_id,
        run_id=run_id,
        mode="BASELINE",
        status="CONVERGED",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        target_urls=["https://example.com/news"],
        extra_artifacts={"portal_state.json": state_file},
    )
    return results_base, pkg_dir, state


def test_validate_baseline_integrity_success(tmp_path):
    """Pre-flight check succeeds when catalog, package, checksums, and state are valid."""
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    results_base, pkg_dir, orig_state = _create_mock_converged_package(tmp_path, portal_id, run_id)

    catalog = ArchiveCatalog(portal_id=portal_id)
    entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        status="CONVERGED",
        article_count=1,
        new_article_count=1,
        changed_article_count=0,
        run_dir=f"packages/{portal_id}/runs/{run_id}/",
    )
    catalog.register_run(entry, is_successful=True)

    prev_run_id, state_file, loaded_state = validate_baseline_integrity(catalog, results_base, portal_id)
    assert prev_run_id == run_id
    assert loaded_state.portal_id == portal_id
    assert loaded_state.run_id == run_id
    assert len(loaded_state.articles) == 1


def test_validate_baseline_integrity_missing_package_fails(tmp_path):
    """Fails if referenced package directory is missing on disk."""
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    catalog = ArchiveCatalog(portal_id=portal_id)
    entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        status="CONVERGED",
        article_count=1,
        new_article_count=1,
        changed_article_count=0,
        run_dir=f"packages/{portal_id}/runs/{run_id}/",
    )
    catalog.register_run(entry, is_successful=True)

    with pytest.raises(BaselineIntegrityError, match="Finalized package directory not found"):
        validate_baseline_integrity(catalog, str(tmp_path), portal_id)


def test_validate_baseline_integrity_corrupted_checksums_fails(tmp_path):
    """Fails if a file in the package is modified/tampered with."""
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    results_base, pkg_dir, orig_state = _create_mock_converged_package(tmp_path, portal_id, run_id)

    catalog = ArchiveCatalog(portal_id=portal_id)
    entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        status="CONVERGED",
        article_count=1,
        new_article_count=1,
        changed_article_count=0,
        run_dir=f"packages/{portal_id}/runs/{run_id}/",
    )
    catalog.register_run(entry, is_successful=True)

    # Tamper with environment.json
    env_file = os.path.join(pkg_dir, "environment.json")
    with open(env_file, "a") as f:
        f.write(" ")

    with pytest.raises(BaselineIntegrityError, match="Package verification failed"):
        validate_baseline_integrity(catalog, results_base, portal_id)


def test_validate_baseline_integrity_state_portal_id_mismatch(tmp_path):
    """Fails if portal_state.json belongs to a different portal."""
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    results_base, pkg_dir, _ = _create_mock_converged_package(tmp_path, "wrong-portal", run_id)

    catalog = ArchiveCatalog(portal_id=portal_id)
    entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        status="CONVERGED",
        article_count=1,
        new_article_count=1,
        changed_article_count=0,
        run_dir=f"packages/{portal_id}/runs/{run_id}/",
    )
    catalog.register_run(entry, is_successful=True)

    # Move package under portal_id directory
    target_dir = os.path.join(results_base, "packages", portal_id, "runs", run_id)
    os.makedirs(os.path.dirname(target_dir), exist_ok=True)
    if os.path.exists(target_dir):
        import shutil
        shutil.rmtree(target_dir)
    os.rename(pkg_dir, target_dir)

    with pytest.raises(BaselineIntegrityError, match="PortalState portal_id mismatch"):
        validate_baseline_integrity(catalog, results_base, portal_id)


# ---------------------------------------------------------------------------
# 4. CatalogReconciler Audit and Repair Tests
# ---------------------------------------------------------------------------

def test_catalog_reconciler_detects_and_adopts_orphaned_package(tmp_path):
    """
    Scenario: A crawler created and verified a package, but crashed before catalog update.
    Reconciler detects the single valid orphaned package and adopts it cleanly into catalog.json.
    """
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    results_base, pkg_dir, orig_state = _create_mock_converged_package(tmp_path, portal_id, run_id)

    catalog_path = os.path.join(results_base, "catalog.json")
    empty_cat = ArchiveCatalog(portal_id=portal_id)
    empty_cat.save_to_json(catalog_path)

    reconciler = CatalogReconciler(results_base, portal_id)

    # 1. Dry run inspection
    report: ReconciliationReport = reconciler.inspect()
    assert report.repairable is True
    assert run_id in report.orphaned_verified_packages
    assert report.proposed_new_head == run_id

    # Catalog on disk is not changed yet
    cat_check = ArchiveCatalog.load_from_json(catalog_path)
    assert cat_check.latest_successful_run_id is None

    # 2. Execute reconciliation
    reconciler.reconcile(dry_run=False)

    # Catalog now includes adopted run as latest_successful_run_id
    updated_cat = ArchiveCatalog.load_from_json(catalog_path)
    assert updated_cat.latest_successful_run_id == run_id
    assert run_id in updated_cat.runs
    assert updated_cat.runs[run_id].status == "CONVERGED"


def test_catalog_reconciler_ambiguous_multiple_orphans_refuses_auto_repair(tmp_path):
    """When multiple orphaned packages exist, reconciler flags ambiguity and refuses auto-repair."""
    portal_id = "test-portal"
    run_1 = "run-20261026T120000Z-tst1"
    run_2 = "run-20261026T120500Z-tst2"
    results_base, _, _ = _create_mock_converged_package(tmp_path, portal_id, run_1)
    _create_mock_converged_package(tmp_path, portal_id, run_2)

    catalog_path = os.path.join(results_base, "catalog.json")
    empty_cat = ArchiveCatalog(portal_id=portal_id)
    empty_cat.save_to_json(catalog_path)

    reconciler = CatalogReconciler(results_base, portal_id)
    report = reconciler.reconcile(dry_run=False)

    assert report.repairable is False
    assert len(report.orphaned_verified_packages) == 2
    # Catalog remains unaltered
    cat_check = ArchiveCatalog.load_from_json(catalog_path)
    assert cat_check.latest_successful_run_id is None


def test_catalog_reconciler_rolls_back_corrupt_head_to_prior_valid(tmp_path):
    """When current catalog head package is corrupted, reconciler safely rolls back to prior valid run."""
    portal_id = "test-portal"
    run_1 = "run-20261026T120000Z-tst1"
    run_2 = "run-20261026T120500Z-tst2"
    results_base, _, _ = _create_mock_converged_package(tmp_path, portal_id, run_1)
    _, pkg_2, _ = _create_mock_converged_package(tmp_path, portal_id, run_2)

    catalog_path = os.path.join(results_base, "catalog.json")
    catalog = ArchiveCatalog(portal_id=portal_id)
    for rid in (run_1, run_2):
        entry = CatalogRunEntry(
            run_id=rid,
            previous_run_id=None,
            mode="BASELINE" if rid == run_1 else "INCREMENTAL",
            started_at="2026-10-26T12:00:00Z",
            completed_at="2026-10-26T12:05:00Z",
            status="CONVERGED",
            article_count=1,
            new_article_count=1,
            changed_article_count=0,
            run_dir=f"packages/{portal_id}/runs/{rid}/",
        )
        catalog.register_run(entry, is_successful=True)
    catalog.save_to_json(catalog_path)

    # Corrupt run_2 package by tampering
    with open(os.path.join(pkg_2, "manifest.json"), "a") as f:
        f.write("corrupted")

    reconciler = CatalogReconciler(results_base, portal_id)
    report = reconciler.reconcile(dry_run=False)

    assert report.repairable is True
    assert report.proposed_new_head == run_1
    assert run_2 in report.corrupted_packages

    updated_cat = ArchiveCatalog.load_from_json(catalog_path)
    assert updated_cat.latest_successful_run_id == run_1


# ---------------------------------------------------------------------------
# 5. StorageRetentionManager Tests
# ---------------------------------------------------------------------------

def test_retention_manager_protects_packages_and_active_baseline(tmp_path):
    """Retention manager never deletes finalized packages or the active baseline run folder."""
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-tst"
    results_base, pkg_dir, _ = _create_mock_converged_package(tmp_path, portal_id, run_id)

    # Create dummy staging runs
    runs_dir = os.path.join(results_base, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    active_baseline_folder = os.path.join(runs_dir, f"baseline-{run_id}")
    os.makedirs(active_baseline_folder, exist_ok=True)

    catalog_path = os.path.join(results_base, "catalog.json")
    catalog = ArchiveCatalog(portal_id=portal_id)
    entry = CatalogRunEntry(
        run_id=run_id,
        previous_run_id=None,
        mode="BASELINE",
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        status="CONVERGED",
        article_count=1,
        new_article_count=1,
        changed_article_count=0,
        run_dir=f"runs/baseline-{run_id}/",
    )
    catalog.register_run(entry, is_successful=True)
    catalog.save_to_json(catalog_path)

    # Create 8 old staging folders
    for i in range(8):
        f = os.path.join(runs_dir, f"old-run-{i:02d}")
        os.makedirs(f, exist_ok=True)
        with open(os.path.join(f, "run.log"), "w") as lf:
            lf.write("log data")

    manager = StorageRetentionManager(results_base, portal_id)
    plan: RetentionPlan = manager.plan_cleanup(min_keep_staging=5)

    # Finalized package MUST be protected
    assert run_id in plan.retained_packages

    # Active baseline staging directory MUST be protected
    assert f"baseline-{run_id}" in plan.retained_runs

    # Out of 8 old runs, 5 are kept by min_keep_staging=5, and 3 are eligible for removal
    assert len(plan.eligible_for_removal) == 3

    # Execute cleanup
    manager.execute_cleanup(min_keep_staging=5, dry_run=False)

    # Verify finalized package still exists
    assert os.path.exists(pkg_dir)
    # Verify active baseline staging still exists
    assert os.path.exists(active_baseline_folder)
    # Verify exactly 3 folders were cleaned up
    remaining = os.listdir(runs_dir)
    assert len(remaining) == 6  # 5 kept + 1 baseline


# ---------------------------------------------------------------------------
# 6. CLI Integration & Concurrency Exit Code 8 Test
# ---------------------------------------------------------------------------

def test_cli_exit_code_8_on_portal_lock_contention(tmp_path):
    """When a portal lock is already held, corpusai_run exits immediately with code 8."""
    results_dir = str(tmp_path / "results")
    backend = LocalStateStorageBackend(results_dir)
    lock = backend.get_lock("wordpress-news")
    assert lock.acquire() is True

    try:
        with pytest.raises(SystemExit) as exc:
            corpusai_run.main(["--portal", "wordpress-news", "--mode", "baseline", "--results-dir", results_dir])
        assert exc.value.code == 8
    finally:
        lock.release()



def test_catalog_reconciler_refuses_orphaned_package_with_failed_crawl_status(tmp_path):
    """
    Integrity is NOT proof of crawl success:
    If a package directory has valid checksums but manifest status is FAILED or has anomalies,
    CatalogReconciler must NOT adopt it as a catalog baseline.
    """
    portal_id = "test-portal"
    run_id = "run-20261026T120000Z-failed"
    results_base = str(tmp_path / "results")
    packages_root = os.path.join(results_base, "packages")
    packager = ArchivePackager(archives_root=packages_root)

    state = PortalState(
        portal_id=portal_id,
        run_id=run_id,
        articles={},
    )
    staging_dir = tmp_path / "staging" / run_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    state_file = str(staging_dir / "portal_state.json")
    state.save_to_json(state_file)

    pkg_dir, ver = packager.package_run(
        portal_id=portal_id,
        run_id=run_id,
        mode="BASELINE",
        status="FAILED",  # Non-successful status!
        started_at="2026-10-26T12:00:00Z",
        completed_at="2026-10-26T12:05:00Z",
        target_urls=["https://example.com/news"],
        extra_artifacts={"portal_state.json": state_file},
        anomalies=["ACCESS_GATE_DETECTED"],
    )

    catalog_path = os.path.join(results_base, "catalog.json")
    empty_cat = ArchiveCatalog(portal_id=portal_id)
    empty_cat.save_to_json(catalog_path)

    reconciler = CatalogReconciler(results_base, portal_id)
    report = reconciler.reconcile(dry_run=False)

    assert report.repairable is False
    assert run_id in report.ambiguous_candidates
    assert run_id not in report.orphaned_verified_packages
    # Catalog head remains unassigned
    cat = ArchiveCatalog.load_from_json(catalog_path)
    assert cat.latest_successful_run_id is None

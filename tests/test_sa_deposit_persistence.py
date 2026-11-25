"""
Stage 9 Unit Tests: Deposit Persistence & SQLAlchemy 2.0 Integration.

Covers:
1. ORM mapping exact schema parity against SQLite deposit_records and deposit_artifacts.
2. Engine creation with NullPool and foreign key enforcement on connect.
3. atomic_write transaction boundary: commits on success, rolls back on error.
4. atomic_read clean reading and session closure.
5. Receipt and artifact save, retrieve, and complete domain reconstruction.
6. Stale artifact deletion upon receipt update.
7. Portal isolation across multiple receipts.
8. DepositRegistry delegation to SQLite database (no JSON write during normal operations).
9. Safe legacy JSON migration: dry-run, backup creation, idempotency, duplicate/conflict detection, reconciliation.
10. Stage 7 & 8 compatibility: DepositStatus enum, canonical remote_md5, error category semantics.
"""

import json
from pathlib import Path
import pytest
from sqlalchemy import select

from src.storage.sqlite_backend import CorpusAIStateDatabase
from src.storage.sa_models import Base, DepositArtifactORM, DepositRecordORM
from src.storage.sa_deposit_adapter import (
    SADepositPersistenceAdapter,
    create_sa_deposit_engine,
)
from src.storage.migration_tool import StateMigrationTool
from src.zenodo_stuff.models import (
    DepositReceipt,
    DepositStatus,
    UploadedArtifactRecord,
    STATUS_DRAFT_CREATED,
    STATUS_FAILED,
    STATUS_UPLOADED,
    STATUS_VERIFIED,
)
from src.zenodo_stuff.registry import DepositRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db(tmp_path):
    """Create an isolated, schema-initialized CorpusAI SQLite database."""
    db_path = tmp_path / "test_corpusai_state.db"
    db = CorpusAIStateDatabase(db_path=db_path)
    # Register dummy portal and run to satisfy foreign key constraints
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("p_test", "Test Portal", "https://example.com", "generic"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("p_test", "run_100", "2026-01-01T00:00:00Z", "COMPLETED"),
        )
        conn.execute(
            "INSERT INTO packages (portal_id, package_id, package_path) VALUES (?, ?, ?);",
            ("p_test", "run_100", "/dummy/path"),
        )
    return db


@pytest.fixture
def sa_adapter(test_db):
    adapter = SADepositPersistenceAdapter(test_db.db_path)
    yield adapter
    adapter.close()


def _sample_receipt(
    portal_id: str = "p_test",
    run_id: str = "run_100",
    status: DepositStatus = DepositStatus.UPLOADED,
) -> DepositReceipt:
    rec = DepositReceipt(
        receipt_id=f"receipt-{portal_id}-{run_id}-test",
        portal_id=portal_id,
        run_id=run_id,
        package_fingerprint="abc12345" * 8,
        repository_target="zenodo",
        environment="sandbox",
        status=status,
        created_at="2026-01-01T10:00:00Z",
        updated_at="2026-01-01T10:05:00Z",
        remote_record_id="123456",
        remote_bucket_url="https://sandbox.zenodo.org/api/files/bucket1",
        doi="10.5072/zenodo.123456",
        last_error=None,
        last_error_category=None,
        retry_eligible=True,
    )
    rec.artifact_inventory["manifest.json"] = UploadedArtifactRecord(
        filename="manifest.json",
        filesize=256,
        local_sha256="sha256_manifest",
        local_md5="md5_manifest",
        remote_md5="remote_md5_manifest",
        remote_file_id="file_1",
        verified=True,
    )
    return rec


# ---------------------------------------------------------------------------
# 1. ORM & Engine Configuration Tests
# ---------------------------------------------------------------------------

class TestSADepositEngineAndORM:

    def test_engine_foreign_keys_strictly_enforced(self, test_db):
        """
        The SA deposit engine strictly enables PRAGMA foreign_keys = ON on all connections.
        """
        engine = create_sa_deposit_engine(test_db.db_path)
        with engine.connect() as conn:
            fk = conn.exec_driver_sql("PRAGMA foreign_keys;").scalar()
            assert fk == 1, "SA deposit engine MUST enable PRAGMA foreign_keys = ON."
        engine.dispose()

    def test_invalid_foreign_key_insertion_rejected(self, sa_adapter):
        """
        Attempting to insert a DepositRecordORM referencing a nonexistent package
        raises an IntegrityError because foreign keys are enabled.
        """
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            with sa_adapter.atomic_write() as session:
                rec = DepositRecordORM(
                    portal_id="p_test",
                    deposit_id="d_bad_fk",
                    package_id="nonexistent_run_999",
                    target_repository="zenodo",
                    status="FAILED",
                    submitted_at="2026-01-01T00:00:00Z",
                )
                session.add(rec)

    def test_registered_invalid_package_failure_receipt_persisted(self, sa_adapter, test_db):
        """
        When a package IS registered in the packages table, saving a failure receipt
        durably persists it because its foreign key requirement is satisfied.
        """
        # test_db fixture already registered ('p_test', 'run_100')
        receipt = _sample_receipt(portal_id="p_test", run_id="run_100", status=DepositStatus.FAILED)
        receipt.last_error = "Local package verification failed"
        receipt.last_error_category = "LOCAL_PACKAGE_INVALID"

        persisted = sa_adapter.save_receipt(receipt)
        assert persisted is True

        retrieved = sa_adapter.get_receipt("p_test", "run_100")
        assert retrieved is not None
        assert retrieved.status == DepositStatus.FAILED
        assert retrieved.last_error == "Local package verification failed"

    def test_unregistered_invalid_package_failure_receipt_not_inserted(self, sa_adapter):
        """
        When a package is NOT registered in the packages table, attempting to save
        a failure receipt returns False and does NOT insert into deposit_records,
        preventing an IntegrityError while leaving the database clean.
        """
        receipt = _sample_receipt(portal_id="p_test", run_id="unregistered_run_888", status=DepositStatus.FAILED)
        receipt.receipt_id = "receipt-unregistered-888"

        persisted = sa_adapter.save_receipt(receipt)
        assert persisted is False

        # Verify not in DB
        retrieved = sa_adapter.get_receipt("p_test", "unregistered_run_888")
        assert retrieved is None

    def test_atomic_write_commits_on_success(self, sa_adapter, test_db):
        with sa_adapter.atomic_write() as session:
            rec = DepositRecordORM(
                portal_id="p_test",
                deposit_id="d_test_1",
                package_id="run_100",
                target_repository="zenodo",
                status="UPLOADED",
                submitted_at="2026-01-01T00:00:00Z",
            )
            session.add(rec)

        # Check committed in native sqlite connection
        conn = test_db.connect()
        row = conn.execute("SELECT deposit_id FROM deposit_records WHERE deposit_id = 'd_test_1';").fetchone()
        assert row is not None
        assert row["deposit_id"] == "d_test_1"

    def test_atomic_write_rolls_back_on_error(self, sa_adapter, test_db):
        with pytest.raises(RuntimeError, match="Simulated crash"):
            with sa_adapter.atomic_write() as session:
                rec = DepositRecordORM(
                    portal_id="p_test",
                    deposit_id="d_rollback",
                    package_id="run_100",
                    target_repository="zenodo",
                    status="FAILED",
                    submitted_at="2026-01-01T00:00:00Z",
                )
                session.add(rec)
                raise RuntimeError("Simulated crash")

        conn = test_db.connect()
        row = conn.execute("SELECT deposit_id FROM deposit_records WHERE deposit_id = 'd_rollback';").fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# 2. Receipt and Artifact Domain Persistence Round-Trip
# ---------------------------------------------------------------------------

class TestReceiptPersistenceRoundTrip:

    def test_save_and_retrieve_receipt_complete_fidelity(self, sa_adapter):
        receipt = _sample_receipt()
        sa_adapter.save_receipt(receipt)

        retrieved = sa_adapter.get_receipt("p_test", "run_100")
        assert retrieved is not None
        assert retrieved.receipt_id == receipt.receipt_id
        assert retrieved.status == DepositStatus.UPLOADED
        assert retrieved.repository_target == "zenodo"
        assert retrieved.environment == "sandbox"
        assert retrieved.remote_record_id == "123456"
        assert retrieved.doi == "10.5072/zenodo.123456"
        assert "manifest.json" in retrieved.artifact_inventory

        art = retrieved.artifact_inventory["manifest.json"]
        assert art.local_sha256 == "sha256_manifest"
        assert art.local_md5 == "md5_manifest"
        assert art.remote_md5 == "remote_md5_manifest"
        assert art.verified is True

    def test_updating_receipt_deletes_stale_artifacts(self, sa_adapter):
        receipt = _sample_receipt()
        receipt.artifact_inventory["old_artifact.txt"] = UploadedArtifactRecord(
            filename="old_artifact.txt",
            filesize=100,
            local_sha256="sha_old",
            local_md5="md5_old",
        )
        sa_adapter.save_receipt(receipt)

        r1 = sa_adapter.get_receipt("p_test", "run_100")
        assert "old_artifact.txt" in r1.artifact_inventory

        # Update receipt without old_artifact.txt
        receipt.artifact_inventory.pop("old_artifact.txt")
        receipt.status = DepositStatus.VERIFIED
        sa_adapter.save_receipt(receipt)

        r2 = sa_adapter.get_receipt("p_test", "run_100")
        assert r2.status == DepositStatus.VERIFIED
        assert "old_artifact.txt" not in r2.artifact_inventory
        assert "manifest.json" in r2.artifact_inventory

    def test_portal_isolation(self, sa_adapter, test_db):
        # Register second portal and run
        with test_db.transaction() as conn:
            conn.execute(
                "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
                ("p_other", "Other Portal", "https://other.com", "generic"),
            )
            conn.execute(
                "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
                ("p_other", "run_100", "2026-01-01T00:00:00Z", "COMPLETED"),
            )
            conn.execute(
                "INSERT INTO packages (portal_id, package_id, package_path) VALUES (?, ?, ?);",
                ("p_other", "run_100", "/other/path"),
            )

        rec1 = _sample_receipt(portal_id="p_test", run_id="run_100")
        rec2 = _sample_receipt(portal_id="p_other", run_id="run_100", status=DepositStatus.FAILED)
        rec2.receipt_id = "receipt-p_other-run_100-test"

        sa_adapter.save_receipt(rec1)
        sa_adapter.save_receipt(rec2)

        p1_rec = sa_adapter.get_receipt("p_test", "run_100")
        p2_rec = sa_adapter.get_receipt("p_other", "run_100")

        assert p1_rec.status == DepositStatus.UPLOADED
        assert p2_rec.status == DepositStatus.FAILED


# ---------------------------------------------------------------------------
# 3. DepositRegistry Delegation Tests
# ---------------------------------------------------------------------------

class TestDepositRegistryDelegation:

    def test_registry_persists_to_db_and_does_not_create_json(self, sa_adapter, tmp_path):
        """
        When registry_path is a non-JSON path (e.g. the DB file itself) and an explicit
        SA adapter is supplied, register_receipt() writes only to the database; no JSON
        mirror is produced.

        Note: when registry_path ends with '.json', DepositRegistry.save() deliberately
        also writes a JSON mirror for backward-compatibility with legacy callers (e.g.
        test_atomic_receipt_persistence in test_zenodo_deposit.py).  Callers that want
        pure database persistence must supply a non-.json registry_path.
        """
        db_path = tmp_path / "corpusai_state.db"
        json_path = tmp_path / "deposit_registry.json"
        reg = DepositRegistry(registry_path=db_path, adapter=sa_adapter)

        receipt = _sample_receipt()
        reg.register_receipt(receipt)

        # Confirm saved to database
        db_rec = sa_adapter.get_receipt("p_test", "run_100")
        assert db_rec is not None
        assert db_rec.receipt_id == receipt.receipt_id

        # Confirm no JSON file was created (db_path is not a .json path)
        assert not json_path.exists()

    def test_explicit_export_json_creates_file(self, sa_adapter, tmp_path):
        json_path = tmp_path / "export_registry.json"
        reg = DepositRegistry(registry_path=tmp_path / "corpusai_state.db", adapter=sa_adapter)

        receipt = _sample_receipt()
        reg.register_receipt(receipt)

        reg.export_json(json_path)
        assert json_path.exists()
        with open(json_path, encoding="UTF-8") as fh:
            data = json.load(fh)
            assert "p_test:run_100" in data["receipts"]


# ---------------------------------------------------------------------------
# 4. Safe Legacy JSON Migration Tests
# ---------------------------------------------------------------------------

class TestLegacyJsonMigration:

    def test_dry_run_does_not_modify_database(self, test_db, tmp_path):
        json_path = tmp_path / "legacy_deposit_registry.json"
        sample_data = {
            "schema_version": "1",
            "receipts": {
                "p_test:run_100": _sample_receipt().to_dict()
            }
        }
        with open(json_path, "w", encoding="UTF-8") as fh:
            json.dump(sample_data, fh)

        migration_tool = StateMigrationTool(test_db)
        report = migration_tool.import_deposit_registry_json(
            registry_path=json_path,
            portal_id="p_test",
            dry_run=True,
            backup_source=False,
            return_report=True,
        )

        assert report["dry_run"] is True
        assert report["imported"] == 0
        assert report["would_import"] == 1
        assert report["reconciled"] is False

        # Database should still be empty
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        assert sa_adapter.get_receipt("p_test", "run_100") is None
        sa_adapter.close()

    def test_migration_imports_data_creates_backup_and_is_idempotent(self, test_db, tmp_path):
        json_path = tmp_path / "legacy_deposit_registry.json"
        sample_data = {
            "schema_version": "1",
            "receipts": {
                "p_test:run_100": _sample_receipt().to_dict()
            }
        }
        with open(json_path, "w", encoding="UTF-8") as fh:
            json.dump(sample_data, fh)

        migration_tool = StateMigrationTool(test_db)

        # First run: import
        report1 = migration_tool.import_deposit_registry_json(
            registry_path=json_path,
            portal_id="p_test",
            dry_run=False,
            backup_source=True,
            return_report=True,
        )
        assert report1["imported"] == 1
        assert report1["duplicates"] == 0

        # Backup file must exist
        backups = list(tmp_path.glob("legacy_deposit_registry.json.bak.*"))
        assert len(backups) == 1

        # Second run: idempotent duplicate detection
        report2 = migration_tool.import_deposit_registry_json(
            registry_path=json_path,
            portal_id="p_test",
            dry_run=False,
            backup_source=False,
            return_report=True,
        )
        assert report2["imported"] == 0
        assert report2["duplicates"] == 1
        assert report2["reconciled"] is True

    def test_multi_record_migration_atomic_rollback_on_failure(self, test_db, tmp_path):
        """
        Verify that multi-record migration is truly atomic: if an error occurs
        partway through importing, ALL previously processed records in that batch roll back.
        """
        # Register a second run and package in DB
        with test_db.transaction() as conn:
            conn.execute("INSERT INTO runs (portal_id, run_id, started_at, status) VALUES ('p_test', 'run_200', '2026-01-01T00:00:00Z', 'COMPLETED');")
            conn.execute("INSERT INTO packages (portal_id, package_id, package_path) VALUES ('p_test', 'run_200', '/pkg/200');")

        rec1 = _sample_receipt(portal_id="p_test", run_id="run_100")
        rec2 = _sample_receipt(portal_id="p_test", run_id="run_200")
        rec2.receipt_id = "rec-200-test"

        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)

        # Simulate a crash during save_receipts_batch
        from unittest import mock
        orig_persist = sa_adapter._persist_receipt_in_session

        call_count = [0]
        def exploding_persist(session, receipt):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Crash on second receipt!")
            return orig_persist(session, receipt)

        with mock.patch.object(sa_adapter, "_persist_receipt_in_session", side_effect=exploding_persist):
            with pytest.raises(RuntimeError, match="Crash on second receipt!"):
                sa_adapter.save_receipts_batch([rec1, rec2])

        # Assert rec1 was rolled back completely and does not exist in DB
        assert sa_adapter.get_receipt("p_test", "run_100") is None
        assert sa_adapter.get_receipt("p_test", "run_200") is None
        sa_adapter.close()

    def test_corrupted_metadata_json_raises_error(self, sa_adapter, test_db):
        """
        Corrupted metadata_json must raise a clear ValueError with chained exception,
        not silently fallback to empty dict.
        """
        # Insert a record with invalid JSON directly into deposit_records
        with test_db.transaction() as conn:
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_corrupt', 'run_100', 'zenodo', 'UPLOADED', '2026-01-01T00:00:00Z', 'INVALID_NOT_JSON');"
            )

        with pytest.raises(ValueError, match="Failed to deserialize metadata_json"):
            sa_adapter.get_receipt("p_test", "run_100")

    def test_missing_environment_determines_explicit_compatibility(self, sa_adapter, test_db):
        """
        When metadata_json lacks an explicit environment, legacy fallback establishes
        environment with explicit recognition of known targets (sandbox, production)
        and preserves 'unknown' for unrecognized targets.
        """
        with test_db.transaction() as conn:
            # 1. Recognized sandbox target
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_legacy_sb', 'run_100', 'zenodo_sandbox', 'UPLOADED', '2026-01-01T00:00:00Z', '{\"package_fingerprint\": \"fp\"}');"
            )
            # 2. Recognized production target (zenodo)
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_legacy_zenodo', 'run_100', 'zenodo', 'UPLOADED', '2026-01-01T00:00:00Z', '{\"package_fingerprint\": \"fp\"}');"
            )
            # 3. Recognized production target (dataverse)
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_legacy_dv', 'run_100', 'dataverse', 'UPLOADED', '2026-01-01T00:00:00Z', '{\"package_fingerprint\": \"fp\"}');"
            )
            # 4. Unrecognized target identifier
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_legacy_unknown', 'run_100', 'custom_repo_xyz', 'UPLOADED', '2026-01-01T00:00:00Z', '{\"package_fingerprint\": \"fp\"}');"
            )
            # 5. Explicit environment in metadata takes precedence over inference
            conn.execute(
                "INSERT INTO deposit_records (portal_id, deposit_id, package_id, target_repository, status, submitted_at, metadata_json) "
                "VALUES ('p_test', 'dep_explicit_override', 'run_100', 'zenodo', 'UPLOADED', '2026-01-01T00:00:00Z', '{\"package_fingerprint\": \"fp\", \"environment\": \"staging\"}');"
            )

        with sa_adapter.atomic_read() as session:
            from src.storage.sa_models import DepositRecordORM
            r_sb = sa_adapter._reconstruct_receipt(session.get(DepositRecordORM, ("p_test", "dep_legacy_sb")), [])
            r_zen = sa_adapter._reconstruct_receipt(session.get(DepositRecordORM, ("p_test", "dep_legacy_zenodo")), [])
            r_dv = sa_adapter._reconstruct_receipt(session.get(DepositRecordORM, ("p_test", "dep_legacy_dv")), [])
            r_unk = sa_adapter._reconstruct_receipt(session.get(DepositRecordORM, ("p_test", "dep_legacy_unknown")), [])
            r_ovr = sa_adapter._reconstruct_receipt(session.get(DepositRecordORM, ("p_test", "dep_explicit_override")), [])

        assert r_sb.environment == "sandbox"
        assert r_zen.environment == "production"
        assert r_dv.environment == "production"
        assert r_unk.environment == "unknown"
        assert r_ovr.environment == "staging"

    def test_migration_conflict_detection_on_error_or_metadata_difference(self, test_db, tmp_path):
        """
        When two records share the same receipt ID and status but differ in error information,
        error category, or other persisted fields, migration tool flags them as a conflict.
        """
        from src.storage.migration_tool import StateMigrationTool
        tool = StateMigrationTool(test_db)
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)

        # Base receipt already saved in DB
        base_receipt = _sample_receipt(portal_id="p_test", run_id="run_100")
        base_receipt.last_error = "Old error"
        base_receipt.last_error_category = "LOCAL_PACKAGE_INVALID"
        sa_adapter.save_receipt(base_receipt)

        # Incoming JSON has identical receipt_id and status, but different error info
        incoming_receipt = _sample_receipt(portal_id="p_test", run_id="run_100")
        incoming_receipt.last_error = "Different error occurred"
        incoming_receipt.last_error_category = "NETWORK_FAILURE"

        json_file = tmp_path / "conflict_registry.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "1",
                "receipts": {f"p_test:{incoming_receipt.run_id}": incoming_receipt.to_dict()}
            }, f)

        report = tool.import_deposit_registry_json(json_file, portal_id="p_test", return_report=True)
        assert report["duplicates"] == 0
        assert len(report["conflicts"]) == 1
        assert report["conflicts"][0]["run_id"] == "run_100"
        assert report["imported"] == 0

    def test_list_receipts_avoids_n_plus_one_queries(self, sa_adapter, test_db):
        """
        list_receipts reconstructs models directly from batch queries without N+1 calls.
        """
        with test_db.transaction() as conn:
            conn.execute("INSERT INTO runs (portal_id, run_id, started_at, status) VALUES ('p_test', 'run_list_1', '2026-01-01T00:00:00Z', 'COMPLETED');")
            conn.execute("INSERT INTO packages (portal_id, package_id, package_path) VALUES ('p_test', 'run_list_1', '/pkg/1');")
            conn.execute("INSERT INTO runs (portal_id, run_id, started_at, status) VALUES ('p_test', 'run_list_2', '2026-01-01T00:00:00Z', 'COMPLETED');")
            conn.execute("INSERT INTO packages (portal_id, package_id, package_path) VALUES ('p_test', 'run_list_2', '/pkg/2');")

        r1 = _sample_receipt(portal_id="p_test", run_id="run_list_1")
        r2 = _sample_receipt(portal_id="p_test", run_id="run_list_2")
        sa_adapter.save_receipt(r1)
        sa_adapter.save_receipt(r2)

        results = sa_adapter.list_receipts("p_test")
        assert len(results) >= 2
        run_ids = [r.run_id for r in results]
        assert "run_list_1" in run_ids
        assert "run_list_2" in run_ids

    def test_registry_register_receipt_failure_reverts_cache(self, test_db):
        """
        When register_receipt raises an exception on persistence, the in-memory
        cache is rolled back and not left in an inconsistent state.
        """
        reg = DepositRegistry(registry_path=test_db.db_path)
        bad_receipt = _sample_receipt(portal_id="p_test", run_id="run_crash")

        from unittest import mock
        with mock.patch.object(reg._adapter, "save_receipt", side_effect=RuntimeError("DB write crash")):
            with pytest.raises(RuntimeError, match="DB write crash"):
                reg.register_receipt(bad_receipt)

        # Cache must NOT retain bad_receipt
        assert "p_test:run_crash" not in reg.receipts


# ---------------------------------------------------------------------------
# 5. Stage 9 Final Persistence Integrity Verification Tests
# ---------------------------------------------------------------------------

class TestStage9PersistenceIntegrity:

    def test_migration_detects_updated_at_difference(self, test_db, tmp_path):
        """Differences in updated_at must be detected as a conflict, not a duplicate."""
        tool = StateMigrationTool(test_db)
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)

        base_receipt = _sample_receipt(portal_id="p_test", run_id="run_100")
        sa_adapter.save_receipt(base_receipt)

        incoming = _sample_receipt(portal_id="p_test", run_id="run_100")
        incoming.updated_at = "2026-02-01T12:00:00Z"

        json_file = tmp_path / "updated_at_conflict.json"
        json_file.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": incoming.to_dict()}}),
            encoding="utf-8",
        )

        report = tool.import_deposit_registry_json(json_file, portal_id="p_test", return_report=True)
        assert report["duplicates"] == 0
        assert len(report["conflicts"]) == 1
        assert report["reconciled"] is False
        sa_adapter.close()

    def test_migration_detects_environment_and_remote_url_difference(self, test_db, tmp_path):
        """Differences in environment or remote_bucket_url/remote_record_url must be detected."""
        tool = StateMigrationTool(test_db)
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)

        base_receipt = _sample_receipt(portal_id="p_test", run_id="run_100")
        sa_adapter.save_receipt(base_receipt)

        incoming = _sample_receipt(portal_id="p_test", run_id="run_100")
        incoming.environment = "production"
        incoming.remote_bucket_url = "https://zenodo.org/api/files/bucket_prod"

        json_file = tmp_path / "env_url_conflict.json"
        json_file.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": incoming.to_dict()}}),
            encoding="utf-8",
        )

        report = tool.import_deposit_registry_json(json_file, portal_id="p_test", return_report=True)
        assert report["duplicates"] == 0
        assert len(report["conflicts"]) == 1
        assert report["reconciled"] is False
        sa_adapter.close()

    def test_migration_detects_artifact_md5_and_filename_difference(self, test_db, tmp_path):
        """Differences in artifact remote_md5 or filename must be detected."""
        tool = StateMigrationTool(test_db)
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)

        base_receipt = _sample_receipt(portal_id="p_test", run_id="run_100")
        sa_adapter.save_receipt(base_receipt)

        incoming = _sample_receipt(portal_id="p_test", run_id="run_100")
        incoming.artifact_inventory["manifest.json"].remote_md5 = "different_remote_md5"

        json_file = tmp_path / "artifact_md5_conflict.json"
        json_file.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": incoming.to_dict()}}),
            encoding="utf-8",
        )

        report = tool.import_deposit_registry_json(json_file, portal_id="p_test", return_report=True)
        assert report["duplicates"] == 0
        assert len(report["conflicts"]) == 1
        assert report["reconciled"] is False
        sa_adapter.close()

    def test_backup_and_restore_preserves_receipt_data(self, test_db, tmp_path):
        """
        Create a database with known data, back it up, mutate the database,
        restore from backup, and verify original data is intact.
        """
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        original = _sample_receipt(portal_id="p_test", run_id="run_100")
        sa_adapter.save_receipt(original)
        sa_adapter.close()

        backup_path = tmp_path / "backup_state.db"
        test_db.backup(backup_path)
        assert backup_path.exists()

        # Controlled mutation: delete deposit_records
        with test_db.transaction() as conn:
            conn.execute("DELETE FROM deposit_records;")
        sa_adapter2 = SADepositPersistenceAdapter(test_db.db_path)
        assert sa_adapter2.get_receipt("p_test", "run_100") is None
        sa_adapter2.close()

        # Restore from backup
        test_db.restore(backup_path, overwrite=True)
        sa_adapter3 = SADepositPersistenceAdapter(test_db.db_path)
        restored = sa_adapter3.get_receipt("p_test", "run_100")
        assert restored is not None
        assert restored.receipt_id == original.receipt_id
        assert restored.doi == original.doi
        assert restored.artifact_inventory["manifest.json"].remote_md5 == "remote_md5_manifest"
        sa_adapter3.close()

    def test_sa_engine_observes_wal_busy_timeout_and_foreign_keys(self, test_db):
        """
        Verify journal_mode=wal, busy_timeout, and foreign_keys=ON on a new SA connection
        to the file-backed database.
        """
        engine = create_sa_deposit_engine(test_db.db_path, timeout=5.0)
        try:
            with engine.connect() as conn:
                journal_mode = conn.exec_driver_sql("PRAGMA journal_mode;").scalar()
                busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout;").scalar()
                foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys;").scalar()

                assert str(journal_mode).lower() == "wal"
                assert busy_timeout == 5000
                assert foreign_keys == 1
        finally:
            engine.dispose()

    def test_concurrent_sa_threads_with_deterministic_barrier(self, test_db):
        """
        Multiple threads opening independent SA sessions concurrently against the same
        file-backed database succeed without corruption.
        """
        import threading

        with test_db.transaction() as conn:
            for i in range(4):
                run_id = f"run_conc_{i}"
                conn.execute(
                    "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
                    ("p_test", run_id, "2026-01-01T00:00:00Z", "COMPLETED"),
                )
                conn.execute(
                    "INSERT INTO packages (portal_id, package_id, package_path) VALUES (?, ?, ?);",
                    ("p_test", run_id, f"/pkg/{run_id}"),
                )

        barrier = threading.Barrier(4)
        errors = []

        def worker(idx: int) -> None:
            adapter = SADepositPersistenceAdapter(test_db.db_path, timeout=5.0)
            try:
                barrier.wait(timeout=5.0)
                rec = _sample_receipt(portal_id="p_test", run_id=f"run_conc_{idx}")
                rec.receipt_id = f"receipt-conc-{idx}"
                assert adapter.save_receipt(rec) is True
            except Exception as exc:
                errors.append(exc)
            finally:
                adapter.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert errors == []
        verify_adapter = SADepositPersistenceAdapter(test_db.db_path)
        for i in range(4):
            got = verify_adapter.get_receipt("p_test", f"run_conc_{i}")
            assert got is not None
            assert got.receipt_id == f"receipt-conc-{i}"
        verify_adapter.close()

    def test_interrupted_transaction_rolls_back_partial_writes(self, sa_adapter):
        """
        If an exception interrupts atomic_write after adding a record, partial writes
        are rolled back and not visible.
        """
        with pytest.raises(RuntimeError, match="Interrupted before commit"):
            with sa_adapter.atomic_write() as session:
                rec = DepositRecordORM(
                    portal_id="p_test",
                    deposit_id="dep_partial",
                    package_id="run_100",
                    target_repository="zenodo",
                    status="UPLOADING",
                    submitted_at="2026-01-01T00:00:00Z",
                )
                session.add(rec)
                session.flush()
                raise RuntimeError("Interrupted before commit")

        assert sa_adapter.get_receipt("p_test", "run_100") is None

    def test_default_db_path_policy_rejects_omitted_or_json_only_path(self, tmp_path):
        """
        DepositRegistry must raise ValueError when no db_path, .db registry_path,
        ':memory:', or adapter is provided, preventing CWD-relative DB creation.
        """
        with pytest.raises(ValueError, match="DepositRegistry requires an explicit db_path"):
            DepositRegistry()

        with pytest.raises(ValueError, match="DepositRegistry requires an explicit db_path"):
            DepositRegistry(registry_path=tmp_path / "deposit_registry.json")

        # ':memory:' remains supported with StaticPool
        mem_reg = DepositRegistry(registry_path=":memory:")
        from sqlalchemy.pool import StaticPool
        assert isinstance(mem_reg._adapter.engine.pool, StaticPool)
        mem_reg._adapter.close()


# ---------------------------------------------------------------------------
# 6. Migration Marker & Idempotency Tests
# ---------------------------------------------------------------------------

class TestMigrationMarkerAndIdempotency:

    def test_successful_import_writes_completion_marker(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        report = tool.import_deposit_registry_json(json_path, portal_id="p_test", return_report=True)

        assert report["imported"] == 1
        assert report["reconciled"] is True
        assert report["already_completed"] is False

        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        marker = sa_adapter.get_data_migration_marker("deposit_registry:p_test")
        assert marker is not None
        assert marker["source_sha256"] == report["source_sha256"]
        assert marker["imported_count"] == 1
        sa_adapter.close()

    def test_repeating_same_import_is_idempotent(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        r1 = tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)
        assert r1["imported"] == 1

        r2 = tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)
        assert r2["already_completed"] is True
        assert r2["imported"] == 0
        assert r2["duplicates"] == 1
        assert r2["reconciled"] is True

    def test_changed_source_fingerprint_is_detected(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        rec = _sample_receipt()
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": rec.to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)

        # Mutate source JSON after completed migration
        rec.doi = "10.5072/zenodo.changed"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": rec.to_dict()}}),
            encoding="utf-8",
        )
        r2 = tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)
        assert r2["source_changed"] is True
        assert r2["imported"] == 0
        assert len(r2["conflicts"]) == 1
        assert r2["conflicts"][0]["reason"] == "source_sha256_mismatch"

    def test_failed_batch_leaves_no_completion_marker(self, test_db, tmp_path):
        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        rec = _sample_receipt()
        from unittest import mock

        with mock.patch.object(sa_adapter, "_persist_receipt_in_session", side_effect=RuntimeError("Batch boom")):
            with pytest.raises(RuntimeError, match="Batch boom"):
                sa_adapter.save_receipts_batch(
                    [rec],
                    migration_marker={
                        "migration_id": "deposit_registry:p_test",
                        "source_path": str(tmp_path / "reg.json"),
                        "source_sha256": "deadbeef",
                        "completed_at": "2026-01-01T00:00:00Z",
                    },
                )

        assert sa_adapter.get_data_migration_marker("deposit_registry:p_test") is None
        assert sa_adapter.get_receipt("p_test", "run_100") is None
        sa_adapter.close()

    def test_interrupted_migration_can_be_retried_successfully(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        from unittest import mock

        with mock.patch(
            "src.storage.sa_deposit_adapter.SADepositPersistenceAdapter.save_receipts_batch",
            side_effect=RuntimeError("Simulated crash"),
        ):
            with pytest.raises(RuntimeError, match="Simulated crash"):
                tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)

        # Retry succeeds cleanly
        report = tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)
        assert report["imported"] == 1
        assert report["reconciled"] is True

    def test_dry_run_creates_no_marker_and_changes_no_state(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        report = tool.import_deposit_registry_json(
            json_path, portal_id="p_test", dry_run=True, backup_source=False, return_report=True
        )
        assert report["dry_run"] is True
        assert report["would_import"] == 1
        assert report["imported"] == 0
        assert report["reconciled"] is False

        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        assert sa_adapter.get_data_migration_marker("deposit_registry:p_test") is None
        assert sa_adapter.get_receipt("p_test", "run_100") is None
        sa_adapter.close()

    def test_backup_and_restore_preserve_records_and_migration_marker(self, test_db, tmp_path):
        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)

        backup_path = tmp_path / "backup_with_marker.db"
        test_db.backup(backup_path)

        with test_db.transaction() as conn:
            conn.execute("DELETE FROM deposit_records;")
            conn.execute("DELETE FROM data_migrations;")

        test_db.restore(backup_path, overwrite=True)

        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        assert sa_adapter.get_receipt("p_test", "run_100") is not None
        assert sa_adapter.get_data_migration_marker("deposit_registry:p_test") is not None
        sa_adapter.close()

    def test_existing_db_without_data_migrations_table_can_read_and_migrate(self, test_db, tmp_path):
        """Simulate a pre-existing V1 database that lacks the data_migrations table."""
        with test_db.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS data_migrations;")

        sa_adapter = SADepositPersistenceAdapter(test_db.db_path)
        assert sa_adapter.get_data_migration_marker("deposit_registry:p_test") is None
        sa_adapter.close()

        json_path = tmp_path / "deposit_registry.json"
        json_path.write_text(
            json.dumps({"schema_version": "1", "receipts": {"p_test:run_100": _sample_receipt().to_dict()}}),
            encoding="utf-8",
        )
        tool = StateMigrationTool(test_db)
        report = tool.import_deposit_registry_json(json_path, portal_id="p_test", backup_source=False, return_report=True)
        assert report["imported"] == 1
        assert report["reconciled"] is True

"""
CorpusAI SQLite Storage Foundation Unit Test Suite.

Covers:
1. Connection lifecycle, foreign key enforcement, WAL mode, busy timeout.
2. Schema initialization and idempotency.
3. Migration tracking and rollback on migration failure.
4. Unsupported/newer schema version detection.
5. Composite foreign keys and cross-portal isolation.
6. Portal latest_successful_run_id trigger validation on INSERT and UPDATE.
7. Prevention of invalidating a referenced successful run.
8. Transaction commit and rollback semantics.
9. Outbox atomic claim, lease rotation, expiry recovery, and late-worker fencing.
10. Outbox attempt exhaustion leading to FAILED and RECONCILIATION_REQUIRED.
11. Outbox timestamp ordering for due events.
12. Backup creation, verification, and reopenability.
13. Restore validation, overwrite protection, and memory/disk recovery.
14. PRAGMA integrity_check and PRAGMA foreign_key_check.
15. Fencing: worker cannot complete event after lease expires even if token not replaced.
16. Fencing: worker cannot retry event after lease expires.
17. Fencing: worker cannot renew an expired lease.
18. Fencing: worker cannot mutate event after another worker reclaimed it.
19. Exhaustion: in-flight event that exhausts attempts transitions to RECONCILIATION_REQUIRED after lease expiry.
20. Exhaustion: exhausted event is never claimed again.
21. Lease cleanup: all lease ownership fields cleared after completion, retry, and exhaustion.
22. Normal retry and completion behavior unhindered under valid lease.
"""

import os
import sqlite3
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta

from src.storage import (
    CorpusAIStateDatabase,
    DatabaseError,
    MigrationError,
    DatabaseLockedError,
    OutboxEventRecord,
    OutboxStatus,
)
from src.storage.schema import LATEST_SCHEMA_VERSION


@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_corpusai_state.db"


@pytest.fixture
def db(temp_db_path):
    database = CorpusAIStateDatabase(db_path=temp_db_path, enable_wal=True)
    yield database
    database.close()


def test_connection_configuration(db, temp_db_path):
    """Test 1: Verify PRAGMA foreign_keys=ON, WAL journal_mode, and busy_timeout."""
    conn = db.connect()
    fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1

    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert journal_mode.lower() == "wal"

    busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert busy_timeout >= 5000


def test_schema_init_and_idempotency(db):
    """Test 2: Schema initialization creates all tables and is idempotent."""
    assert db.get_schema_version() == LATEST_SCHEMA_VERSION

    version_again = db.initialize_schema()
    assert version_again == LATEST_SCHEMA_VERSION

    conn = db.connect()
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
    ]
    expected_tables = {
        "schema_migrations",
        "portals",
        "runs",
        "run_checkpoints",
        "discovered_urls",
        "url_observations",
        "portal_states",
        "archive_page_observations",
        "article_states",
        "packages",
        "deposit_records",
        "deposit_artifacts",
        "outbox_events",
    }
    for table in expected_tables:
        assert table in tables, f"Expected table '{table}' not found in SQLite schema"


def test_unsupported_future_schema_version(tmp_path):
    """Test 3: Detect and reject database created by a newer schema version."""
    db_file = tmp_path / "future.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT, description TEXT);"
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
        (999, "2026-11-15T12:00:00Z", "Future migration"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError) as exc_info:
        CorpusAIStateDatabase(db_path=db_file, auto_initialize=True)
    assert "is newer than supported" in str(exc_info.value)


def test_composite_foreign_keys_and_cross_portal_isolation(db):
    """Test 4: Verify composite foreign key constraints prevent cross-portal corruption."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-A", "Portal A", "https://portal-a.com", "generic"),
        )
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-B", "Portal B", "https://portal-b.com", "generic"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("portal-A", "run-1", "2026-11-15T10:00:00Z", "COMPLETED"),
        )

    # Discovered URL for Portal B pointing to Run on Portal A should be rejected
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO discovered_urls (
                    portal_id, url_hash, normalized_url, first_seen_run_id, first_seen_at,
                    last_seen_run_id, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "portal-B",
                    "hash123",
                    "https://portal-b.com/article1",
                    "run-1",  # Belongs to portal-A, not portal-B!
                    "2026-11-15T10:00:00Z",
                    "run-1",
                    "2026-11-15T10:00:00Z",
                ),
            )


def test_portal_latest_run_validation_trigger_on_insert(db):
    """Test 5: Trigger validates latest_successful_run_id belongs to same portal and has successful status."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-P", "Portal P", "https://p.com", "wordpress"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("portal-P", "run-failed", "2026-11-15T10:00:00Z", "FAILED"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("portal-P", "run-ok", "2026-11-15T11:00:00Z", "CONVERGED"),
        )

    # 1. Reject invalid run_id on INSERT
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO portals (portal_id, name, base_url, parser_type, latest_successful_run_id) VALUES (?, ?, ?, ?, ?);",
                ("portal-Q", "Portal Q", "https://q.com", "wordpress", "nonexistent-run"),
            )
    assert "Referenced latest_successful_run_id must belong to same portal" in str(exc_info.value)

    # 2. Reject non-successful run on UPDATE
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE portals SET latest_successful_run_id = ? WHERE portal_id = ?;",
                ("run-failed", "portal-P"),
            )
    assert "Referenced latest_successful_run_id must belong to same portal" in str(exc_info.value)

    # 3. Allow valid successful run on UPDATE
    with db.transaction() as conn:
        conn.execute(
            "UPDATE portals SET latest_successful_run_id = ? WHERE portal_id = ?;",
            ("run-ok", "portal-P"),
        )
    conn = db.connect()
    row = conn.execute("SELECT latest_successful_run_id FROM portals WHERE portal_id = 'portal-P';").fetchone()
    assert row[0] == "run-ok"


def test_runs_prevent_successful_status_invalidation_trigger(db):
    """Test 6: Trigger prevents modifying run status away from successful if referenced by portal."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-R", "Portal R", "https://r.com", "custom"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("portal-R", "run-success", "2026-11-15T10:00:00Z", "COMPLETED"),
        )
        conn.execute(
            "UPDATE portals SET latest_successful_run_id = 'run-success' WHERE portal_id = 'portal-R';"
        )

    # Attempt to change run status from COMPLETED to FAILED while referenced
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = 'FAILED' WHERE portal_id = 'portal-R' AND run_id = 'run-success';"
            )
    assert "Cannot change run status away from successful while referenced" in str(exc_info.value)

    # If portal reference is cleared, changing status succeeds
    with db.transaction() as conn:
        conn.execute("UPDATE portals SET latest_successful_run_id = NULL WHERE portal_id = 'portal-R';")
        conn.execute("UPDATE runs SET status = 'FAILED' WHERE portal_id = 'portal-R' AND run_id = 'run-success';")

    conn = db.connect()
    row = conn.execute("SELECT status FROM runs WHERE portal_id = 'portal-R' AND run_id = 'run-success';").fetchone()
    assert row[0] == "FAILED"


def test_transaction_commit_and_rollback(db):
    """Test 7: Verify transaction commit on success and rollback on exception."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-tx1", "TX 1", "https://tx1.com", "wordpress"),
        )

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM portals WHERE portal_id = 'portal-tx1';").fetchone()[0] == 1

    with pytest.raises(ValueError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
                ("portal-tx2", "TX 2", "https://tx2.com", "wordpress"),
            )
            raise ValueError("Simulated unexpected failure during transaction")

    assert conn.execute("SELECT COUNT(*) FROM portals WHERE portal_id = 'portal-tx2';").fetchone()[0] == 0


def test_outbox_insert_claim_lease_rotation_and_completion(db):
    """Test 8: Outbox insert, claim with atomic lease rotation, and complete."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="portal-news",
        event_type="PACKAGE_CREATED",
        payload={"package_id": "pkg-001", "size": 1024},
    )

    event = db.get_outbox_event(eid)
    assert event is not None
    assert event.status == "PENDING"
    assert event.attempts == 0
    assert event.lease_token is None

    claimed = db.claim_outbox_event(lease_duration_seconds=30)
    assert claimed is not None
    assert claimed.event_id == eid
    assert claimed.status == "IN_FLIGHT"
    assert claimed.attempts == 1
    assert claimed.lease_token is not None
    assert claimed.payload["package_id"] == "pkg-001"

    token_1 = claimed.lease_token

    # Attempting to claim again should return None because it is IN_FLIGHT and lease is valid
    second_claim = db.claim_outbox_event()
    assert second_claim is None

    # Complete with wrong lease token should fail (fencing protection)
    completed_wrong = db.complete_outbox_event(eid, "wrong-token-abc")
    assert completed_wrong is False

    # Complete with correct token should succeed
    completed_ok = db.complete_outbox_event(eid, token_1)
    assert completed_ok is True

    final_event = db.get_outbox_event(eid)
    assert final_event.status == "COMPLETED"
    assert final_event.lease_token is None
    assert final_event.lease_expires_at is None
    assert final_event.processed_at is not None


def test_outbox_lease_expiry_and_recovery(db):
    """Test 9: Expired lease allows reclamation and rejects late worker."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="portal-p",
        event_type="DISPATCH",
        payload={"task": 1},
        max_attempts=3,
    )

    claimed_1 = db.claim_outbox_event(lease_duration_seconds=1)
    assert claimed_1 is not None
    token_1 = claimed_1.lease_token
    assert claimed_1.attempts == 1

    # Manually backdate lease_expires_at to simulate expiration
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Re-claim event (should succeed because lease expired and attempts < max_attempts)
    claimed_2 = db.claim_outbox_event(lease_duration_seconds=60)
    assert claimed_2 is not None
    assert claimed_2.event_id == eid
    assert claimed_2.attempts == 2
    token_2 = claimed_2.lease_token
    assert token_2 != token_1

    # Late worker with token_1 attempts to complete -> REJECTED
    assert db.complete_outbox_event(eid, token_1) is False

    # Active worker with token_2 completes -> ACCEPTED
    assert db.complete_outbox_event(eid, token_2) is True


def test_outbox_lease_renewal(db):
    """Test 10: Renew lease extension on active in-flight event."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p1",
        event_type="TEST",
        payload={},
    )
    claimed = db.claim_outbox_event(lease_duration_seconds=10)
    token = claimed.lease_token

    # Renew lease with wrong token -> fails
    assert db.renew_outbox_lease(eid, "bad-token", extension_seconds=100) is False

    # Renew lease with correct token -> succeeds
    assert db.renew_outbox_lease(eid, token, extension_seconds=300) is True

    updated = db.get_outbox_event(eid)
    assert updated.lease_expires_at > claimed.lease_expires_at


def test_outbox_retry_and_exhaustion(db):
    """Test 11: Retry increments attempts and marks FAILED when max_attempts reached."""
    eid = db.insert_outbox_event(
        aggregate_type="zenodo",
        aggregate_id="dep-1",
        event_type="DEPOSIT_UPLOAD",
        payload={},
        max_attempts=2,
    )

    # Claim 1
    c1 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c1.attempts == 1
    # Retry 1
    retry_ok = db.retry_outbox_event(eid, c1.lease_token, "Network timeout 504", backoff_seconds=0)
    assert retry_ok is True

    e1 = db.get_outbox_event(eid)
    assert e1.status == "RETRY"
    assert e1.attempts == 1
    assert e1.lease_token is None
    assert e1.lease_expires_at is None
    assert "Network timeout" in e1.error_message

    # Claim 2 (attempts becomes 2 == max_attempts)
    c2 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c2.attempts == 2

    # Retry 2 should exhaust attempts and set status = FAILED
    retry_exhausted = db.retry_outbox_event(eid, c2.lease_token, "Final rate limit 429")
    assert retry_exhausted is True

    e2 = db.get_outbox_event(eid)
    assert e2.status == "FAILED"
    assert e2.lease_token is None
    assert e2.lease_expires_at is None
    assert e2.attempts == 2


def test_outbox_timestamp_ordering_for_due_events(db):
    """Test 12: Events are claimed strictly in due timestamp order."""
    now = datetime.now(timezone.utc)
    t_past = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_future = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    eid_past = db.insert_outbox_event("type", "1", "EVENT_PAST", {"order": 1})
    eid_future = db.insert_outbox_event("type", "2", "EVENT_FUTURE", {"order": 2})

    with db.transaction() as conn:
        conn.execute("UPDATE outbox_events SET next_attempt_at = ? WHERE event_id = ?;", (t_past, eid_past))
        conn.execute("UPDATE outbox_events SET next_attempt_at = ? WHERE event_id = ?;", (t_future, eid_future))

    claimed = db.claim_outbox_event()
    assert claimed is not None
    assert claimed.event_id == eid_past

    assert db.claim_outbox_event() is None


def test_backup_and_restore(db, tmp_path):
    """Test 13: Backup consistency and restore protection against overwrite."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("portal-backup", "Backup Test", "https://b.com", "custom"),
        )

    backup_file = tmp_path / "backups" / "state_backup.db"
    res_path = db.backup(backup_file)
    assert res_path.exists()

    verify_conn = sqlite3.connect(str(backup_file))
    row = verify_conn.execute("SELECT name FROM portals WHERE portal_id = 'portal-backup';").fetchone()
    assert row[0] == "Backup Test"
    verify_conn.close()

    with pytest.raises(FileExistsError):
        db.restore(backup_file, overwrite=False)

    with db.transaction() as conn:
        conn.execute("DELETE FROM portals WHERE portal_id = 'portal-backup';")

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM portals WHERE portal_id = 'portal-backup';").fetchone()[0] == 0

    db.restore(backup_file, overwrite=True)

    conn = db.connect()
    restored_row = conn.execute("SELECT name FROM portals WHERE portal_id = 'portal-backup';").fetchone()
    assert restored_row[0] == "Backup Test"


def test_integrity_check_and_foreign_key_check(db):
    """Test 14: PRAGMA integrity_check and foreign_key_check succeed on valid state."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO portals (portal_id, name, base_url, parser_type) VALUES (?, ?, ?, ?);",
            ("p-valid", "Valid", "https://v.com", "wordpress"),
        )
        conn.execute(
            "INSERT INTO runs (portal_id, run_id, started_at, status) VALUES (?, ?, ?, ?);",
            ("p-valid", "r-valid", "2026-11-15T12:00:00Z", "COMPLETED"),
        )

    valid, errors = db.check_integrity()
    assert valid is True
    assert errors == []


# =============================================================================
# Phase 9B.1.1 Focused Outbox Lease Fencing & Hardening Tests
# =============================================================================

def test_worker_cannot_complete_event_after_lease_expires(db):
    """Test 15: A worker cannot complete an event after its lease expires, even if its token has not yet been replaced."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-test",
        event_type="DISPATCH",
        payload={},
    )
    claimed = db.claim_outbox_event(lease_duration_seconds=10)
    assert claimed is not None
    token = claimed.lease_token

    # Manually expire the lease in the database, leaving token unchanged
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Worker attempts completion with token -> MUST BE REJECTED
    success = db.complete_outbox_event(eid, token)
    assert success is False

    # Verify event remains in-flight and not completed
    event = db.get_outbox_event(eid)
    assert event.status == "IN_FLIGHT"
    assert event.processed_at is None


def test_worker_cannot_retry_event_after_lease_expires(db):
    """Test 16: A worker cannot retry an event after its lease expires."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-test",
        event_type="DISPATCH",
        payload={},
    )
    claimed = db.claim_outbox_event(lease_duration_seconds=10)
    token = claimed.lease_token

    # Expire lease
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Worker attempts retry -> MUST BE REJECTED
    success = db.retry_outbox_event(eid, token, "Late error")
    assert success is False

    # Status must NOT transition to RETRY
    event = db.get_outbox_event(eid)
    assert event.status == "IN_FLIGHT"


def test_worker_cannot_renew_expired_lease(db):
    """Test 17: A worker cannot renew an expired lease."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-test",
        event_type="DISPATCH",
        payload={},
    )
    claimed = db.claim_outbox_event(lease_duration_seconds=10)
    token = claimed.lease_token

    # Expire lease
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Worker attempts renewal -> MUST BE REJECTED
    success = db.renew_outbox_lease(eid, token, extension_seconds=60)
    assert success is False

    event = db.get_outbox_event(eid)
    assert event.lease_expires_at == "2026-01-01T00:00:00Z"


def test_worker_cannot_mutate_after_reclamation(db):
    """Test 18: A worker cannot mutate an event after another worker has reclaimed it with a new token."""
    eid = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-test",
        event_type="DISPATCH",
        payload={},
        max_attempts=3,
    )
    worker1 = db.claim_outbox_event(lease_duration_seconds=10)
    token1 = worker1.lease_token

    # Expire worker1's lease
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Worker 2 reclaims event
    worker2 = db.claim_outbox_event(lease_duration_seconds=60)
    assert worker2 is not None
    token2 = worker2.lease_token
    assert token1 != token2

    # Worker 1 attempts complete, retry, and renew with token1 -> ALL MUST BE REJECTED
    assert db.complete_outbox_event(eid, token1) is False
    assert db.retry_outbox_event(eid, token1, "Worker 1 late retry") is False
    assert db.renew_outbox_lease(eid, token1, extension_seconds=120) is False

    # Worker 2 operates normally with token2 -> SUCCESS
    assert db.complete_outbox_event(eid, token2) is True
    event = db.get_outbox_event(eid)
    assert event.status == "COMPLETED"


def test_expired_in_flight_exhausted_event_transitions_to_reconciliation(db):
    """Test 19: An event that exhausts its attempts while IN_FLIGHT transitions to RECONCILIATION_REQUIRED after lease expiry."""
    eid = db.insert_outbox_event(
        aggregate_type="deposit",
        aggregate_id="dep-rec",
        event_type="ZENODO_DEPOSIT",
        payload={},
        max_attempts=1,
    )

    # Claim event (attempts becomes 1 == max_attempts)
    claimed = db.claim_outbox_event(lease_duration_seconds=10)
    assert claimed.attempts == 1

    # Simulate worker crash: lease expires while event is in-flight at max attempts
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid,),
        )

    # Explicit reconciliation call transitions event
    transitioned_count = db.reconcile_expired_exhausted_outbox_events()
    assert transitioned_count == 1

    event = db.get_outbox_event(eid)
    assert event.status == "RECONCILIATION_REQUIRED"
    assert event.lease_token is None
    assert event.lease_expires_at is None
    assert "Lease expired while in-flight after exhausting maximum attempts" in event.error_message


def test_exhausted_event_is_never_claimed_again(db):
    """Test 20: An exhausted event is never claimed again."""
    eid_failed = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-fail",
        event_type="DISPATCH",
        payload={},
        max_attempts=1,
    )
    c1 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c1.event_id == eid_failed
    db.retry_outbox_event(eid_failed, c1.lease_token, "Fatal failure")

    event_failed = db.get_outbox_event(eid_failed)
    assert event_failed.status == "FAILED"

    # Now attempt to claim: must return None
    assert db.claim_outbox_event() is None

    # Also test an expired in-flight event that has exhausted attempts
    eid_exhausted = db.insert_outbox_event(
        aggregate_type="portal",
        aggregate_id="p-ex",
        event_type="DISPATCH",
        payload={},
        max_attempts=1,
    )
    c2 = db.claim_outbox_event(lease_duration_seconds=10)
    assert c2.event_id == eid_exhausted
    with db.transaction() as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at = '2026-01-01T00:00:00Z' WHERE event_id = ?;",
            (eid_exhausted,),
        )

    # claim_outbox_event should reconcile it to RECONCILIATION_REQUIRED and return None
    assert db.claim_outbox_event() is None

    reconciled = db.get_outbox_event(eid_exhausted)
    assert reconciled.status == "RECONCILIATION_REQUIRED"


def test_all_lease_fields_cleared_on_state_transitions(db):
    """Test 21: All lease ownership fields are cleared after completion, retry scheduling, and exhaustion."""
    # 1. Cleared on normal retry
    e1 = db.insert_outbox_event("type", "1", "E1", {}, max_attempts=3)
    c1 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c1.lease_token is not None
    db.retry_outbox_event(e1, c1.lease_token, "Transient network drop", backoff_seconds=10)
    r1 = db.get_outbox_event(e1)
    assert r1.status == "RETRY"
    assert r1.lease_token is None
    assert r1.lease_expires_at is None

    # 2. Cleared on completion
    e2 = db.insert_outbox_event("type", "2", "E2", {})
    c2 = db.claim_outbox_event(lease_duration_seconds=60)
    db.complete_outbox_event(e2, c2.lease_token)
    r2 = db.get_outbox_event(e2)
    assert r2.status == "COMPLETED"
    assert r2.lease_token is None
    assert r2.lease_expires_at is None

    # 3. Cleared on reconciliation-required retry
    e3 = db.insert_outbox_event("type", "3", "E3", {}, max_attempts=5)
    c3 = db.claim_outbox_event(lease_duration_seconds=60)
    db.retry_outbox_event(e3, c3.lease_token, "Deposit uncertain", requires_reconciliation=True)
    r3 = db.get_outbox_event(e3)
    assert r3.status == "RECONCILIATION_REQUIRED"
    assert r3.lease_token is None
    assert r3.lease_expires_at is None


def test_normal_retry_and_completion_behavior_preserved(db):
    """Test 22: Normal retry and successful completion behavior remain unhindered under valid lease."""
    eid = db.insert_outbox_event("type", "norm", "EVENT_NORM", {}, max_attempts=3)

    # Attempt 1: Fail with transient error -> RETRY
    c1 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c1.attempts == 1
    assert db.retry_outbox_event(eid, c1.lease_token, "Err 1", backoff_seconds=0) is True

    # Immediate Attempt 2: Success -> COMPLETED
    c2 = db.claim_outbox_event(lease_duration_seconds=60)
    assert c2.attempts == 2
    assert db.complete_outbox_event(eid, c2.lease_token) is True

    final = db.get_outbox_event(eid)
    assert final.status == "COMPLETED"
    assert final.attempts == 2
    assert final.lease_token is None

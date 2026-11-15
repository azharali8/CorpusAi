"""
CorpusAI SQLite Storage Backend Implementation.

Provides:
- CorpusAIStateDatabase: Connection lifecycle, WAL mode, transaction context management.
- Schema migration execution and tracking.
- Primitives for transactional outbox pattern (insert, claim with lease rotation, complete, retry,
  reconcile expired exhausted events).
- Backup and restore functions using sqlite3.Connection.backup().
"""

import sqlite3
import json
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Generator, Tuple
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from src.storage.schema import LATEST_SCHEMA_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Base exception for storage errors."""
    pass


class MigrationError(DatabaseError):
    """Raised when database schema migration fails or version is unsupported."""
    pass


class DatabaseLockedError(DatabaseError):
    """Raised when a database lock or busy timeout is encountered."""
    pass


class OutboxStatus(str, Enum):
    """
    Standardized statuses for the Transactional Outbox pattern.

    - PENDING: Event created and awaiting initial pickup.
    - IN_FLIGHT: Event currently claimed by a worker under an active lease token.
    - RETRY: Event failed a transient attempt and is awaiting next retry attempt.
    - COMPLETED: Event successfully processed and acknowledged.
    - FAILED: Event reached max attempts or encountered a definitive non-recoverable error
      requiring no human or external state reconciliation.
    - RECONCILIATION_REQUIRED: Event reached max attempts or failed where external side-effects
      are indeterminate (e.g. timeout during HTTP POST/PUT/deposit or lease expired while in-flight
      after max attempts). Requires manual or operator reconciliation.
    """
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    RETRY = "RETRY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


@dataclass(frozen=True)
class OutboxEventRecord:
    """Represents a claimed or fetched outbox event record."""
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload_json: str
    status: str
    created_at: str
    attempts: int
    max_attempts: int
    last_attempt_at: Optional[str]
    next_attempt_at: str
    lease_token: Optional[str]
    lease_expires_at: Optional[str]
    error_message: Optional[str]
    processed_at: Optional[str]

    @property
    def payload(self) -> Dict[str, Any]:
        """Parse payload JSON string into Python dict."""
        try:
            return json.loads(self.payload_json)
        except Exception:
            return {}


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 string format (Z suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_iso(dt: datetime) -> str:
    """Format a datetime in ISO-8601 UTC string format (Z suffix)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class CorpusAIStateDatabase:
    """
    Manages SQLite connection, transactional execution, migrations,
    outbox state transitions, and snapshot backup/restore.
    """

    def __init__(
        self,
        db_path: str | Path = "storage/corpusai_state.db",
        timeout: float = 5.0,
        enable_wal: bool = True,
        auto_initialize: bool = True,
    ):
        self.db_path = Path(db_path)
        self.timeout = timeout
        self.enable_wal = enable_wal
        self._connection: Optional[sqlite3.Connection] = None

        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if auto_initialize:
            self.initialize_schema()

    def connect(self) -> sqlite3.Connection:
        """
        Open or reuse the underlying SQLite connection configured with
        foreign keys, WAL mode, busy timeout, and row factory.
        """
        if self._connection is not None:
            return self._connection

        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self.timeout,
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode; we manage transactions explicitly
            )
            conn.row_factory = sqlite3.Row

            # Enforce foreign keys and busy timeout on every connection
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)};")

            if self.enable_wal and str(self.db_path) != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = NORMAL;")

            self._connection = conn
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                raise DatabaseLockedError(f"Database locked or busy: {e}") from e
            raise DatabaseError(f"Failed to connect to SQLite: {e}") from e
        except Exception as e:
            raise DatabaseError(f"Unexpected connection error: {e}") from e

    def close(self) -> None:
        """Close connection if open."""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as e:
                logger.warning(f"Error closing SQLite connection: {e}")
            finally:
                self._connection = None

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for atomic transactions.
        Issues BEGIN IMMEDIATE, yields connection, and commits or rolls back on exception.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                raise DatabaseLockedError(f"Database locked attempting BEGIN IMMEDIATE: {e}") from e
            raise DatabaseError(f"Transaction begin failed: {e}") from e

        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK;")
            except Exception as rollback_err:
                logger.error(f"Rollback failed: {rollback_err}")
            raise exc

    def get_schema_version(self) -> int:
        """Query current applied schema version from schema_migrations table."""
        conn = self.connect()
        cursor = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
        )
        if not cursor.fetchone():
            return 0

        cursor = conn.execute("SELECT MAX(version) FROM schema_migrations;")
        row = cursor.fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return 0

    def initialize_schema(self) -> int:
        """
        Run all pending migrations in sequence within transaction blocks.
        Validates unsupported schema versions.
        """
        current_version = self.get_schema_version()
        if current_version > LATEST_SCHEMA_VERSION:
            raise MigrationError(
                f"Database schema version {current_version} is newer than supported {LATEST_SCHEMA_VERSION}. Downgrade or upgrade application."
            )

        for version, description, statements in MIGRATIONS:
            if version > current_version:
                logger.info(f"Applying migration v{version}: {description}")
                try:
                    with self.transaction() as conn:
                        for stmt in statements:
                            stmt_clean = stmt.strip()
                            if stmt_clean:
                                conn.execute(stmt_clean)
                        now_str = _utc_now_iso()
                        conn.execute(
                            "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?);",
                            (version, now_str, description),
                        )
                except Exception as e:
                    raise MigrationError(f"Migration v{version} failed: {e}") from e

        return self.get_schema_version()

    def check_integrity(self) -> Tuple[bool, List[str]]:
        """
        Run PRAGMA integrity_check and PRAGMA foreign_key_check.
        Returns (is_valid, list_of_errors).
        """
        conn = self.connect()
        errors: List[str] = []

        # 1. Integrity check
        cursor = conn.execute("PRAGMA integrity_check;")
        rows = [r[0] for r in cursor.fetchall()]
        if rows != ["ok"]:
            errors.extend([f"Integrity check error: {r}" for r in rows if r != "ok"])

        # 2. Foreign key check
        fk_cursor = conn.execute("PRAGMA foreign_key_check;")
        fk_rows = fk_cursor.fetchall()
        for r in fk_rows:
            table_name = r[0]
            rowid = r[1]
            parent_table = r[2]
            fkid = r[3]
            errors.append(f"Foreign key violation: table '{table_name}' rowid {rowid} references '{parent_table}' (fk index {fkid})")

        return len(errors) == 0, errors

    def backup(self, dest_path: str | Path) -> Path:
        """
        Perform an online, consistent backup of the database to dest_path using SQLite backup API.
        """
        dest_p = Path(dest_path)
        dest_p.parent.mkdir(parents=True, exist_ok=True)

        source_conn = self.connect()
        dest_conn = sqlite3.connect(str(dest_p))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()

        return dest_p

    def restore(self, backup_path: str | Path, overwrite: bool = False) -> None:
        """
        Restore the database from backup_path.
        If current db_path exists and overwrite is False, raises FileExistsError.
        """
        b_path = Path(backup_path)
        if not b_path.exists():
            raise FileNotFoundError(f"Backup file not found: {b_path}")

        if str(self.db_path) != ":memory:":
            if self.db_path.exists() and not overwrite:
                raise FileExistsError(
                    f"Target database file {self.db_path} already exists. Specify overwrite=True to replace."
                )

        self.close()

        if str(self.db_path) != ":memory:":
            if self.db_path.exists():
                self.db_path.unlink()
            dest_conn = sqlite3.connect(str(self.db_path))
            src_conn = sqlite3.connect(str(b_path))
            try:
                src_conn.backup(dest_conn)
            finally:
                src_conn.close()
                dest_conn.close()
        else:
            self.connect()
            src_conn = sqlite3.connect(str(b_path))
            try:
                src_conn.backup(self._connection)
            finally:
                src_conn.close()

    # =========================================================================
    # Transactional Outbox Primitives
    # =========================================================================

    def insert_outbox_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Dict[str, Any],
        max_attempts: int = 5,
        event_id: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """
        Insert an outbox event. If conn is provided, runs inside that transaction.
        Otherwise executes within its own transaction.
        """
        eid = event_id or str(uuid.uuid4())
        payload_str = json.dumps(payload, ensure_ascii=False)
        now_str = _utc_now_iso()

        sql = """
        INSERT INTO outbox_events (
            event_id, aggregate_type, aggregate_id, event_type, payload_json,
            status, created_at, attempts, max_attempts, next_attempt_at
        ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, 0, ?, ?);
        """

        if conn is not None:
            conn.execute(sql, (eid, aggregate_type, aggregate_id, event_type, payload_str, now_str, max_attempts, now_str))
        else:
            with self.transaction() as t_conn:
                t_conn.execute(sql, (eid, aggregate_type, aggregate_id, event_type, payload_str, now_str, max_attempts, now_str))

        return eid

    def reconcile_expired_exhausted_outbox_events(self) -> int:
        """
        Finds any IN_FLIGHT events whose lease has expired AND whose attempts >= max_attempts,
        and transitions them to RECONCILIATION_REQUIRED, clearing lease ownership fields.
        This prevents exhausted events whose lease expired from remaining stranded in IN_FLIGHT.
        Returns the number of events transitioned.
        """
        now = datetime.now(timezone.utc)
        now_str = _format_iso(now)

        with self.transaction() as conn:
            sql = """
            UPDATE outbox_events
            SET status = 'RECONCILIATION_REQUIRED',
                lease_token = NULL,
                lease_expires_at = NULL,
                error_message = coalesce(error_message, 'Lease expired while in-flight after exhausting maximum attempts')
            WHERE status = 'IN_FLIGHT'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
              AND attempts >= max_attempts;
            """
            cursor = conn.execute(sql, (now_str,))
            return cursor.rowcount

    def claim_outbox_event(
        self,
        lease_duration_seconds: int = 60,
    ) -> Optional[OutboxEventRecord]:
        """
        Atomically find and claim the next due outbox event:
        1. First reconciles any expired in-flight events that have exhausted max_attempts
           to RECONCILIATION_REQUIRED.
        2. Selects the oldest due candidate:
           - status in ('PENDING', 'RETRY') AND next_attempt_at <= now AND attempts < max_attempts
           - OR status = 'IN_FLIGHT' AND lease_expires_at < now AND attempts < max_attempts
        3. Rotates lease_token to a new UUID, increments attempts, sets status = 'IN_FLIGHT',
           and sets lease_expires_at = now + lease_duration_seconds.
        Returns OutboxEventRecord or None if no eligible due events exist.
        """
        # Step 1: Clean up any expired exhausted events
        self.reconcile_expired_exhausted_outbox_events()

        now = datetime.now(timezone.utc)
        now_str = _format_iso(now)
        lease_expires = now + timedelta(seconds=lease_duration_seconds)
        lease_expires_str = _format_iso(lease_expires)
        new_token = str(uuid.uuid4())

        with self.transaction() as conn:
            # Select oldest due event where attempts < max_attempts
            select_sql = """
            SELECT event_id, attempts, max_attempts
            FROM outbox_events
            WHERE (
                (status IN ('PENDING', 'RETRY') AND next_attempt_at <= ?)
                OR
                (status = 'IN_FLIGHT' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
            )
            AND attempts < max_attempts
            ORDER BY next_attempt_at ASC, created_at ASC
            LIMIT 1;
            """
            row = conn.execute(select_sql, (now_str, now_str)).fetchone()
            if not row:
                return None

            event_id = row["event_id"]
            new_attempts = row["attempts"] + 1

            update_sql = """
            UPDATE outbox_events
            SET status = 'IN_FLIGHT',
                attempts = ?,
                last_attempt_at = ?,
                lease_token = ?,
                lease_expires_at = ?
            WHERE event_id = ?;
            """
            conn.execute(update_sql, (new_attempts, now_str, new_token, lease_expires_str, event_id))

            # Fetch updated record
            fetch_sql = "SELECT * FROM outbox_events WHERE event_id = ?;"
            rec_row = conn.execute(fetch_sql, (event_id,)).fetchone()
            return self._row_to_outbox_record(rec_row)

    def renew_outbox_lease(
        self,
        event_id: str,
        lease_token: str,
        extension_seconds: int = 60,
    ) -> bool:
        """
        Extend the lease on an IN_FLIGHT event if:
        1. event_id matches
        2. status == 'IN_FLIGHT'
        3. lease_token matches currently held token
        4. lease_expires_at > now (the lease must NOT be expired)

        Cannot revive an expired lease or overwrite a newer worker's lease.
        Returns True if renewed, False otherwise.
        """
        now = datetime.now(timezone.utc)
        now_str = _format_iso(now)
        new_expiry = now + timedelta(seconds=extension_seconds)
        new_expiry_str = _format_iso(new_expiry)

        with self.transaction() as conn:
            sql = """
            UPDATE outbox_events
            SET lease_expires_at = ?
            WHERE event_id = ?
              AND lease_token = ?
              AND status = 'IN_FLIGHT'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?;
            """
            cursor = conn.execute(sql, (new_expiry_str, event_id, lease_token, now_str))
            return cursor.rowcount > 0

    def complete_outbox_event(
        self,
        event_id: str,
        lease_token: str,
    ) -> bool:
        """
        Mark an IN_FLIGHT event as COMPLETED if:
        1. event_id matches
        2. status == 'IN_FLIGHT'
        3. lease_token matches currently held token
        4. lease_expires_at > now (the lease must NOT be expired)

        Releases lease_token and lease_expires_at (sets to NULL).
        Returns True if successfully completed, False if lease was expired, token mismatched,
        or status was not IN_FLIGHT.
        """
        now = datetime.now(timezone.utc)
        now_str = _format_iso(now)

        with self.transaction() as conn:
            sql = """
            UPDATE outbox_events
            SET status = 'COMPLETED',
                processed_at = ?,
                lease_token = NULL,
                lease_expires_at = NULL,
                error_message = NULL
            WHERE event_id = ?
              AND lease_token = ?
              AND status = 'IN_FLIGHT'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?;
            """
            cursor = conn.execute(sql, (now_str, event_id, lease_token, now_str))
            return cursor.rowcount > 0

    def retry_outbox_event(
        self,
        event_id: str,
        lease_token: str,
        error_message: str,
        backoff_seconds: int = 30,
        requires_reconciliation: bool = False,
    ) -> bool:
        """
        Record a failure for an IN_FLIGHT event held by lease_token:
        Validates:
        1. event_id matches
        2. status == 'IN_FLIGHT'
        3. lease_token matches currently held token
        4. lease_expires_at > now (the lease must NOT be expired)

        State transitions:
        - If attempts < max_attempts and not requires_reconciliation:
            status = 'RETRY', next_attempt_at = now + backoff_seconds
        - If attempts >= max_attempts or requires_reconciliation:
            - If requires_reconciliation is True: status = 'RECONCILIATION_REQUIRED'
            - Otherwise: status = 'FAILED' (definitive failure requiring no reconciliation)

        Always clears lease_token and lease_expires_at to NULL.
        Returns True if updated, False if lease was expired, token mismatched, or status was not IN_FLIGHT.
        """
        now = datetime.now(timezone.utc)
        now_str = _format_iso(now)
        next_dt = now + timedelta(seconds=backoff_seconds)
        next_str = _format_iso(next_dt)

        with self.transaction() as conn:
            # Verify event identity, token, status, and active unexpired lease
            row = conn.execute(
                """
                SELECT attempts, max_attempts
                FROM outbox_events
                WHERE event_id = ?
                  AND lease_token = ?
                  AND status = 'IN_FLIGHT'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?;
                """,
                (event_id, lease_token, now_str),
            ).fetchone()
            if not row:
                return False

            attempts = row["attempts"]
            max_attempts = row["max_attempts"]

            if requires_reconciliation:
                new_status = OutboxStatus.RECONCILIATION_REQUIRED.value
            elif attempts >= max_attempts:
                new_status = OutboxStatus.FAILED.value
            else:
                new_status = OutboxStatus.RETRY.value

            sql = """
            UPDATE outbox_events
            SET status = ?,
                error_message = ?,
                next_attempt_at = ?,
                lease_token = NULL,
                lease_expires_at = NULL
            WHERE event_id = ? AND lease_token = ?;
            """
            cursor = conn.execute(sql, (new_status, error_message, next_str, event_id, lease_token))
            return cursor.rowcount > 0

    def get_outbox_event(self, event_id: str) -> Optional[OutboxEventRecord]:
        """Fetch outbox event by event_id."""
        conn = self.connect()
        row = conn.execute("SELECT * FROM outbox_events WHERE event_id = ?;", (event_id,)).fetchone()
        if not row:
            return None
        return self._row_to_outbox_record(row)

    @staticmethod
    def _row_to_outbox_record(row: sqlite3.Row) -> OutboxEventRecord:
        return OutboxEventRecord(
            event_id=row["event_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            event_type=row["event_type"],
            payload_json=row["payload_json"],
            status=row["status"],
            created_at=row["created_at"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            last_attempt_at=row["last_attempt_at"],
            next_attempt_at=row["next_attempt_at"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            error_message=row["error_message"],
            processed_at=row["processed_at"],
        )

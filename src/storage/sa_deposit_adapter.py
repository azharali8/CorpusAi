"""
SQLAlchemy 2.0 Persistence Adapter for CorpusAI Deposit Records.

Implements encapsulated persistence boundaries:
- Engine creation with NullPool, WAL mode, busy timeout, and PRAGMA foreign_keys = ON.
- atomic_write(session) -> commit or rollback
- atomic_read(session) -> read with automatic session cleanup
- save_receipt(receipt) -> atomic upsert of record and full artifact synchronization
- get_receipt(portal_id, run_id) -> domain model reconstruction with full field fidelity
- list_receipts(portal_id) -> list all receipts for a portal

Preserves domain model isolation: neither DepositOrchestrator nor callers touch SQLAlchemy sessions.
"""

from contextlib import contextmanager
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union

from sqlalchemy import Engine, create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from src.storage.sa_models import DepositArtifactORM, DepositRecordORM
from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord

logger = logging.getLogger("corpusai.storage.sa_deposit")


def create_sa_deposit_engine(
    db_path: Union[str, Path],
    timeout: float = 5.0,
) -> Engine:
    """
    Create a SQLAlchemy 2.0 Engine bound to the authoritative SQLite database.

    Configuration:
    - NullPool: No persistent connection pooling to avoid locking SQLite files.
    - connect listener:
        - PRAGMA busy_timeout = {timeout_ms}.
        - PRAGMA foreign_keys = ON. Strict foreign key enforcement on all SA connections.
    """
    is_memory = (str(db_path) == ":memory:")
    path_str = ":memory:" if is_memory else str(Path(db_path).resolve())
    url = "sqlite:///:memory:" if is_memory else f"sqlite:///{path_str}"
    pool_cls = StaticPool if is_memory else NullPool

    engine = create_engine(
        url,
        poolclass=pool_cls,
        connect_args={"timeout": timeout, "check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)};")
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.close()

    return engine


class SADepositPersistenceAdapter:
    """
    Persistence adapter encapsulating all SQLAlchemy session and transaction management.
    """

    def __init__(
        self,
        engine_or_path: Union[Engine, str, Path],
        timeout: float = 5.0,
    ):
        if isinstance(engine_or_path, Engine):
            self.engine = engine_or_path
            self._owns_engine = False
        else:
            self.engine = create_sa_deposit_engine(engine_or_path, timeout=timeout)
            self._owns_engine = True

        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            future=True,
        )

    def close(self) -> None:
        """Dispose engine if created internally."""
        if self._owns_engine:
            self.engine.dispose()

    @contextmanager
    def atomic_write(self) -> Generator[Session, None, None]:
        """
        Context manager for write transactions.
        Commits on successful exit, rolls back on exception, always closes session.
        """
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"atomic_write rolled back due to error: {e}")
            raise
        finally:
            session.close()

    @contextmanager
    def atomic_read(self) -> Generator[Session, None, None]:
        """
        Context manager for read operations.
        Ensures clean read boundary and guarantees session cleanup without uncommitted changes.
        """
        session: Session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def package_exists(self, portal_id: str, package_id: str) -> bool:
        """
        Check if the parent package record exists in the authoritative database.
        Used to verify foreign key prerequisites before inserting deposit records.
        """
        with self.atomic_read() as session:
            row = session.execute(
                text("SELECT 1 FROM packages WHERE portal_id = :portal_id AND package_id = :package_id"),
                {"portal_id": portal_id, "package_id": package_id},
            ).scalar()
            return row is not None

    def _persist_receipt_in_session(self, session: Session, receipt: DepositReceipt) -> bool:
        """
        Internal worker that validates foreign key prerequisites and persists a single receipt
        and its artifacts within an existing Session transaction.

        Returns True if persisted, False if skipped due to missing parent package record.
        """
        portal_id = receipt.portal_id
        deposit_id = receipt.receipt_id
        run_id = receipt.run_id

        # Verify parent package exists before attempting insertion
        pkg_row = session.execute(
            text("SELECT 1 FROM packages WHERE portal_id = :portal_id AND package_id = :package_id"),
            {"portal_id": portal_id, "package_id": run_id},
        ).scalar()
        if not pkg_row:
            logger.warning(
                f"Skipping database persistence for deposit receipt '{deposit_id}': "
                f"Parent package ({portal_id}, {run_id}) does not exist in 'packages' table. "
                "Foreign key constraint preserved."
            )
            return False

        # Check for existing record
        record_orm = session.get(DepositRecordORM, (portal_id, deposit_id))

        meta: Dict[str, Any] = {}
        if record_orm is not None and record_orm.metadata_json:
            try:
                meta = json.loads(record_orm.metadata_json)
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}

        # Update metadata dict while preserving unrelated existing keys
        meta.update({
            "package_fingerprint": receipt.package_fingerprint,
            "environment": receipt.environment,
            "remote_record_id": receipt.remote_record_id,
            "remote_bucket_url": receipt.remote_bucket_url,
            "remote_record_url": receipt.remote_record_url,
            "checksum_verification_outcome": receipt.checksum_verification_outcome,
            "last_error_category": receipt.last_error_category,
            "retry_eligible": receipt.retry_eligible,
            "reconciliation_notes": receipt.reconciliation_notes,
            "updated_at": receipt.updated_at,
            "artifact_inventory_extras": {
                fname: {
                    "local_md5": art.local_md5,
                    "remote_md5": art.remote_md5,
                }
                for fname, art in receipt.artifact_inventory.items()
            },
        })
        metadata_json = json.dumps(meta, ensure_ascii=False)

        if record_orm is None:
            record_orm = DepositRecordORM(
                portal_id=portal_id,
                deposit_id=deposit_id,
                package_id=run_id,
                target_repository=receipt.repository_target,
                deposition_id=receipt.remote_record_id,
                status=str(receipt.status),
                doi=receipt.doi,
                concept_doi=None,
                badge_url=None,
                submitted_at=receipt.created_at,
                completed_at=receipt.updated_at if str(receipt.status) == "PUBLISHED" else None,
                error_message=receipt.last_error,
                metadata_json=metadata_json,
            )
            session.add(record_orm)
        else:
            record_orm.package_id = run_id
            record_orm.target_repository = receipt.repository_target
            record_orm.deposition_id = receipt.remote_record_id
            record_orm.status = str(receipt.status)
            record_orm.doi = receipt.doi
            record_orm.submitted_at = receipt.created_at
            record_orm.completed_at = receipt.updated_at if str(receipt.status) == "PUBLISHED" else None
            record_orm.error_message = receipt.last_error
            record_orm.metadata_json = metadata_json

        # Synchronize artifacts: delete existing, re-add current inventory
        stmt_artifacts = select(DepositArtifactORM).where(
            DepositArtifactORM.portal_id == portal_id,
            DepositArtifactORM.deposit_id == deposit_id,
        )
        existing_artifacts = session.scalars(stmt_artifacts).all()
        for art_orm in existing_artifacts:
            session.delete(art_orm)

        for rel_path, art in receipt.artifact_inventory.items():
            new_art = DepositArtifactORM(
                portal_id=portal_id,
                deposit_id=deposit_id,
                artifact_filename=art.filename,
                file_size_bytes=art.filesize,
                sha256_checksum=art.local_sha256,
                remote_file_id=art.remote_file_id,
                upload_status="VERIFIED" if art.verified else "UPLOADED",
                uploaded_at=receipt.updated_at,
            )
            session.add(new_art)
        return True

    def save_receipt(self, receipt: DepositReceipt) -> bool:
        """
        Atomically persist a DepositReceipt and its UploadedArtifactRecord inventory.
        Performs full artifact synchronization (stale artifacts are deleted).
        Returns True if persisted, False if skipped due to missing parent package record.
        """
        with self.atomic_write() as session:
            return self._persist_receipt_in_session(session, receipt)

    def save_receipts_batch(
        self,
        receipts: List[DepositReceipt],
        migration_marker: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Atomically persist multiple DepositReceipts inside a single database transaction.
        If ``migration_marker`` is provided (keys: ``migration_id``, ``source_path``,
        ``source_sha256``, ``completed_at``), records completion in ``data_migrations``
        within the exact same transaction so a failed batch never leaves a marker behind.
        Returns the number of successfully persisted receipts.
        """
        count = 0
        with self.atomic_write() as session:
            for receipt in receipts:
                if self._persist_receipt_in_session(session, receipt):
                    count += 1
            if migration_marker is not None:
                session.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS data_migrations ("
                        "migration_id TEXT PRIMARY KEY, "
                        "source_path TEXT NOT NULL, "
                        "source_sha256 TEXT NOT NULL, "
                        "imported_count INTEGER NOT NULL DEFAULT 0, "
                        "completed_at TEXT NOT NULL)"
                    )
                )
                session.execute(
                    text(
                        "INSERT INTO data_migrations "
                        "(migration_id, source_path, source_sha256, imported_count, completed_at) "
                        "VALUES (:migration_id, :source_path, :source_sha256, :imported_count, :completed_at) "
                        "ON CONFLICT(migration_id) DO UPDATE SET "
                        "source_path = excluded.source_path, "
                        "source_sha256 = excluded.source_sha256, "
                        "imported_count = excluded.imported_count, "
                        "completed_at = excluded.completed_at"
                    ),
                    {
                        "migration_id": migration_marker["migration_id"],
                        "source_path": migration_marker["source_path"],
                        "source_sha256": migration_marker["source_sha256"],
                        "imported_count": count,
                        "completed_at": migration_marker["completed_at"],
                    },
                )
        return count

    def get_data_migration_marker(self, migration_id: str) -> Optional[Dict[str, Any]]:
        """Return the recorded data_migrations row for ``migration_id``, or None."""
        with self.atomic_read() as session:
            table_exists = session.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='data_migrations'")
            ).scalar()
            if not table_exists:
                return None
            row = session.execute(
                text(
                    "SELECT migration_id, source_path, source_sha256, imported_count, completed_at "
                    "FROM data_migrations WHERE migration_id = :migration_id"
                ),
                {"migration_id": migration_id},
            ).mappings().first()
            return dict(row) if row is not None else None

    def _reconstruct_receipt(self, record: DepositRecordORM, artifacts: List[DepositArtifactORM]) -> DepositReceipt:
        """
        Helper to reconstruct a DepositReceipt domain model from an ORM record and its artifacts.
        Raises ValueError with chained exception on corrupted metadata JSON.
        """
        deposit_id = record.deposit_id
        portal_id = record.portal_id
        meta: Dict[str, Any] = {}

        if record.metadata_json is not None:
            try:
                meta = json.loads(record.metadata_json)
                if not isinstance(meta, dict):
                    raise ValueError(f"Corrupted metadata_json: expected JSON object, got {type(meta).__name__}")
            except Exception as e:
                raise ValueError(
                    f"Failed to deserialize metadata_json for deposit record ({portal_id}, {deposit_id}): {e}"
                ) from e

        # Determine environment with explicit recognition of known targets
        env = meta.get("environment")
        if not env:
            target_repo = (record.target_repository or "").strip().lower()
            if target_repo in ("zenodo_sandbox", "sandbox") or (target_repo.startswith("zenodo") and "sandbox" in target_repo):
                env = "sandbox"
            elif target_repo in ("zenodo", "dataverse"):
                env = "production"
            else:
                env = "unknown"

        extras = meta.get("artifact_inventory_extras", {})
        inventory: Dict[str, UploadedArtifactRecord] = {}
        for ar in artifacts:
            fname = ar.artifact_filename
            extra_art = extras.get(fname, {}) if isinstance(extras, dict) else {}
            inventory[fname] = UploadedArtifactRecord(
                filename=fname,
                filesize=ar.file_size_bytes,
                local_sha256=ar.sha256_checksum,
                local_md5=extra_art.get("local_md5", ""),
                remote_md5=extra_art.get("remote_md5"),
                remote_file_id=ar.remote_file_id,
                verified=(ar.upload_status == "VERIFIED"),
            )

        receipt = DepositReceipt(
            receipt_id=deposit_id,
            portal_id=portal_id,
            run_id=record.package_id,
            package_fingerprint=meta.get("package_fingerprint", ""),
            repository_target=record.target_repository,
            environment=env,
            status=record.status,
            created_at=record.submitted_at,
            updated_at=meta.get("updated_at") or record.completed_at or record.submitted_at,
            remote_record_id=record.deposition_id or meta.get("remote_record_id"),
            remote_bucket_url=meta.get("remote_bucket_url"),
            remote_record_url=meta.get("remote_record_url"),
            doi=record.doi,
            artifact_inventory=inventory,
            checksum_verification_outcome=meta.get("checksum_verification_outcome", "UNVERIFIED"),
            last_error=record.error_message,
            last_error_category=meta.get("last_error_category"),
            retry_eligible=meta.get("retry_eligible", True),
            reconciliation_notes=meta.get("reconciliation_notes"),
        )
        return receipt

    def get_receipt(self, portal_id: str, run_id: str) -> Optional[DepositReceipt]:
        """
        Reconstruct a DepositReceipt domain object by (portal_id, run_id).
        Note: deposit_records.package_id corresponds to run_id.
        """
        with self.atomic_read() as session:
            stmt = select(DepositRecordORM).where(
                DepositRecordORM.portal_id == portal_id,
                DepositRecordORM.package_id == run_id,
            ).order_by(DepositRecordORM.submitted_at.desc())

            record = session.scalars(stmt).first()
            if record is None:
                return None

            stmt_art = select(DepositArtifactORM).where(
                DepositArtifactORM.portal_id == portal_id,
                DepositArtifactORM.deposit_id == record.deposit_id,
            ).order_by(DepositArtifactORM.artifact_filename.asc())
            art_records = session.scalars(stmt_art).all()

            return self._reconstruct_receipt(record, art_records)

    def list_receipts(self, portal_id: str) -> List[DepositReceipt]:
        """
        List all receipts for a portal.
        Reconstructs domain models directly from queried records and artifacts,
        avoiding N+1 queries.
        """
        with self.atomic_read() as session:
            stmt = select(DepositRecordORM).where(
                DepositRecordORM.portal_id == portal_id
            ).order_by(DepositRecordORM.submitted_at.asc())
            records = session.scalars(stmt).all()

            if not records:
                return []

            deposit_ids = [r.deposit_id for r in records]
            stmt_arts = select(DepositArtifactORM).where(
                DepositArtifactORM.portal_id == portal_id,
                DepositArtifactORM.deposit_id.in_(deposit_ids),
            ).order_by(DepositArtifactORM.artifact_filename.asc())
            all_arts = session.scalars(stmt_arts).all()

            # Group artifacts by deposit_id
            arts_by_deposit: Dict[str, List[DepositArtifactORM]] = {did: [] for did in deposit_ids}
            for art in all_arts:
                if art.deposit_id in arts_by_deposit:
                    arts_by_deposit[art.deposit_id].append(art)

            return [self._reconstruct_receipt(r, arts_by_deposit[r.deposit_id]) for r in records]

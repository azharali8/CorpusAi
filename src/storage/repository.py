"""
CorpusAI SQLite Repository / DAO Layer.

Provides typed, parameterized data access abstractions for:
- Portals and runs
- Discovered URLs and observations
- Portal state snapshots, archive page observations, and article states
- Packages, deposit records, and uploaded artifacts

Maintains same-portal composite foreign key boundaries and supports atomic transaction participation.
"""

import json
import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from src.storage.sqlite_backend import CorpusAIStateDatabase


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CorpusAIRepository:
    """
    Data Access Object exposing clean methods over the complete SQLite operational state schema.
    """

    def __init__(self, db: CorpusAIStateDatabase):
        self.db = db

    # =========================================================================
    # Portals & Runs
    # =========================================================================

    def upsert_portal(
        self,
        portal_id: str,
        name: str,
        base_url: str,
        parser_type: str = "generic",
        latest_successful_run_id: Optional[str] = None,
        legacy_imported: Optional[bool] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update a portal record, optionally setting legacy_imported."""
        now_str = _utc_now_iso()
        # In SQLite, we store legacy_imported flag inside schema_migrations or metadata if column exists
        sql = """
        INSERT INTO portals (portal_id, name, base_url, parser_type, latest_successful_run_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id) DO UPDATE SET
            name = excluded.name,
            base_url = excluded.base_url,
            parser_type = excluded.parser_type,
            latest_successful_run_id = coalesce(excluded.latest_successful_run_id, portals.latest_successful_run_id),
            updated_at = excluded.updated_at;
        """
        params = (portal_id, name, base_url, parser_type, latest_successful_run_id, now_str, now_str)
        if conn is not None:
            conn.execute(sql, params)
            if legacy_imported is not None:
                self.record_portal_migration(portal_id, legacy_imported, conn=conn)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)
                if legacy_imported is not None:
                    self.record_portal_migration(portal_id, legacy_imported, conn=c)

    def record_portal_migration(
        self,
        portal_id: str,
        imported: bool = True,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Explicitly record in portals table that legacy catalog has been imported for portal."""
        # We record this in portals by setting updated_at or by using run_checkpoints/metadata
        # A clean, schema-safe way: record in run_checkpoints with checkpoint_name='__migration_complete__'
        # run_checkpoints has (portal_id, run_id, checkpoint_name)
        # Or store in run_checkpoints under a special dummy run '__portal__'
        # Even simpler: ensure a checkpoint or a special run_checkpoints row
        sql = """
        INSERT OR REPLACE INTO run_checkpoints (portal_id, run_id, checkpoint_name, checkpoint_timestamp, state_snapshot_json)
        VALUES (?, '__system__', 'legacy_migration_complete', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), '{"migrated": true}');
        """
        # Ensure __system__ run exists to satisfy foreign key
        now_str = _utc_now_iso()
        run_sql = """
        INSERT OR IGNORE INTO runs (portal_id, run_id, started_at, status, run_type)
        VALUES (?, '__system__', ?, 'COMPLETED', 'system');
        """
        if conn is not None:
            conn.execute(run_sql, (portal_id, now_str))
            conn.execute(sql, (portal_id,))
        else:
            with self.db.transaction() as c:
                c.execute(run_sql, (portal_id, now_str))
                c.execute(sql, (portal_id,))

    def is_portal_migrated(self, portal_id: str) -> bool:
        """Check if portal has been explicitly recorded as migrated."""
        conn = self.db.connect()
        row = conn.execute(
            "SELECT 1 FROM run_checkpoints WHERE portal_id = ? AND run_id = '__system__' AND checkpoint_name = 'legacy_migration_complete';",
            (portal_id,)
        ).fetchone()
        return bool(row)
        return bool(row)


    def get_portal(self, portal_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve portal metadata dictionary by portal_id."""
        conn = self.db.connect()
        row = conn.execute("SELECT * FROM portals WHERE portal_id = ?;", (portal_id,)).fetchone()
        return dict(row) if row else None

    def upsert_run(
        self,
        portal_id: str,
        run_id: str,
        started_at: str,
        status: str,
        completed_at: Optional[str] = None,
        articles_discovered: int = 0,
        articles_downloaded: int = 0,
        articles_extracted: int = 0,
        warc_records: int = 0,
        run_type: str = "incremental",
        seed_urls: Optional[List[str]] = None,
        archive_page_count: int = 0,
        pages_revisited: int = 0,
        scope_completion: str = "INCOMPLETE",
        full_portal_coverage: bool = False,
        head_changed: bool = False,
        new_articles_count: int = 0,
        changed_articles_count: int = 0,
        unchanged_articles_count: int = 0,
        terminal_reason: Optional[str] = None,
        last_checkpoint_timestamp: Optional[str] = None,
        interrupted: bool = False,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update a run record."""
        now_str = _utc_now_iso()
        seed_json = json.dumps(seed_urls or [], ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None else None

        sql = """
        INSERT INTO runs (
            portal_id, run_id, started_at, completed_at, status,
            articles_discovered, articles_downloaded, articles_extracted, warc_records,
            run_type, seed_urls_json, archive_page_count, pages_revisited,
            scope_completion, full_portal_coverage, head_changed,
            new_articles_count, changed_articles_count, unchanged_articles_count,
            terminal_reason, last_checkpoint_timestamp, interrupted, error_message,
            metadata_json, created_at
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?
        )
        ON CONFLICT(portal_id, run_id) DO UPDATE SET
            completed_at = coalesce(excluded.completed_at, runs.completed_at),
            status = excluded.status,
            articles_discovered = excluded.articles_discovered,
            articles_downloaded = excluded.articles_downloaded,
            articles_extracted = excluded.articles_extracted,
            warc_records = excluded.warc_records,
            run_type = excluded.run_type,
            seed_urls_json = excluded.seed_urls_json,
            archive_page_count = excluded.archive_page_count,
            pages_revisited = excluded.pages_revisited,
            scope_completion = excluded.scope_completion,
            full_portal_coverage = excluded.full_portal_coverage,
            head_changed = excluded.head_changed,
            new_articles_count = excluded.new_articles_count,
            changed_articles_count = excluded.changed_articles_count,
            unchanged_articles_count = excluded.unchanged_articles_count,
            terminal_reason = excluded.terminal_reason,
            last_checkpoint_timestamp = excluded.last_checkpoint_timestamp,
            interrupted = excluded.interrupted,
            error_message = excluded.error_message,
            metadata_json = excluded.metadata_json;
        """
        params = (
            portal_id, run_id, started_at, completed_at, status,
            articles_discovered, articles_downloaded, articles_extracted, warc_records,
            run_type, seed_json, archive_page_count, pages_revisited,
            scope_completion, 1 if full_portal_coverage else 0, 1 if head_changed else 0,
            new_articles_count, changed_articles_count, unchanged_articles_count,
            terminal_reason, last_checkpoint_timestamp, 1 if interrupted else 0, error_message,
            meta_json, now_str
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def get_run(self, portal_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve run row by (portal_id, run_id)."""
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM runs WHERE portal_id = ? AND run_id = ?;",
            (portal_id, run_id)
        ).fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Discovered URLs & URL Observations
    # =========================================================================

    def upsert_discovered_url(
        self,
        portal_id: str,
        url_hash: str,
        normalized_url: str,
        first_seen_run_id: str,
        first_seen_at: str,
        last_seen_run_id: str,
        last_seen_at: str,
        http_status: Optional[int] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_hash: Optional[str] = None,
        observation_count: int = 1,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update a discovered URL entry."""
        sql = """
        INSERT INTO discovered_urls (
            portal_id, url_hash, normalized_url, first_seen_run_id, first_seen_at,
            last_seen_run_id, last_seen_at, http_status, etag, last_modified,
            content_hash, observation_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, url_hash) DO UPDATE SET
            last_seen_run_id = excluded.last_seen_run_id,
            last_seen_at = excluded.last_seen_at,
            http_status = coalesce(excluded.http_status, discovered_urls.http_status),
            etag = coalesce(excluded.etag, discovered_urls.etag),
            last_modified = coalesce(excluded.last_modified, discovered_urls.last_modified),
            content_hash = coalesce(excluded.content_hash, discovered_urls.content_hash),
            observation_count = discovered_urls.observation_count + 1;
        """
        params = (
            portal_id, url_hash, normalized_url, first_seen_run_id, first_seen_at,
            last_seen_run_id, last_seen_at, http_status, etag, last_modified,
            content_hash, observation_count
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def get_discovered_url(self, portal_id: str, url_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve discovered URL record."""
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM discovered_urls WHERE portal_id = ? AND url_hash = ?;",
            (portal_id, url_hash)
        ).fetchone()
        return dict(row) if row else None

    def add_url_observation(
        self,
        portal_id: str,
        url_hash: str,
        run_id: str,
        observed_at: str,
        http_status: Optional[int] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_hash: Optional[str] = None,
        content_length: Optional[int] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Record an observation of a URL during a specific run."""
        sql = """
        INSERT INTO url_observations (
            portal_id, url_hash, run_id, observed_at, http_status, etag,
            last_modified, content_hash, content_length
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, url_hash, run_id) DO UPDATE SET
            observed_at = excluded.observed_at,
            http_status = excluded.http_status,
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            content_hash = excluded.content_hash,
            content_length = excluded.content_length;
        """
        params = (portal_id, url_hash, run_id, observed_at, http_status, etag, last_modified, content_hash, content_length)
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    # =========================================================================
    # Portal States, Archive Pages & Article States
    # =========================================================================

    def save_portal_state_snapshot(
        self,
        portal_id: str,
        run_id: str,
        state_timestamp: str,
        last_successful_run: Optional[str] = None,
        last_run_timestamp: Optional[str] = None,
        total_runs_count: int = 0,
        total_articles_count: int = 0,
        total_archive_pages_count: int = 0,
        active_rules_count: int = 0,
        source_manifest: Optional[Dict[str, Any]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Save a portal state snapshot."""
        manifest_json = json.dumps(source_manifest or {}, ensure_ascii=False)
        sql = """
        INSERT INTO portal_states (
            portal_id, run_id, state_timestamp, last_successful_run, last_run_timestamp,
            total_runs_count, total_articles_count, total_archive_pages_count,
            active_rules_count, source_manifest_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, run_id) DO UPDATE SET
            state_timestamp = excluded.state_timestamp,
            last_successful_run = excluded.last_successful_run,
            last_run_timestamp = excluded.last_run_timestamp,
            total_runs_count = excluded.total_runs_count,
            total_articles_count = excluded.total_articles_count,
            total_archive_pages_count = excluded.total_archive_pages_count,
            active_rules_count = excluded.active_rules_count,
            source_manifest_json = excluded.source_manifest_json;
        """
        params = (
            portal_id, run_id, state_timestamp, last_successful_run, last_run_timestamp,
            total_runs_count, total_articles_count, total_archive_pages_count,
            active_rules_count, manifest_json
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def get_latest_portal_state(self, portal_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve most recent portal state snapshot for a portal."""
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT * FROM portal_states
            WHERE portal_id = ?
            ORDER BY state_timestamp DESC
            LIMIT 1;
            """,
            (portal_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_archive_page_observation(
        self,
        portal_id: str,
        run_id: str,
        page_url: str,
        page_number: int,
        observed_at: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_hash: Optional[str] = None,
        discovered_article_count: int = 0,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update an archive page observation."""
        sql = """
        INSERT INTO archive_page_observations (
            portal_id, run_id, page_url, page_number, etag, last_modified,
            content_hash, discovered_article_count, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, run_id, page_url) DO UPDATE SET
            page_number = excluded.page_number,
            etag = excluded.etag,
            last_modified = excluded.last_modified,
            content_hash = excluded.content_hash,
            discovered_article_count = excluded.discovered_article_count,
            observed_at = excluded.observed_at;
        """
        params = (
            portal_id, run_id, page_url, page_number, etag, last_modified,
            content_hash, discovered_article_count, observed_at
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def upsert_article_state(
        self,
        portal_id: str,
        url_hash: str,
        article_url: str,
        first_discovered_run_id: str,
        first_discovered_at: str,
        last_extracted_run_id: str,
        last_extracted_at: str,
        extraction_status: str,
        warc_record_id: Optional[str] = None,
        warc_path: Optional[str] = None,
        content_sha256: Optional[str] = None,
        title: Optional[str] = None,
        published_date: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update an article state entry."""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None else None
        sql = """
        INSERT INTO article_states (
            portal_id, url_hash, article_url, first_discovered_run_id, first_discovered_at,
            last_extracted_run_id, last_extracted_at, extraction_status, warc_record_id,
            warc_path, content_sha256, title, published_date, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, url_hash) DO UPDATE SET
            last_extracted_run_id = excluded.last_extracted_run_id,
            last_extracted_at = excluded.last_extracted_at,
            extraction_status = excluded.extraction_status,
            warc_record_id = coalesce(excluded.warc_record_id, article_states.warc_record_id),
            warc_path = coalesce(excluded.warc_path, article_states.warc_path),
            content_sha256 = coalesce(excluded.content_sha256, article_states.content_sha256),
            title = coalesce(excluded.title, article_states.title),
            published_date = coalesce(excluded.published_date, article_states.published_date),
            metadata_json = coalesce(excluded.metadata_json, article_states.metadata_json);
        """
        params = (
            portal_id, url_hash, article_url, first_discovered_run_id, first_discovered_at,
            last_extracted_run_id, last_extracted_at, extraction_status, warc_record_id,
            warc_path, content_sha256, title, published_date, meta_json
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def get_article_state(self, portal_id: str, url_hash: str) -> Optional[Dict[str, Any]]:
        """Retrieve an article state record."""
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM article_states WHERE portal_id = ? AND url_hash = ?;",
            (portal_id, url_hash)
        ).fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Packages & Deposits
    # =========================================================================

    def upsert_package(
        self,
        portal_id: str,
        package_id: str,
        package_path: str,
        total_articles: int = 0,
        warc_count: int = 0,
        total_bytes: int = 0,
        checksum_sha256: Optional[str] = None,
        manifest_path: Optional[str] = None,
        package_status: str = "CREATED",
        sealed_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update an archive package record."""
        sql = """
        INSERT INTO packages (
            portal_id, package_id, package_path, total_articles, warc_count,
            total_bytes, checksum_sha256, manifest_path, package_status, sealed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, package_id) DO UPDATE SET
            package_path = excluded.package_path,
            total_articles = excluded.total_articles,
            warc_count = excluded.warc_count,
            total_bytes = excluded.total_bytes,
            checksum_sha256 = excluded.checksum_sha256,
            manifest_path = excluded.manifest_path,
            package_status = excluded.package_status,
            sealed_at = excluded.sealed_at;
        """
        params = (
            portal_id, package_id, package_path, total_articles, warc_count,
            total_bytes, checksum_sha256, manifest_path, package_status, sealed_at
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def upsert_deposit_record(
        self,
        portal_id: str,
        deposit_id: str,
        package_id: str,
        target_repository: str,
        status: str,
        submitted_at: str,
        deposition_id: Optional[str] = None,
        doi: Optional[str] = None,
        concept_doi: Optional[str] = None,
        badge_url: Optional[str] = None,
        completed_at: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update a deposit record."""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False) if metadata is not None else None
        sql = """
        INSERT INTO deposit_records (
            portal_id, deposit_id, package_id, target_repository, deposition_id,
            status, doi, concept_doi, badge_url, submitted_at, completed_at,
            error_message, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, deposit_id) DO UPDATE SET
            status = excluded.status,
            deposition_id = coalesce(excluded.deposition_id, deposit_records.deposition_id),
            doi = coalesce(excluded.doi, deposit_records.doi),
            concept_doi = coalesce(excluded.concept_doi, deposit_records.concept_doi),
            badge_url = coalesce(excluded.badge_url, deposit_records.badge_url),
            completed_at = coalesce(excluded.completed_at, deposit_records.completed_at),
            error_message = excluded.error_message,
            metadata_json = coalesce(excluded.metadata_json, deposit_records.metadata_json);
        """
        params = (
            portal_id, deposit_id, package_id, target_repository, deposition_id,
            status, doi, concept_doi, badge_url, submitted_at, completed_at,
            error_message, meta_json
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)


    def upsert_deposit_artifact(
        self,
        portal_id: str,
        deposit_id: str,
        artifact_filename: str,
        file_size_bytes: int,
        sha256_checksum: str,
        upload_status: str,
        remote_file_id: Optional[str] = None,
        uploaded_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Insert or update a deposit artifact record."""
        sql = """
        INSERT INTO deposit_artifacts (
            portal_id, deposit_id, artifact_filename, file_size_bytes, sha256_checksum,
            remote_file_id, upload_status, uploaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal_id, deposit_id, artifact_filename) DO UPDATE SET
            file_size_bytes = excluded.file_size_bytes,
            sha256_checksum = excluded.sha256_checksum,
            remote_file_id = coalesce(excluded.remote_file_id, deposit_artifacts.remote_file_id),
            upload_status = excluded.upload_status,
            uploaded_at = coalesce(excluded.uploaded_at, deposit_artifacts.uploaded_at);
        """
        params = (
            portal_id, deposit_id, artifact_filename, file_size_bytes, sha256_checksum,
            remote_file_id, upload_status, uploaded_at
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)

    def get_deposit_record(self, portal_id: str, deposit_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a deposit record by (portal_id, deposit_id)."""
        conn = self.db.connect()
        row = conn.execute(
            "SELECT * FROM deposit_records WHERE portal_id = ? AND deposit_id = ?;",
            (portal_id, deposit_id)
        ).fetchone()
        return dict(row) if row else None

    def get_deposit_artifacts(self, portal_id: str, deposit_id: str) -> List[Dict[str, Any]]:
        """Retrieve all deposit artifact records for a deposit."""
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT * FROM deposit_artifacts WHERE portal_id = ? AND deposit_id = ? ORDER BY artifact_filename ASC;",
            (portal_id, deposit_id)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_deposit_records(self, portal_id: str) -> List[Dict[str, Any]]:
        """List all deposit records for a portal."""
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT * FROM deposit_records WHERE portal_id = ? ORDER BY submitted_at ASC;",
            (portal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # Interrupted-Run Recovery
    # =========================================================================

    def get_interrupted_runs(self, portal_id: str) -> List[Dict[str, Any]]:
        """
        Return all runs for a portal that are in a non-terminal state (RUNNING or FINALIZING).

        These represent runs that were registered but never reached a terminal state
        (CONVERGED, FAILED, BUDGET_EXHAUSTED, MANUAL_REVIEW_REQUIRED, UNCHANGED, etc.).
        A RUNNING run from a previous process indicates an interrupted execution.
        """
        conn = self.db.connect()
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE portal_id = ?
              AND status IN ('RUNNING', 'FINALIZING')
            ORDER BY started_at ASC;
            """,
            (portal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_run_interrupted(
        self,
        portal_id: str,
        run_id: str,
        completed_at: Optional[str] = None,
        error_message: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        """
        Transition a RUNNING run to INTERRUPTED status.

        This records that the run was detected as orphaned on startup — it was registered
        but the process was killed before it could reach a terminal state. The run record
        and all committed crawl state (URLs, article states, archive observations) are
        preserved. The run is never promoted to successful.
        """
        now_str = completed_at or _utc_now_iso()
        err = error_message or "Run interrupted: process exited before terminal state was recorded."
        sql = """
        UPDATE runs
        SET status = 'INTERRUPTED',
            completed_at = coalesce(completed_at, ?),
            interrupted = 1,
            error_message = coalesce(error_message, ?)
        WHERE portal_id = ? AND run_id = ? AND status = 'RUNNING';
        """
        params = (now_str, err, portal_id, run_id)
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.db.transaction() as c:
                c.execute(sql, params)

    def recover_finalizing_run(
        self,
        portal_id: str,
        run_id: str,
        results_base_dir: str,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """
        Recover a run left in FINALIZING state.
        Inspects whether the package was finalized and verified.
        If a valid package exists in packages/<portal_id>/runs/<run_id>, promotes it to CONVERGED
        and updates the portal's latest_successful_run_id.
        If invalid, corrupt, or missing, marks the run as FAILED or MANUAL_REVIEW_REQUIRED.
        Returns the resolved status string ('CONVERGED', 'FAILED', or 'MANUAL_REVIEW_REQUIRED').
        """
        from pathlib import Path
        import hashlib
        from src.archive_package import PackageVerifier

        pkg_dir = Path(results_base_dir) / "packages" / portal_id / "runs" / run_id
        now_str = _utc_now_iso()

        if pkg_dir.exists() and (pkg_dir / "checksums.sha256").exists():
            verifier_fn = getattr(PackageVerifier, "verify", PackageVerifier.verify_package)
            try:
                verification = verifier_fn(str(pkg_dir))
                if verification.is_valid:
                    chk_file = pkg_dir / "checksums.sha256"
                    pkg_checksum = hashlib.sha256(chk_file.read_bytes()).hexdigest()

                    # Complete package and run finalization
                    def _finalize(c: sqlite3.Connection):
                        self.upsert_package(
                            portal_id=portal_id,
                            package_id=run_id,
                            package_path=str(pkg_dir),
                            checksum_sha256=pkg_checksum,
                            manifest_path=str(pkg_dir / "manifest.json"),
                            package_status="SEALED",
                            sealed_at=now_str,
                            conn=c,
                        )
                        c.execute(
                            """
                            UPDATE runs
                            SET status = 'CONVERGED',
                                completed_at = coalesce(completed_at, ?),
                                error_message = NULL
                            WHERE portal_id = ? AND run_id = ?;
                            """,
                            (now_str, portal_id, run_id)
                        )
                        c.execute(
                            """
                            UPDATE portals
                            SET latest_successful_run_id = ?,
                                updated_at = ?
                            WHERE portal_id = ?;
                            """,
                            (run_id, now_str, portal_id)
                        )

                    if conn is not None:
                        _finalize(conn)
                    else:
                        with self.db.transaction() as c:
                            _finalize(c)
                    return "CONVERGED"
                else:
                    err = f"FINALIZING recovery: package verification failed ({verification.checksum_mismatches or verification.missing_files})"
                    self.upsert_run(
                        portal_id=portal_id,
                        run_id=run_id,
                        started_at=now_str,
                        completed_at=now_str,
                        status="FAILED",
                        error_message=err,
                        conn=conn,
                    )
                    return "FAILED"
            except Exception as e:
                err = f"FINALIZING recovery error: {e}"
                self.upsert_run(
                    portal_id=portal_id,
                    run_id=run_id,
                    started_at=now_str,
                    completed_at=now_str,
                    status="MANUAL_REVIEW_REQUIRED",
                    error_message=err,
                    conn=conn,
                )
                return "MANUAL_REVIEW_REQUIRED"
        else:
            err = "FINALIZING recovery: finalized package directory or checksums not found."
            self.upsert_run(
                portal_id=portal_id,
                run_id=run_id,
                started_at=now_str,
                completed_at=now_str,
                status="FAILED",
                error_message=err,
                conn=conn,
            )
            return "FAILED"

    def list_runs(
        self,
        portal_id: str,
        statuses: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List runs for a portal, optionally filtered by status, newest first."""
        conn = self.db.connect()
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            sql = f"""
            SELECT * FROM runs
            WHERE portal_id = ? AND status IN ({placeholders})
            ORDER BY started_at DESC LIMIT ?;
            """
            rows = conn.execute(sql, [portal_id] + statuses + [limit]).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs WHERE portal_id = ? ORDER BY started_at DESC LIMIT ?;",
                (portal_id, limit)
            ).fetchall()
        return [dict(r) for r in rows]

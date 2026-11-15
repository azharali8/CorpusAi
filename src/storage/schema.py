"""
CorpusAI SQLite Storage Schema & Migration DDL Definitions.

Defines the relational tables, indices, triggers, and migrations for operational state:
- schema_migrations
- portals
- runs
- run_checkpoints
- discovered_urls
- url_observations
- portal_states
- archive_page_observations
- article_states
- packages
- deposit_records
- deposit_artifacts
- outbox_events
"""

from typing import List, Tuple

# Current schema version supported by this codebase
LATEST_SCHEMA_VERSION = 1

# V1 DDL Statements in strict dependency order
V1_SCHEMA_STATEMENTS: List[str] = [
    # 1. Schema Migrations table
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        description TEXT NOT NULL
    );
    """,

    # 2. Portals table
    """
    CREATE TABLE IF NOT EXISTS portals (
        portal_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        parser_type TEXT NOT NULL,
        latest_successful_run_id TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    );
    """,

    # 3. Runs table
    """
    CREATE TABLE IF NOT EXISTS runs (
        portal_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL,
        articles_discovered INTEGER NOT NULL DEFAULT 0,
        articles_downloaded INTEGER NOT NULL DEFAULT 0,
        articles_extracted INTEGER NOT NULL DEFAULT 0,
        warc_records INTEGER NOT NULL DEFAULT 0,
        run_type TEXT NOT NULL DEFAULT 'incremental',
        seed_urls_json TEXT NOT NULL DEFAULT '[]',
        archive_page_count INTEGER NOT NULL DEFAULT 0,
        pages_revisited INTEGER NOT NULL DEFAULT 0,
        scope_completion TEXT NOT NULL DEFAULT 'INCOMPLETE',
        full_portal_coverage INTEGER NOT NULL DEFAULT 0,
        head_changed INTEGER NOT NULL DEFAULT 0,
        new_articles_count INTEGER NOT NULL DEFAULT 0,
        changed_articles_count INTEGER NOT NULL DEFAULT 0,
        unchanged_articles_count INTEGER NOT NULL DEFAULT 0,
        terminal_reason TEXT,
        last_checkpoint_timestamp TEXT,
        interrupted INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (portal_id, run_id),
        FOREIGN KEY (portal_id) REFERENCES portals(portal_id) ON DELETE CASCADE
    );
    """,

    # Indices on runs
    """
    CREATE INDEX IF NOT EXISTS idx_runs_portal_status ON runs(portal_id, status);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
    """,

    # 4. Run Checkpoints table
    """
    CREATE TABLE IF NOT EXISTS run_checkpoints (
        portal_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        checkpoint_name TEXT NOT NULL,
        checkpoint_timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        state_snapshot_json TEXT NOT NULL,
        PRIMARY KEY (portal_id, run_id, checkpoint_name),
        FOREIGN KEY (portal_id, run_id) REFERENCES runs(portal_id, run_id) ON DELETE CASCADE
    );
    """,

    # 5. Discovered URLs table
    """
    CREATE TABLE IF NOT EXISTS discovered_urls (
        portal_id TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        normalized_url TEXT NOT NULL,
        first_seen_run_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_run_id TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        http_status INTEGER,
        etag TEXT,
        last_modified TEXT,
        content_hash TEXT,
        observation_count INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (portal_id, url_hash),
        FOREIGN KEY (portal_id) REFERENCES portals(portal_id) ON DELETE CASCADE,
        FOREIGN KEY (portal_id, first_seen_run_id) REFERENCES runs(portal_id, run_id) ON DELETE RESTRICT,
        FOREIGN KEY (portal_id, last_seen_run_id) REFERENCES runs(portal_id, run_id) ON DELETE RESTRICT
    );
    """,

    # Indices on discovered_urls
    """
    CREATE INDEX IF NOT EXISTS idx_discovered_urls_lookup ON discovered_urls(portal_id, normalized_url);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_discovered_urls_first_seen ON discovered_urls(portal_id, first_seen_run_id);
    """,

    # 6. URL Observations table
    """
    CREATE TABLE IF NOT EXISTS url_observations (
        portal_id TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        run_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        http_status INTEGER,
        etag TEXT,
        last_modified TEXT,
        content_hash TEXT,
        content_length INTEGER,
        PRIMARY KEY (portal_id, url_hash, run_id),
        FOREIGN KEY (portal_id, url_hash) REFERENCES discovered_urls(portal_id, url_hash) ON DELETE CASCADE,
        FOREIGN KEY (portal_id, run_id) REFERENCES runs(portal_id, run_id) ON DELETE CASCADE
    );
    """,

    # 7. Portal States table
    """
    CREATE TABLE IF NOT EXISTS portal_states (
        portal_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        state_timestamp TEXT NOT NULL,
        last_successful_run TEXT,
        last_run_timestamp TEXT,
        total_runs_count INTEGER NOT NULL DEFAULT 0,
        total_articles_count INTEGER NOT NULL DEFAULT 0,
        total_archive_pages_count INTEGER NOT NULL DEFAULT 0,
        active_rules_count INTEGER NOT NULL DEFAULT 0,
        source_manifest_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (portal_id, run_id),
        FOREIGN KEY (portal_id, run_id) REFERENCES runs(portal_id, run_id) ON DELETE CASCADE
    );
    """,

    # 8. Archive Page Observations table
    """
    CREATE TABLE IF NOT EXISTS archive_page_observations (
        portal_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        page_url TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        etag TEXT,
        last_modified TEXT,
        content_hash TEXT,
        discovered_article_count INTEGER NOT NULL DEFAULT 0,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (portal_id, run_id, page_url),
        FOREIGN KEY (portal_id, run_id) REFERENCES runs(portal_id, run_id) ON DELETE CASCADE
    );
    """,

    # 9. Article States table
    """
    CREATE TABLE IF NOT EXISTS article_states (
        portal_id TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        article_url TEXT NOT NULL,
        first_discovered_run_id TEXT NOT NULL,
        first_discovered_at TEXT NOT NULL,
        last_extracted_run_id TEXT NOT NULL,
        last_extracted_at TEXT NOT NULL,
        extraction_status TEXT NOT NULL,
        warc_record_id TEXT,
        warc_path TEXT,
        content_sha256 TEXT,
        title TEXT,
        published_date TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (portal_id, url_hash),
        FOREIGN KEY (portal_id) REFERENCES portals(portal_id) ON DELETE CASCADE,
        FOREIGN KEY (portal_id, first_discovered_run_id) REFERENCES runs(portal_id, run_id) ON DELETE RESTRICT,
        FOREIGN KEY (portal_id, last_extracted_run_id) REFERENCES runs(portal_id, run_id) ON DELETE RESTRICT
    );
    """,

    # 10. Packages table
    """
    CREATE TABLE IF NOT EXISTS packages (
        portal_id TEXT NOT NULL,
        package_id TEXT NOT NULL,
        package_path TEXT NOT NULL,
        total_articles INTEGER NOT NULL DEFAULT 0,
        warc_count INTEGER NOT NULL DEFAULT 0,
        total_bytes INTEGER NOT NULL DEFAULT 0,
        checksum_sha256 TEXT,
        manifest_path TEXT,
        package_status TEXT NOT NULL DEFAULT 'CREATED',
        sealed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (portal_id, package_id),
        FOREIGN KEY (portal_id) REFERENCES portals(portal_id) ON DELETE CASCADE
    );
    """,

    # 11. Deposit Records table
    """
    CREATE TABLE IF NOT EXISTS deposit_records (
        portal_id TEXT NOT NULL,
        deposit_id TEXT NOT NULL,
        package_id TEXT NOT NULL,
        target_repository TEXT NOT NULL,
        deposition_id TEXT,
        status TEXT NOT NULL,
        doi TEXT,
        concept_doi TEXT,
        badge_url TEXT,
        submitted_at TEXT NOT NULL,
        completed_at TEXT,
        error_message TEXT,
        metadata_json TEXT,
        PRIMARY KEY (portal_id, deposit_id),
        FOREIGN KEY (portal_id, package_id) REFERENCES packages(portal_id, package_id) ON DELETE CASCADE
    );
    """,

    # 12. Deposit Artifacts table
    """
    CREATE TABLE IF NOT EXISTS deposit_artifacts (
        portal_id TEXT NOT NULL,
        deposit_id TEXT NOT NULL,
        artifact_filename TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL,
        sha256_checksum TEXT NOT NULL,
        remote_file_id TEXT,
        upload_status TEXT NOT NULL,
        uploaded_at TEXT,
        PRIMARY KEY (portal_id, deposit_id, artifact_filename),
        FOREIGN KEY (portal_id, deposit_id) REFERENCES deposit_records(portal_id, deposit_id) ON DELETE CASCADE
    );
    """,

    # 13. Outbox Events table
    """
    CREATE TABLE IF NOT EXISTS outbox_events (
        event_id TEXT PRIMARY KEY,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        last_attempt_at TEXT,
        next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        lease_token TEXT,
        lease_expires_at TEXT,
        error_message TEXT,
        processed_at TEXT
    );
    """,

    # Indices on outbox_events
    """
    CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_events(status, next_attempt_at)
    WHERE status IN ('PENDING', 'RETRY');
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_outbox_lease ON outbox_events(status, lease_expires_at)
    WHERE status = 'IN_FLIGHT';
    """,

    # 14. Triggers for Portals.latest_successful_run_id Validation
    """
    CREATE TRIGGER IF NOT EXISTS trg_portals_latest_run_validate_insert
    BEFORE INSERT ON portals
    FOR EACH ROW
    WHEN NEW.latest_successful_run_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'Referenced latest_successful_run_id must belong to same portal and have successful status')
        WHERE NOT EXISTS (
            SELECT 1 FROM runs
            WHERE runs.portal_id = NEW.portal_id
              AND runs.run_id = NEW.latest_successful_run_id
              AND runs.status IN ('CONVERGED', 'COMPLETED', 'PILOT_SCOPE_CONVERGED', 'MULTIPAGE_PILOT_COMPLETE')
        );
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_portals_latest_run_validate_update
    BEFORE UPDATE OF latest_successful_run_id, portal_id ON portals
    FOR EACH ROW
    WHEN NEW.latest_successful_run_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'Referenced latest_successful_run_id must belong to same portal and have successful status')
        WHERE NOT EXISTS (
            SELECT 1 FROM runs
            WHERE runs.portal_id = NEW.portal_id
              AND runs.run_id = NEW.latest_successful_run_id
              AND runs.status IN ('CONVERGED', 'COMPLETED', 'PILOT_SCOPE_CONVERGED', 'MULTIPAGE_PILOT_COMPLETE')
        );
    END;
    """,

    # 15. Trigger to prevent invalidating successful status of a referenced run
    """
    CREATE TRIGGER IF NOT EXISTS trg_runs_prevent_successful_status_invalidation
    BEFORE UPDATE OF status ON runs
    FOR EACH ROW
    WHEN OLD.status IN ('CONVERGED', 'COMPLETED', 'PILOT_SCOPE_CONVERGED', 'MULTIPAGE_PILOT_COMPLETE')
     AND NEW.status NOT IN ('CONVERGED', 'COMPLETED', 'PILOT_SCOPE_CONVERGED', 'MULTIPAGE_PILOT_COMPLETE')
    BEGIN
        SELECT RAISE(ABORT, 'Cannot change run status away from successful while referenced as portal latest_successful_run_id')
        WHERE EXISTS (
            SELECT 1 FROM portals
            WHERE portals.portal_id = OLD.portal_id
              AND portals.latest_successful_run_id = OLD.run_id
        );
    END;
    """
]

# Migration runner tuples: (version, description, statements)
MIGRATIONS: List[Tuple[int, str, List[str]]] = [
    (1, "Initial schema v1: relational operational state, composite keys, outbox, and validation triggers", V1_SCHEMA_STATEMENTS),
]

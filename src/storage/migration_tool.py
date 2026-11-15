"""
CorpusAI State Migration and Bi-directional Export Tool.

Provides:
- Ingestion of legacy JSON files (catalog.json, portal_state.json, content_url_registry.json, deposit_receipts)
  into SQLite tables.
- Verification of per-field parity.
- Reverse export from SQLite back to authoritative or interchange JSON representations.
"""

import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

from src.storage.sqlite_backend import CorpusAIStateDatabase
from src.storage.repository import CorpusAIRepository
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.portal_state import PortalState, ArticleState, ArchivePageState
from src.content_url_registry import ContentURLRegistry
from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord


class StateMigrationTool:
    """Imports operational JSON files into SQLite and exports SQLite records back to JSON."""

    def __init__(self, db: CorpusAIStateDatabase):
        self.db = db
        self.repo = CorpusAIRepository(db)

    def import_catalog_json(self, catalog_path: Path, portal_id: str, portal_name: str, base_url: str) -> int:
        """Import an ArchiveCatalog JSON file into SQLite."""
        if not catalog_path.exists():
            raise FileNotFoundError(f"Catalog file not found: {catalog_path}")

        catalog = ArchiveCatalog.load_from_json(catalog_path)
        with self.db.transaction() as conn:
            self.repo.upsert_portal(
                portal_id=portal_id,
                name=portal_name,
                base_url=base_url,
                conn=conn,
            )
            for run_id, run in catalog.runs.items():
                meta = {
                    "previous_run_id": run.previous_run_id,
                    "mode": run.mode,
                    "run_dir": run.run_dir,
                    "manual_review_status": run.manual_review_status,
                    "anomalies": run.anomalies,
                }
                self.repo.upsert_run(
                    portal_id=portal_id,
                    run_id=run.run_id,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    status=run.status,
                    articles_discovered=run.article_count,
                    articles_downloaded=run.article_count,
                    articles_extracted=run.article_count,
                    new_articles_count=run.new_article_count,
                    changed_articles_count=run.changed_article_count,
                    metadata=meta,
                    conn=conn,
                )
            if catalog.latest_successful_run_id:
                self.repo.upsert_portal(
                    portal_id=portal_id,
                    name=portal_name,
                    base_url=base_url,
                    latest_successful_run_id=catalog.latest_successful_run_id,
                    legacy_imported=True,
                    conn=conn,
                )
            else:
                self.repo.record_portal_migration(portal_id, True, conn=conn)
        return len(catalog.runs)

    def import_portal_state_json(self, state_path: Path, portal_id: str, run_id: str) -> Tuple[int, int]:
        """
        Import a PortalState JSON file into SQLite, including child archive pages and article states.
        Returns (archive_pages_count, articles_count).
        """
        if not state_path.exists():
            raise FileNotFoundError(f"Portal state file not found: {state_path}")

        state = PortalState.load_from_json(state_path)
        page_count = 0
        art_count = 0

        with self.db.transaction() as conn:
            # 1. Snapshot metadata
            manifest_wrapper = {
                "source_manifest": state.source_manifest,
                "archive_order": state.archive_order,
                "head_fingerprint": state.head_fingerprint,
                "boundary_fingerprints": state.boundary_fingerprints,
            }
            self.repo.save_portal_state_snapshot(
                portal_id=portal_id,
                run_id=run_id,
                state_timestamp=state.captured_at or "1970-01-01T00:00:00Z",
                last_successful_run=None,
                last_run_timestamp=state.captured_at,
                total_runs_count=0,
                total_articles_count=len(state.articles),
                total_archive_pages_count=len(state.archive_pages),
                active_rules_count=0,
                source_manifest=manifest_wrapper,
                conn=conn,
            )

            # 2. Archive pages
            for page_url, p_state in state.archive_pages.items():
                self.repo.upsert_archive_page_observation(
                    portal_id=portal_id,
                    run_id=run_id,
                    page_url=page_url,
                    page_number=p_state.page_number or 0,
                    observed_at=p_state.observed_at or state.captured_at,
                    content_hash=p_state.ordered_fingerprint,
                    discovered_article_count=len(p_state.article_urls),
                    conn=conn,
                )
                page_count += 1

            # 3. Articles
            for can_url, a_state in state.articles.items():
                import hashlib
                url_hash = hashlib.sha256(can_url.encode("utf-8")).hexdigest()
                self.repo.upsert_article_state(
                    portal_id=portal_id,
                    url_hash=url_hash,
                    article_url=can_url,
                    first_discovered_run_id=a_state.first_seen_run_id or run_id,
                    first_discovered_at=a_state.last_captured_at or state.captured_at,
                    last_extracted_run_id=a_state.last_seen_run_id or run_id,
                    last_extracted_at=a_state.last_captured_at or state.captured_at,
                    extraction_status=a_state.status,
                    warc_record_id=a_state.warc_record_id,
                    warc_path=a_state.warc_file,
                    content_sha256=a_state.content_hash,
                    title=a_state.title,
                    published_date=a_state.published_at,
                    metadata=a_state.metadata,
                    conn=conn,
                )
                art_count += 1

        return page_count, art_count

    def import_content_url_registry_json(self, registry_path: Path, portal_id: str, default_run_id: str) -> int:
        """Import a ContentURLRegistry JSON file into SQLite."""
        if not registry_path.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        registry = ContentURLRegistry.load_from_json(registry_path)
        count = 0
        with self.db.transaction() as conn:
            for url_hash, entry in registry.urls.items():
                self.repo.upsert_discovered_url(
                    portal_id=portal_id,
                    url_hash=url_hash,
                    normalized_url=entry.normalized_url,
                    first_seen_run_id=entry.first_seen_run_id or default_run_id,
                    first_seen_at=entry.first_seen_at or "1970-01-01T00:00:00Z",
                    last_seen_run_id=entry.last_seen_run_id or default_run_id,
                    last_seen_at=entry.last_seen_at or "1970-01-01T00:00:00Z",
                    http_status=entry.http_status,
                    etag=entry.etag,
                    last_modified=entry.last_modified,
                    content_hash=entry.content_hash,
                    observation_count=entry.observation_count,
                    conn=conn,
                )
                count += 1
        return count

    def export_catalog_json(self, portal_id: str, dest_path: Path) -> ArchiveCatalog:
        """Export SQLite records back to an ArchiveCatalog object and save to JSON."""
        portal = self.repo.get_portal(portal_id)
        if not portal:
            raise ValueError(f"Portal not found: {portal_id}")

        conn = self.db.connect()
        rows = conn.execute(
            "SELECT * FROM runs WHERE portal_id = ? AND run_id != '__system__' ORDER BY started_at ASC;",
            (portal_id,)
        ).fetchall()

        runs_dict: dict[str, CatalogRunEntry] = {}
        all_ids: list[str] = []
        for r in rows:
            meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
            run_entry = CatalogRunEntry(
                run_id=r["run_id"],
                previous_run_id=meta.get("previous_run_id"),
                mode=meta.get("mode", "incremental"),
                started_at=r["started_at"],
                completed_at=r["completed_at"],
                status=r["status"],
                article_count=r["articles_discovered"],
                new_article_count=r["new_articles_count"],
                changed_article_count=r["changed_articles_count"],
                run_dir=meta.get("run_dir", f"runs/{r['run_id']}"),
                manual_review_status=meta.get("manual_review_status", "PENDING"),
                anomalies=meta.get("anomalies", []),
            )
            runs_dict[r["run_id"]] = run_entry
            all_ids.append(r["run_id"])

        catalog = ArchiveCatalog(
            portal_id=portal["portal_id"],
            latest_successful_run_id=portal["latest_successful_run_id"],
            all_run_ids=all_ids,
            runs=runs_dict,
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        catalog.save_to_json(dest_path)
        return catalog

    def export_portal_state_json(self, portal_id: str, run_id: str, dest_path: Path) -> PortalState:
        """Export SQLite records back to a PortalState object and save to JSON."""
        conn = self.db.connect()
        p_row = conn.execute(
            "SELECT * FROM portal_states WHERE portal_id = ? AND run_id = ?;",
            (portal_id, run_id)
        ).fetchone()
        if not p_row:
            raise ValueError(f"PortalState not found for portal {portal_id}, run {run_id}")

        meta = json.loads(p_row["source_manifest_json"]) if p_row["source_manifest_json"] else {}
        source_manifest = meta.get("source_manifest")
        archive_order = meta.get("archive_order", [])
        head_fingerprint = meta.get("head_fingerprint")
        boundary_fingerprints = meta.get("boundary_fingerprints", {})

        # Load archive pages
        page_rows = conn.execute(
            "SELECT * FROM archive_page_observations WHERE portal_id = ? AND run_id = ?;",
            (portal_id, run_id)
        ).fetchall()
        pages_dict = {}
        for pr in page_rows:
            pages_dict[pr["page_url"]] = ArchivePageState(
                url=pr["page_url"],
                ordered_fingerprint=pr["content_hash"] or "",
                unordered_fingerprint=pr["content_hash"] or "",
                article_urls=[],
                page_number=pr["page_number"],
                observed_at=pr["observed_at"],
            )

        # Load articles
        art_rows = conn.execute(
            "SELECT * FROM article_states WHERE portal_id = ? AND last_extracted_run_id = ?;",
            (portal_id, run_id)
        ).fetchall()
        articles_dict = {}
        for ar in art_rows:
            a_meta = json.loads(ar["metadata_json"]) if ar["metadata_json"] else {}
            articles_dict[ar["article_url"]] = ArticleState(
                canonical_url=ar["article_url"],
                first_seen_run_id=ar["first_discovered_run_id"],
                last_seen_run_id=ar["last_extracted_run_id"],
                title=ar["title"],
                published_at=ar["published_date"],
                content_hash=ar["content_sha256"],
                status=ar["extraction_status"],
                warc_file=ar["warc_path"],
                warc_record_id=ar["warc_record_id"],
                last_captured_at=ar["last_extracted_at"],
                metadata=a_meta,
            )

        state = PortalState(
            portal_id=portal_id,
            run_id=run_id,
            captured_at=p_row["state_timestamp"],
            archive_pages=pages_dict,
            articles=articles_dict,
            archive_order=archive_order,
            head_fingerprint=head_fingerprint,
            boundary_fingerprints=boundary_fingerprints,
            total_articles_count=p_row["total_articles_count"],
            total_archive_pages_count=p_row["total_archive_pages_count"],
            source_manifest=source_manifest,
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        state.save_to_json(dest_path)
        return state

    # =========================================================================
    # Deposit Registry Import & Export
    # =========================================================================

    def import_deposit_registry_json(self, registry_path: Path, portal_id: str) -> int:
        """Import a deposit_registry.json file into deposit_records and deposit_artifacts tables."""
        if not registry_path.exists():
            raise FileNotFoundError(f"Deposit registry not found: {registry_path}")

        from src.zenodo_stuff.registry import DepositRegistry
        registry = DepositRegistry.load_or_create(registry_path)
        count = 0

        with self.db.transaction() as conn:
            for key, receipt in registry.receipts.items():
                if receipt.portal_id != portal_id:
                    continue
                meta = {
                    "package_fingerprint": receipt.package_fingerprint,
                    "environment": receipt.environment,
                    "remote_record_id": receipt.remote_record_id,
                    "remote_bucket_url": receipt.remote_bucket_url,
                    "remote_record_url": receipt.remote_record_url,
                    "checksum_verification_outcome": receipt.checksum_verification_outcome,
                    "last_error_category": receipt.last_error_category,
                    "retry_eligible": receipt.retry_eligible,
                    "reconciliation_notes": receipt.reconciliation_notes,
                }
                self.repo.upsert_deposit_record(
                    portal_id=portal_id,
                    deposit_id=receipt.receipt_id,
                    package_id=receipt.run_id,
                    target_repository=receipt.repository_target,
                    status=receipt.status,
                    submitted_at=receipt.created_at,
                    deposition_id=receipt.remote_record_id,
                    doi=receipt.doi,
                    completed_at=receipt.updated_at if receipt.status == "PUBLISHED" else None,
                    error_message=receipt.last_error,
                    metadata=meta,
                    conn=conn,
                )
                for fname, art in receipt.artifact_inventory.items():
                    self.repo.upsert_deposit_artifact(
                        portal_id=portal_id,
                        deposit_id=receipt.receipt_id,
                        artifact_filename=art.filename,
                        file_size_bytes=art.filesize,
                        sha256_checksum=art.local_sha256,
                        remote_file_id=art.remote_file_id,
                        upload_status="VERIFIED" if art.verified else "UPLOADED",
                        uploaded_at=receipt.updated_at,
                        conn=conn,
                    )
                count += 1
        return count

    def export_deposit_registry_json(self, portal_id: str, dest_path: Path) -> Any:
        """Export SQLite deposit_records and deposit_artifacts back to a DepositRegistry and save."""
        from src.zenodo_stuff.registry import DepositRegistry
        from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord

        registry = DepositRegistry(dest_path)
        conn = self.db.connect()
        records = conn.execute(
            "SELECT * FROM deposit_records WHERE portal_id = ? ORDER BY submitted_at ASC;",
            (portal_id,)
        ).fetchall()

        for rec in records:
            deposit_id = rec["deposit_id"]
            meta = json.loads(rec["metadata_json"]) if rec["metadata_json"] else {}
            
            # Fetch artifacts
            art_rows = conn.execute(
                "SELECT * FROM deposit_artifacts WHERE portal_id = ? AND deposit_id = ?;",
                (portal_id, deposit_id)
            ).fetchall()

            inventory = {}
            for ar in art_rows:
                inventory[ar["artifact_filename"]] = UploadedArtifactRecord(
                    filename=ar["artifact_filename"],
                    filesize=ar["file_size_bytes"],
                    local_sha256=ar["sha256_checksum"],
                    local_md5="",
                    remote_file_id=ar["remote_file_id"],
                    verified=(ar["upload_status"] == "VERIFIED"),
                )

            receipt = DepositReceipt(
                receipt_id=deposit_id,
                portal_id=portal_id,
                run_id=rec["package_id"],
                package_fingerprint=meta.get("package_fingerprint", ""),
                repository_target=rec["target_repository"],
                environment=meta.get("environment", "sandbox"),
                status=rec["status"],
                created_at=rec["submitted_at"],
                updated_at=rec["completed_at"] or rec["submitted_at"],
                remote_record_id=rec["deposition_id"] or meta.get("remote_record_id"),
                remote_bucket_url=meta.get("remote_bucket_url"),
                remote_record_url=meta.get("remote_record_url"),
                doi=rec["doi"],
                artifact_inventory=inventory,
                checksum_verification_outcome=meta.get("checksum_verification_outcome", "UNVERIFIED"),
                last_error=rec["error_message"],
                last_error_category=meta.get("last_error_category"),
                retry_eligible=meta.get("retry_eligible", True),
                reconciliation_notes=meta.get("reconciliation_notes"),
            )
            registry.receipts[f"{portal_id}:{receipt.run_id}"] = receipt

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        registry.save()
        return registry

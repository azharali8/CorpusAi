"""
CorpusAI State Migration and Bi-directional Export Tool.

Provides:
- Ingestion of legacy JSON files (catalog.json, portal_state.json, content_url_registry.json, deposit_receipts)
  into SQLite tables.
- Verification of per-field parity.
- Reverse export from SQLite back to authoritative or interchange JSON representations.
"""

import json
import logging
from pathlib import Path
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from src.storage.sqlite_backend import CorpusAIStateDatabase
from src.storage.repository import CorpusAIRepository
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.portal_state import PortalState, ArticleState, ArchivePageState
from src.content_url_registry import ContentURLRegistry
from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord

logger = logging.getLogger("corpusai.storage.migration")


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

    def import_deposit_registry_json(
        self,
        registry_path: Path,
        portal_id: str,
        dry_run: bool = False,
        backup_source: bool = True,
        return_report: bool = False,
    ) -> Union[int, Dict[str, Any]]:
        """
        Import a deposit_registry.json file into deposit_records and deposit_artifacts tables.

        Safety safeguards:
        - dry_run mode: validates schema and records without modifying the database.
        - backup_source: creates a timestamped copy of source JSON before import.
        - validation: validates status, checksums, package existence.
        - conflict detection: reports duplicates and conflicts without overwriting silently.
        - reconciliation report: returns imported count (or full report dict if return_report=True).
        """
        if not registry_path.exists():
            raise FileNotFoundError(f"Deposit registry not found: {registry_path}")

        import hashlib
        source_bytes = registry_path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        migration_id = f"deposit_registry:{portal_id}"

        # 1. Source backup if requested and not in dry_run
        if backup_source and not dry_run:
            backup_path = registry_path.with_name(
                f"{registry_path.name}.bak.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            )
            shutil.copy2(registry_path, backup_path)
            logger.info(f"Created pre-migration backup at {backup_path}")

        from src.zenodo_stuff.models import DepositReceipt
        from src.storage.sa_deposit_adapter import SADepositPersistenceAdapter

        data = json.loads(source_bytes.decode("UTF-8"))

        receipts_raw = data.get("receipts", {})
        report: Dict[str, Any] = {
            "dry_run": dry_run,
            "migration_id": migration_id,
            "source_sha256": source_sha256,
            "already_completed": False,
            "source_changed": False,
            "total_evaluated": len(receipts_raw),
            "imported": 0,
            "would_import": 0,
            "skipped": 0,
            "missing_parent": 0,
            "duplicates": 0,
            "conflicts": [],
            "reconciled": False,
        }

        sa_adapter = SADepositPersistenceAdapter(self.db.db_path)
        existing_marker = sa_adapter.get_data_migration_marker(migration_id)
        if existing_marker is not None:
            if existing_marker["source_sha256"] == source_sha256:
                report["already_completed"] = True
            else:
                report["source_changed"] = True
                report["conflicts"].append({
                    "run_id": "__migration_marker__",
                    "reason": "source_sha256_mismatch",
                    "recorded_sha256": existing_marker["source_sha256"],
                    "incoming_sha256": source_sha256,
                })
                if not dry_run:
                    if return_report:
                        return report
                    return 0
        valid_receipts: List[DepositReceipt] = []

        for key, r_data in receipts_raw.items():
            receipt = DepositReceipt.from_dict(r_data)
            if receipt.portal_id != portal_id:
                report["skipped"] += 1
                continue

            # Verify parent package exists before considering valid for import
            if not sa_adapter.package_exists(portal_id, receipt.run_id):
                report["missing_parent"] += 1
                continue

            existing = sa_adapter.get_receipt(portal_id, receipt.run_id)
            if existing is not None:
                # Complete canonical comparison of all persisted receipt and artifact fields
                same_scalars = (
                    existing.receipt_id == receipt.receipt_id
                    and existing.portal_id == receipt.portal_id
                    and existing.run_id == receipt.run_id
                    and existing.package_fingerprint == receipt.package_fingerprint
                    and existing.repository_target == receipt.repository_target
                    and existing.environment == receipt.environment
                    and str(existing.status) == str(receipt.status)
                    and existing.created_at == receipt.created_at
                    and existing.updated_at == receipt.updated_at
                    and existing.remote_record_id == receipt.remote_record_id
                    and existing.remote_bucket_url == receipt.remote_bucket_url
                    and existing.remote_record_url == receipt.remote_record_url
                    and existing.doi == receipt.doi
                    and existing.checksum_verification_outcome == receipt.checksum_verification_outcome
                    and existing.last_error == receipt.last_error
                    and existing.last_error_category == receipt.last_error_category
                    and existing.retry_eligible == receipt.retry_eligible
                    and existing.reconciliation_notes == receipt.reconciliation_notes
                )

                # Compare complete artifact inventories
                same_artifacts = True
                if len(existing.artifact_inventory) != len(receipt.artifact_inventory):
                    same_artifacts = False
                else:
                    for art_name, art_in in receipt.artifact_inventory.items():
                        art_ex = existing.artifact_inventory.get(art_name)
                        if art_ex is None:
                            same_artifacts = False
                            break
                        if (
                            art_ex.filename != art_in.filename
                            or art_ex.filesize != art_in.filesize
                            or art_ex.local_sha256 != art_in.local_sha256
                            or art_ex.local_md5 != art_in.local_md5
                            or art_ex.remote_md5 != art_in.remote_md5
                            or art_ex.remote_file_id != art_in.remote_file_id
                            or art_ex.verified != art_in.verified
                        ):
                            same_artifacts = False
                            break

                if same_scalars and same_artifacts:
                    report["duplicates"] += 1
                    continue
                else:
                    report["conflicts"].append({
                        "run_id": receipt.run_id,
                        "existing_status": str(existing.status),
                        "incoming_status": str(receipt.status),
                        "existing_id": existing.receipt_id,
                        "incoming_id": receipt.receipt_id,
                    })
                    continue

            valid_receipts.append(receipt)

        if dry_run:
            report["imported"] = 0
            report["would_import"] = len(valid_receipts)
            report["reconciled"] = False
        else:
            marker_payload = None
            if len(report["conflicts"]) == 0 and report["missing_parent"] == 0 and not report["already_completed"]:
                marker_payload = {
                    "migration_id": migration_id,
                    "source_path": str(registry_path),
                    "source_sha256": source_sha256,
                    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }

            if valid_receipts or marker_payload is not None:
                persisted_count = sa_adapter.save_receipts_batch(valid_receipts, migration_marker=marker_payload)
                report["imported"] = persisted_count
            else:
                report["imported"] = 0
            report["would_import"] = 0

            # Perform complete canonical post-migration reconciliation against source.
            # A migration is reconciled if and only if:
            # 1. No conflicts and no missing parents.
            # 2. Every portal receipt in the source JSON exists in DB with full field-by-field equality
            #    across ALL 18 persisted receipt scalars AND ALL 7 persisted artifact fields.
            reconciled = (len(report["conflicts"]) == 0 and report["missing_parent"] == 0)
            if reconciled:
                for r_data in receipts_raw.values():
                    r = DepositReceipt.from_dict(r_data)
                    if r.portal_id == portal_id:
                        db_r = sa_adapter.get_receipt(portal_id, r.run_id)
                        if db_r is None:
                            reconciled = False
                            break
                        # Compare all 18 persisted receipt scalar fields
                        if (
                            db_r.receipt_id != r.receipt_id
                            or db_r.portal_id != r.portal_id
                            or db_r.run_id != r.run_id
                            or db_r.package_fingerprint != r.package_fingerprint
                            or db_r.repository_target != r.repository_target
                            or db_r.environment != r.environment
                            or str(db_r.status) != str(r.status)
                            or db_r.created_at != r.created_at
                            or db_r.updated_at != r.updated_at
                            or db_r.remote_record_id != r.remote_record_id
                            or db_r.remote_bucket_url != r.remote_bucket_url
                            or db_r.remote_record_url != r.remote_record_url
                            or db_r.doi != r.doi
                            or db_r.checksum_verification_outcome != r.checksum_verification_outcome
                            or db_r.last_error != r.last_error
                            or db_r.last_error_category != r.last_error_category
                            or db_r.retry_eligible != r.retry_eligible
                            or db_r.reconciliation_notes != r.reconciliation_notes
                            or len(db_r.artifact_inventory) != len(r.artifact_inventory)
                        ):
                            reconciled = False
                            break
                        # Compare all 7 persisted artifact fields per artifact
                        for a_name, a_val in r.artifact_inventory.items():
                            db_a = db_r.artifact_inventory.get(a_name)
                            if (
                                db_a is None
                                or db_a.filename != a_val.filename
                                or db_a.filesize != a_val.filesize
                                or db_a.local_sha256 != a_val.local_sha256
                                or db_a.local_md5 != a_val.local_md5
                                or db_a.remote_md5 != a_val.remote_md5
                                or db_a.remote_file_id != a_val.remote_file_id
                                or db_a.verified != a_val.verified
                            ):
                                reconciled = False
                                break
                        if not reconciled:
                            break
            report["reconciled"] = reconciled

        if return_report:
            return report
        return report["imported"]

    def export_deposit_registry_json(self, portal_id: str, dest_path: Path) -> Any:
        """Export SQLite deposit_records and deposit_artifacts back to a DepositRegistry and save."""
        from src.zenodo_stuff.registry import DepositRegistry
        from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord

        registry = DepositRegistry(registry_path=dest_path, db_path=self.db.db_path)
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
            # Extras stored in metadata_json carry local_md5 and remote_md5
            art_extras = meta.get("artifact_inventory_extras", {}) if isinstance(meta.get("artifact_inventory_extras"), dict) else {}
            for ar in art_rows:
                fname = ar["artifact_filename"]
                extra = art_extras.get(fname, {})
                inventory[fname] = UploadedArtifactRecord(
                    filename=fname,
                    filesize=ar["file_size_bytes"],
                    local_sha256=ar["sha256_checksum"],
                    local_md5=extra.get("local_md5", ""),
                    remote_md5=extra.get("remote_md5"),
                    remote_file_id=ar["remote_file_id"],
                    verified=(ar["upload_status"] == "VERIFIED"),
                )

            receipt = DepositReceipt(
                receipt_id=deposit_id,
                portal_id=portal_id,
                run_id=rec["package_id"],
                package_fingerprint=meta.get("package_fingerprint", ""),
                repository_target=rec["target_repository"],
                environment=meta.get("environment", "unknown"),
                status=rec["status"],
                created_at=rec["submitted_at"],
                # updated_at is authoritative in metadata_json; fall back to completed_at,
                # then submitted_at so the field is never empty.
                updated_at=meta.get("updated_at") or rec["completed_at"] or rec["submitted_at"],
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
        registry.export_json(dest_path)
        return registry

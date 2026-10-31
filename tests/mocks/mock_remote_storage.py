"""
Mock In-Memory Remote Storage Backend for CorpusAI Phase 9C testing.

Test-support implementation of RemoteStorageContract.
Simulates object store keys, byte payloads, metadata, and conditional writes.
Moved from src/remote_storage_contract.py (Batch 2C).
"""

import datetime
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.remote_storage_contract import (
    BaselinePointer,
    LeaseInfo,
    PortalControlRecord,
    RemoteArtifactMetadata,
    RemoteCasConflictError,
    RemoteLockError,
    RemoteQuarantineRecord,
    RemoteStorageContract,
    RemoteStorageError,
)


class MockRemoteStorageBackend(RemoteStorageContract):
    """
    Deterministic In-Memory implementation of RemoteStorageContract.
    Simulates object store keys, byte payloads, metadata, and conditional writes.
    """

    def __init__(self):
        # key -> {"data": bytes, "etag": str, "metadata": Dict[str, str], "sha256": str}
        self.objects: Dict[str, Dict[str, Any]] = {}
        self.quarantine_records: List[RemoteQuarantineRecord] = []
        self.simulate_network_failure: bool = False
        self.injected_corruptions: Set[str] = set()

    def _compute_sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _compute_etag(self, data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def _get_control_key(self, portal_id: str) -> str:
        return f"state/{portal_id}/portal_control.json"

    def acquire_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[PortalControlRecord], Optional[str]]:
        control_key = self._get_control_key(portal_id)
        now_dt = datetime.datetime.now(datetime.timezone.utc) if current_time_iso is None else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        now_iso = now_dt.isoformat()
        expires_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        expires_iso = expires_dt.isoformat()

        if control_key in self.objects:
            obj = self.objects[control_key]
            record = PortalControlRecord.from_dict(json.loads(obj["data"].decode("utf-8")))
            if not record.lease.is_expired(now_iso):
                # Lock actively held
                return False, record, obj["etag"]

            # Vacant or expired: claim lease and increment fencing_generation ONLY
            record.fencing_generation += 1
            record.control_version += 1
            record.lease = LeaseInfo(
                lease_owner_id=runner_id,
                acquired_at=now_iso,
                renewed_at=now_iso,
                expires_at=expires_iso,
                ttl_seconds=ttl_seconds,
            )
            record.updated_at = now_iso
            data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
            new_etag = self._compute_etag(data_bytes)
            self.objects[control_key] = {
                "data": data_bytes,
                "etag": new_etag,
                "sha256": self._compute_sha256(data_bytes),
                "metadata": {},
            }
            return True, record, new_etag
        else:
            # First time initialization for portal
            record = PortalControlRecord(
                portal_id=portal_id,
                control_version=1,
                fencing_generation=1,
                baseline_generation=0,
                lease=LeaseInfo(
                    lease_owner_id=runner_id,
                    acquired_at=now_iso,
                    renewed_at=now_iso,
                    expires_at=expires_iso,
                    ttl_seconds=ttl_seconds,
                ),
                updated_at=now_iso,
            )
            data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
            new_etag = self._compute_etag(data_bytes)
            self.objects[control_key] = {
                "data": data_bytes,
                "etag": new_etag,
                "sha256": self._compute_sha256(data_bytes),
                "metadata": {},
            }
            return True, record, new_etag

    def renew_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        control_key = self._get_control_key(portal_id)
        if control_key not in self.objects:
            return False, None

        obj = self.objects[control_key]
        if obj["etag"] != expected_control_etag:
            return False, None

        record = PortalControlRecord.from_dict(json.loads(obj["data"].decode("utf-8")))
        if record.lease.lease_owner_id != runner_id or record.fencing_generation != fencing_generation:
            return False, None

        now_dt = datetime.datetime.now(datetime.timezone.utc) if current_time_iso is None else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        now_iso = now_dt.isoformat()
        if record.lease.is_expired(now_iso):
            return False, None

        expires_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        record.lease.renewed_at = now_iso
        record.lease.expires_at = expires_dt.isoformat()
        record.control_version += 1
        record.updated_at = now_iso

        # Invariant: Renewal does not advance fencing_generation or baseline_generation
        data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        new_etag = self._compute_etag(data_bytes)
        self.objects[control_key] = {
            "data": data_bytes,
            "etag": new_etag,
            "sha256": self._compute_sha256(data_bytes),
            "metadata": {},
        }
        return True, new_etag

    def release_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
    ) -> bool:
        control_key = self._get_control_key(portal_id)
        if control_key not in self.objects:
            return False

        obj = self.objects[control_key]
        if obj["etag"] != expected_control_etag:
            return False

        record = PortalControlRecord.from_dict(json.loads(obj["data"].decode("utf-8")))
        if record.lease.lease_owner_id != runner_id or record.fencing_generation != fencing_generation:
            return False

        # Release lease: vacate owner and zero expiry, but PRESERVE fencing_generation & baseline_generation
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record.lease = LeaseInfo(
            lease_owner_id=None,
            acquired_at=None,
            renewed_at=None,
            expires_at="1970-01-01T00:00:00Z",
            ttl_seconds=record.lease.ttl_seconds,
        )
        record.control_version += 1
        record.updated_at = now_iso

        data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        self.objects[control_key] = {
            "data": data_bytes,
            "etag": self._compute_etag(data_bytes),
            "sha256": self._compute_sha256(data_bytes),
            "metadata": {},
        }
        return True

    def hydrate_state(
        self,
        portal_id: str,
        target_dir: str,
    ) -> Tuple[Optional[PortalControlRecord], Optional[str], Optional[ArchiveCatalog], Optional[str], Optional[Dict[str, Any]]]:
        os.makedirs(target_dir, exist_ok=True)
        control_key = self._get_control_key(portal_id)
        catalog_key = f"state/{portal_id}/catalog.json"
        deposit_key = f"state/{portal_id}/deposit_registry.json"

        control_record = None
        control_etag = None
        catalog = None
        catalog_etag = None
        deposit_data = None

        if control_key in self.objects:
            c_obj = self.objects[control_key]
            control_etag = c_obj["etag"]
            c_dict = json.loads(c_obj["data"].decode("utf-8"))
            local_control_path = os.path.join(target_dir, "portal_control.json")
            with open(local_control_path, "w", encoding="utf-8") as f:
                json.dump(c_dict, f, indent=2)
            control_record = PortalControlRecord.from_dict(c_dict)

        if catalog_key in self.objects:
            cat_obj = self.objects[catalog_key]
            catalog_etag = cat_obj["etag"]
            cat_dict = json.loads(cat_obj["data"].decode("utf-8"))
            local_cat_path = os.path.join(target_dir, "catalog.json")
            with open(local_cat_path, "w", encoding="utf-8") as f:
                json.dump(cat_dict, f, indent=2)
            catalog = ArchiveCatalog.from_dict(cat_dict)

        # Baseline resolution: portal_control.json is STRICTLY authoritative
        active_baseline_id = None
        if control_record and control_record.baseline.latest_successful_run_id:
            active_baseline_id = control_record.baseline.latest_successful_run_id
            # Reconcile local catalog projection if out of sync with control record
            if catalog and catalog.latest_successful_run_id != active_baseline_id:
                catalog.latest_successful_run_id = active_baseline_id
                local_cat_path = os.path.join(target_dir, "catalog.json")
                catalog.save_to_json(local_cat_path)

        if active_baseline_id:
            pkg_prefix = f"packages/{portal_id}/runs/{active_baseline_id}/"
            for obj_key, obj_val in self.objects.items():
                if obj_key.startswith(pkg_prefix):
                    rel_file = obj_key[len(pkg_prefix):]
                    dst_path = os.path.join(target_dir, "packages", portal_id, "runs", active_baseline_id, rel_file)
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    with open(dst_path, "wb") as f:
                        f.write(obj_val["data"])

        if deposit_key in self.objects:
            dep_obj = self.objects[deposit_key]
            deposit_data = json.loads(dep_obj["data"].decode("utf-8"))
            local_dep_path = os.path.join(target_dir, "deposit_registry.json")
            with open(local_dep_path, "w", encoding="utf-8") as f:
                json.dump(deposit_data, f, indent=2)

        return control_record, control_etag, catalog, catalog_etag, deposit_data

    def stage_package_artifacts(
        self,
        portal_id: str,
        run_id: str,
        local_files: Dict[str, str],
    ) -> Dict[str, RemoteArtifactMetadata]:
        if self.simulate_network_failure:
            raise RemoteStorageError("Simulated network failure during package artifact staging.")

        result_meta: Dict[str, RemoteArtifactMetadata] = {}
        for rel_path, local_abs_path in local_files.items():
            norm_rel = rel_path.replace("\\", "/")
            remote_key = f"packages/{portal_id}/runs/{run_id}/{norm_rel}"

            with open(local_abs_path, "rb") as f:
                file_bytes = f.read()

            if norm_rel in self.injected_corruptions:
                file_bytes = file_bytes + b"\x00_CORRUPTED"

            sha256 = self._compute_sha256(file_bytes)
            etag = self._compute_etag(file_bytes)
            self.objects[remote_key] = {
                "data": file_bytes,
                "etag": etag,
                "sha256": sha256,
                "metadata": {"sha256": sha256},
            }
            result_meta[norm_rel] = RemoteArtifactMetadata(
                key=remote_key,
                size=len(file_bytes),
                etag=etag,
                sha256=sha256,
                content_type="application/octet-stream",
                metadata={"sha256": sha256},
            )
        return result_meta

    def verify_remote_artifacts(
        self,
        portal_id: str,
        run_id: str,
        expected_checksums: Dict[str, str],
    ) -> Tuple[bool, List[str]]:
        mismatches: List[str] = []
        for rel_path, expected_sha256 in expected_checksums.items():
            norm_rel = rel_path.replace("\\", "/")
            remote_key = f"packages/{portal_id}/runs/{run_id}/{norm_rel}"
            if remote_key not in self.objects:
                mismatches.append(f"Missing remote file: {norm_rel}")
                continue

            obj = self.objects[remote_key]
            # Must compute actual byte SHA-256 independently
            actual_sha256 = self._compute_sha256(obj["data"])
            if actual_sha256 != expected_sha256:
                mismatches.append(
                    f"Checksum mismatch for {norm_rel}: expected {expected_sha256}, got {actual_sha256}"
                )

        return len(mismatches) == 0, mismatches

    def quarantine_failed_upload(
        self,
        portal_id: str,
        run_id: str,
        runner_id: str,
        reason: str,
        details: Dict[str, Any],
    ) -> str:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_id = f"quarantine-{run_id}-{timestamp}"
        rec = RemoteQuarantineRecord(
            quarantine_id=quarantine_id,
            portal_id=portal_id,
            run_id=run_id,
            reason=reason,
            detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            runner_id=runner_id,
            details=details,
        )
        self.quarantine_records.append(rec)
        q_key = f"state/{portal_id}/quarantine/{quarantine_id}.json"
        data_bytes = json.dumps(rec.to_dict(), indent=2).encode("utf-8")
        self.objects[q_key] = {
            "data": data_bytes,
            "etag": self._compute_etag(data_bytes),
            "sha256": self._compute_sha256(data_bytes),
            "metadata": {},
        }
        return quarantine_id

    def commit_baseline_and_catalog(
        self,
        portal_id: str,
        run_id: str,
        catalog: ArchiveCatalog,
        expected_catalog_etag: Optional[str],
        expected_control_etag: str,
        fencing_generation: int,
        runner_id: str,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        control_key = self._get_control_key(portal_id)
        if control_key not in self.objects:
            return False, None, None

        control_obj = self.objects[control_key]
        # 1. Atomic Check: control record ETag must match expected ETag
        if control_obj["etag"] != expected_control_etag:
            return False, None, None

        control_record = PortalControlRecord.from_dict(json.loads(control_obj["data"].decode("utf-8")))

        # 2. Fencing Generation & Ownership Check
        if control_record.lease.lease_owner_id != runner_id:
            return False, None, None
        if control_record.fencing_generation != fencing_generation:
            return False, None, None

        # 3. Catalog CAS check
        catalog_key = f"state/{portal_id}/catalog.json"
        current_cat_etag = self.objects[catalog_key]["etag"] if catalog_key in self.objects else None
        if expected_catalog_etag != current_cat_etag:
            return False, None, None

        now_dt = datetime.datetime.now(datetime.timezone.utc) if current_time_iso is None else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        now_iso = now_dt.isoformat()

        # Commit Catalog Object
        cat_bytes = json.dumps(catalog.to_dict(), indent=2).encode("utf-8")
        new_cat_etag = self._compute_etag(cat_bytes)
        self.objects[catalog_key] = {
            "data": cat_bytes,
            "etag": new_cat_etag,
            "sha256": self._compute_sha256(cat_bytes),
            "metadata": {},
        }

        # Update Portal Control Record atomically (advances authoritative active baseline pointer)
        control_record.control_version += 1
        control_record.baseline_generation += 1  # Invariant: increments on new baseline publication
        control_record.baseline = BaselinePointer(
            latest_successful_run_id=run_id,
            committed_at=now_iso,
            committed_by_generation=fencing_generation,
            baseline_generation=control_record.baseline_generation,
            manifest_path=f"packages/{portal_id}/runs/{run_id}/manifest.json",
            checksums_path=f"packages/{portal_id}/runs/{run_id}/checksums.sha256",
        )
        control_record.catalog_head_etag = new_cat_etag
        control_record.updated_at = now_iso

        control_bytes = json.dumps(control_record.to_dict(), indent=2).encode("utf-8")
        new_control_etag = self._compute_etag(control_bytes)
        self.objects[control_key] = {
            "data": control_bytes,
            "etag": new_control_etag,
            "sha256": self._compute_sha256(control_bytes),
            "metadata": {},
        }

        return True, new_control_etag, new_cat_etag

    def project_control_to_catalog(
        self,
        portal_id: str,
        control_record: PortalControlRecord,
        verified_run_entry: CatalogRunEntry,
        expected_catalog_etag: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Projects an already-committed baseline from portal_control.json onto catalog.json.
        Enforces strict non-regression: never regresses latest_successful_run_id.
        """
        catalog_key = f"state/{portal_id}/catalog.json"
        current_cat_etag = self.objects[catalog_key]["etag"] if catalog_key in self.objects else None
        if expected_catalog_etag is not None and expected_catalog_etag != current_cat_etag:
            return False, current_cat_etag

        cat_dict = json.loads(self.objects[catalog_key]["data"].decode("utf-8")) if catalog_key in self.objects else {"portal_id": portal_id, "runs": {}}
        catalog = ArchiveCatalog.from_dict(cat_dict)

        target_baseline_id = control_record.baseline.latest_successful_run_id
        if not target_baseline_id:
            return False, current_cat_etag

        # Non-regression invariant:
        # If catalog already points to a newer run than the one being projected, refuse to regress
        if catalog.latest_successful_run_id and catalog.latest_successful_run_id != target_baseline_id:
            if catalog.latest_successful_run_id in catalog.runs:
                curr_entry = catalog.runs[catalog.latest_successful_run_id]
                if curr_entry.started_at > verified_run_entry.started_at:
                    return False, current_cat_etag

        catalog.register_run(verified_run_entry, is_successful=True)
        catalog.latest_successful_run_id = target_baseline_id

        cat_bytes = json.dumps(catalog.to_dict(), indent=2).encode("utf-8")
        new_cat_etag = self._compute_etag(cat_bytes)
        self.objects[catalog_key] = {
            "data": cat_bytes,
            "etag": new_cat_etag,
            "sha256": self._compute_sha256(cat_bytes),
            "metadata": {},
        }
        return True, new_cat_etag

    def sync_deposit_registry(
        self,
        portal_id: str,
        registry_data: Dict[str, Any],
    ) -> bool:
        dep_key = f"state/{portal_id}/deposit_registry.json"
        data_bytes = json.dumps(registry_data, indent=2).encode("utf-8")
        self.objects[dep_key] = {
            "data": data_bytes,
            "etag": self._compute_etag(data_bytes),
            "sha256": self._compute_sha256(data_bytes),
            "metadata": {},
        }
        return True

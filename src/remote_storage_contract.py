"""
Remote Storage Protocol, Data Models for CorpusAI Phase 9C-A.

Implements an explicit protocol boundary for durable state persistence across
ephemeral runners without coupling to specific cloud providers (AWS S3, Cloudflare R2).

Architecture & Invariants:
1. Two-Tier Monotonic Generations:
   - fencing_generation: Monotonically increments on every successful lease acquisition or takeover.
     It never resets across lease releases or expiries.
   - baseline_generation: Monotonically increments ONLY when a new baseline is published and committed.
     Lease renewals, releases, and non-baseline updates MUST NOT advance baseline_generation.
2. Sole Authority: state/{portal_id}/portal_control.json is the single authoritative source of truth
   for lease locking, fencing/baseline generations, and the active baseline commit pointer.
3. Expiry & Takeover Semantics:
   - Expiry renders the control record takeover-eligible, but does NOT automatically revoke ownership
     until an atomic Compare-And-Swap (CAS) update succeeds.
   - Races between publication and takeover are resolved by atomic conditional write (If-Match)
     to portal_control.json.
   - Once takeover updates the control record, the former owner's stale ETag is permanently
     invalidated and strictly cannot publish.
   - A runner whose heartbeat fails must abort before baseline publication.
4. Non-Regressing Catalog Projection:
   - catalog.json is an auxiliary projected history ledger.
   - Projections from portal_control.json to catalog.json are idempotent and conditional.
   - A delayed recovery or stale process can NEVER move catalog.latest_successful_run_id backward.
5. Byte-Level Checksum Verification:
   - SHA-256 verification reads actual payload bytes. User metadata or provider ETags alone
     are never treated as proof of content integrity.
6. Non-Destructive Quarantine:
   - Verification failures write a quarantine manifest to state/{portal_id}/quarantine/
     without deleting package artifacts.
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry


@dataclass
class LeaseInfo:
    """Lease status inside the portal control record."""
    lease_owner_id: Optional[str] = None
    acquired_at: Optional[str] = None
    renewed_at: Optional[str] = None
    expires_at: Optional[str] = None
    ttl_seconds: int = 2700

    def is_expired(self, current_time_iso: Optional[str] = None) -> bool:
        if not self.expires_at or not self.lease_owner_id:
            return True
        if current_time_iso is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        else:
            now = datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        exp = datetime.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return now >= exp

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LeaseInfo":
        return cls(**data)


@dataclass
class BaselinePointer:
    """Active published baseline metadata."""
    latest_successful_run_id: Optional[str] = None
    committed_at: Optional[str] = None
    committed_by_generation: Optional[int] = None
    baseline_generation: int = 0
    manifest_path: Optional[str] = None
    checksums_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaselinePointer":
        return cls(**data)


@dataclass
class PortalControlRecord:
    """
    Single Authoritative Control Record stored at state/{portal_id}/portal_control.json.
    Coordinates lease ownership, fencing generation, baseline generation, and active baseline publication.
    """
    portal_id: str
    control_version: int = 1
    fencing_generation: int = 0
    baseline_generation: int = 0
    lease: LeaseInfo = field(default_factory=LeaseInfo)
    baseline: BaselinePointer = field(default_factory=BaselinePointer)
    catalog_head_etag: Optional[str] = None
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portal_id": self.portal_id,
            "control_version": self.control_version,
            "fencing_generation": self.fencing_generation,
            "baseline_generation": self.baseline_generation,
            "lease": self.lease.to_dict(),
            "baseline": self.baseline.to_dict(),
            "catalog_head_etag": self.catalog_head_etag,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortalControlRecord":
        return cls(
            portal_id=data["portal_id"],
            control_version=data.get("control_version", 1),
            fencing_generation=data.get("fencing_generation", 0),
            baseline_generation=data.get("baseline_generation", 0),
            lease=LeaseInfo.from_dict(data.get("lease", {})),
            baseline=BaselinePointer.from_dict(data.get("baseline", {})),
            catalog_head_etag=data.get("catalog_head_etag"),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class RemoteQuarantineRecord:
    """Diagnostic manifest for quarantined corrupted or incomplete uploads."""
    quarantine_id: str
    portal_id: str
    run_id: str
    reason: str
    detected_at: str
    runner_id: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemoteQuarantineRecord":
        return cls(**data)


@dataclass
class RemoteArtifactMetadata:
    """Metadata for an object in remote storage including sha256."""
    key: str
    size: int
    etag: str
    sha256: str
    content_type: str
    metadata: Dict[str, str] = field(default_factory=dict)


class RemoteStorageError(Exception):
    """Base exception for remote storage protocol failures."""
    pass


class RemoteLockError(RemoteStorageError):
    """Raised when lease acquisition or release fails."""
    pass


class RemoteCasConflictError(RemoteStorageError):
    """Raised when Control Record or Catalog Compare-And-Swap condition fails."""
    pass


class RemoteChecksumMismatchError(RemoteStorageError):
    """Raised when remote object SHA-256 does not match local checksum."""
    pass


class RemoteStorageContract(ABC):
    """
    Abstract Protocol defining remote state hydration, locking, artifact staging,
    and publication for ephemeral runners.
    """

    @abstractmethod
    def acquire_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[PortalControlRecord], Optional[str]]:
        """
        Atomically acquire exclusive lease lock by updating portal_control.json.
        Increments fencing_generation; baseline_generation remains unchanged.
        Returns (acquired, control_record, control_etag).
        """
        pass

    @abstractmethod
    def renew_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Renew active lease lock. Does NOT advance baseline_generation or fencing_generation.
        Returns (success, new_control_etag).
        """
        pass

    @abstractmethod
    def release_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
    ) -> bool:
        """
        Release lease safely by marking lease vacant in portal_control.json without deleting it.
        Preserves fencing_generation, baseline_generation, and active baseline.
        """
        pass

    @abstractmethod
    def hydrate_state(
        self,
        portal_id: str,
        target_dir: str,
    ) -> Tuple[Optional[PortalControlRecord], Optional[str], Optional[ArchiveCatalog], Optional[str], Optional[Dict[str, Any]]]:
        """
        Download portal_control.json, catalog.json, and baseline package files to local target_dir.
        Enforces that portal_control.json is strictly authoritative over catalog.json.
        Returns (control_record, control_etag, catalog, catalog_etag, deposit_registry_data).
        """
        pass

    @abstractmethod
    def stage_package_artifacts(
        self,
        portal_id: str,
        run_id: str,
        local_files: Dict[str, str],  # rel_path -> local_abs_path
    ) -> Dict[str, RemoteArtifactMetadata]:
        """Upload package artifacts to packages/{portal_id}/runs/{run_id}/."""
        pass

    @abstractmethod
    def verify_remote_artifacts(
        self,
        portal_id: str,
        run_id: str,
        expected_checksums: Dict[str, str],  # rel_path -> sha256_hex
    ) -> Tuple[bool, List[str]]:
        """
        Verify all uploaded package artifacts match expected SHA-256 digests by reading actual bytes.
        Returns (is_valid, list_of_mismatch_descriptions).
        """
        pass

    @abstractmethod
    def quarantine_failed_upload(
        self,
        portal_id: str,
        run_id: str,
        runner_id: str,
        reason: str,
        details: Dict[str, Any],
    ) -> str:
        """Write non-destructive quarantine descriptor to state/{portal_id}/quarantine/."""
        pass

    @abstractmethod
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
        """
        Atomically publish new baseline by updating portal_control.json and catalog.json.
        Advances baseline_generation monotonically. portal_control.json is the authoritative commit gate.
        Returns (success, new_control_etag, new_catalog_etag).
        """
        pass

    @abstractmethod
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
        pass

    @abstractmethod
    def sync_deposit_registry(
        self,
        portal_id: str,
        registry_data: Dict[str, Any],
    ) -> bool:
        """Upload updated deposit_registry.json."""
        pass

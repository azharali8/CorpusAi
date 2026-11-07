"""
Zenodo data models and status transition constants.
"""

from dataclasses import asdict, dataclass, field
import datetime
from typing import Any, Dict, Optional

# Status Constants
STATUS_PENDING = "PENDING"
STATUS_DRAFT_CREATED = "DRAFT_CREATED"
STATUS_UPLOADING = "UPLOADING"
STATUS_UPLOADED = "UPLOADED"
STATUS_VERIFIED = "VERIFIED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = "FAILED"
STATUS_NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

VALID_TRANSITIONS = {
    STATUS_PENDING: {STATUS_DRAFT_CREATED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_DRAFT_CREATED: {STATUS_UPLOADING, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_UPLOADING: {STATUS_UPLOADED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_UPLOADED: {STATUS_VERIFIED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_VERIFIED: {STATUS_PUBLISHED, STATUS_FAILED},
    STATUS_FAILED: {
        STATUS_DRAFT_CREATED,
        STATUS_UPLOADING,
        STATUS_UPLOADED,
        STATUS_VERIFIED,
        STATUS_NEEDS_RECONCILIATION,
    },
    STATUS_NEEDS_RECONCILIATION: {
        STATUS_DRAFT_CREATED,
        STATUS_UPLOADING,
        STATUS_UPLOADED,
        STATUS_VERIFIED,
        STATUS_FAILED,
    },
    STATUS_PUBLISHED: set(),  # Terminal state
}


@dataclass
class UploadedArtifactRecord:
    """Metadata for an individual uploaded file on the repository."""

    filename: str
    filesize: int
    local_sha256: str
    local_md5: str
    remote_md5: Optional[str] = None
    remote_checksum: Optional[str] = None
    remote_file_id: Optional[str] = None
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UploadedArtifactRecord":
        return cls(**data)


@dataclass
class DepositReceipt:
    """
    Durable receipt tracking the outcome of an archival package deposit.
    Maintained separately from ArchiveCatalog to prevent deposit failures from
    invalidating valid crawls.
    """

    receipt_id: str
    portal_id: str
    run_id: str
    package_fingerprint: str
    repository_target: str
    environment: str
    status: str
    created_at: str
    updated_at: str
    remote_record_id: Optional[str] = None
    remote_bucket_url: Optional[str] = None
    remote_record_url: Optional[str] = None
    doi: Optional[str] = None
    artifact_inventory: Dict[str, UploadedArtifactRecord] = field(default_factory=dict)
    checksum_verification_outcome: str = "UNVERIFIED"
    last_error: Optional[str] = None
    last_error_category: Optional[str] = None
    retry_eligible: bool = True
    reconciliation_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["artifact_inventory"] = {
            k: v.to_dict() if hasattr(v, "to_dict") else v
            for k, v in self.artifact_inventory.items()
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DepositReceipt":
        cleaned = dict(data)
        inv = cleaned.get("artifact_inventory", {})
        cleaned["artifact_inventory"] = {
            k: UploadedArtifactRecord.from_dict(v) if isinstance(v, dict) else v
            for k, v in inv.items()
        }
        return cls(**cleaned)

    def transition_to(
        self,
        new_status: str,
        error: Optional[str] = None,
        error_category: Optional[str] = None,
    ) -> None:
        """Enforce permitted state transitions."""
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid deposit status transition from '{self.status}' to '{new_status}'."
            )
        self.status = new_status
        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if error:
            self.last_error = error
            self.last_error_category = error_category or "UNKNOWN"

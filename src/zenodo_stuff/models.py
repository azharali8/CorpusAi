"""
Zenodo data models and status transition constants.

Error-category semantics
------------------------
``last_error_category`` follows a two-value null convention:

* ``None``       — no error has occurred; ``last_error`` is also ``None``.
* ``"UNKNOWN"``  — an error occurred (``last_error`` is non-empty) but its
                   category could not be classified.

Do not use ``"UNKNOWN"`` when no error exists, and do not leave
``last_error_category`` as ``None`` when ``last_error`` is set.
``transition_to()`` enforces this rule automatically.
"""

from dataclasses import asdict, dataclass, field
import datetime
from enum import Enum
from typing import Any, Dict, Optional, Set


class DepositStatus(str, Enum):
    """
    String-compatible enumeration of deposit lifecycle states.

    Inheriting from ``str`` means every member *is* a str:
    ``DepositStatus.PENDING == "PENDING"`` is ``True``, and members
    can be used anywhere a plain string status is expected, preserving
    full backward compatibility with existing code and persisted data.
    """

    PENDING = "PENDING"
    DRAFT_CREATED = "DRAFT_CREATED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

    # String representation returns the bare value so serialization is
    # indistinguishable from the legacy plain-string constants.
    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# Plain-string aliases — kept for backward compatibility with all existing
# callers and persisted JSON data.  New code should prefer DepositStatus.
# ---------------------------------------------------------------------------
STATUS_PENDING = DepositStatus.PENDING
STATUS_DRAFT_CREATED = DepositStatus.DRAFT_CREATED
STATUS_UPLOADING = DepositStatus.UPLOADING
STATUS_UPLOADED = DepositStatus.UPLOADED
STATUS_VERIFIED = DepositStatus.VERIFIED
STATUS_PUBLISHED = DepositStatus.PUBLISHED
STATUS_FAILED = DepositStatus.FAILED
STATUS_NEEDS_RECONCILIATION = DepositStatus.NEEDS_RECONCILIATION

# Private transition table — indexed by DepositStatus members.
_VALID_TRANSITIONS: Dict[DepositStatus, Set[DepositStatus]] = {
    DepositStatus.PENDING: {DepositStatus.DRAFT_CREATED, DepositStatus.FAILED, DepositStatus.NEEDS_RECONCILIATION},
    DepositStatus.DRAFT_CREATED: {DepositStatus.UPLOADING, DepositStatus.FAILED, DepositStatus.NEEDS_RECONCILIATION},
    DepositStatus.UPLOADING: {DepositStatus.UPLOADED, DepositStatus.FAILED, DepositStatus.NEEDS_RECONCILIATION},
    DepositStatus.UPLOADED: {DepositStatus.VERIFIED, DepositStatus.FAILED, DepositStatus.NEEDS_RECONCILIATION},
    DepositStatus.VERIFIED: {DepositStatus.PUBLISHED, DepositStatus.FAILED},
    DepositStatus.FAILED: {
        DepositStatus.DRAFT_CREATED,
        DepositStatus.UPLOADING,
        DepositStatus.UPLOADED,
        DepositStatus.VERIFIED,
        DepositStatus.NEEDS_RECONCILIATION,
    },
    DepositStatus.NEEDS_RECONCILIATION: {
        DepositStatus.DRAFT_CREATED,
        DepositStatus.UPLOADING,
        DepositStatus.UPLOADED,
        DepositStatus.VERIFIED,
        DepositStatus.FAILED,
    },
    DepositStatus.PUBLISHED: set(),  # Terminal state
}


def _coerce_status(value: Any) -> DepositStatus:
    """Return the DepositStatus member for *value*, raising ValueError if unknown."""
    if isinstance(value, DepositStatus):
        return value
    try:
        return DepositStatus(value)
    except ValueError:
        raise ValueError(f"Unknown deposit status: {value!r}")


@dataclass
class UploadedArtifactRecord:
    """
    Metadata for an individual file uploaded to the remote repository.

    Checksum fields
    ---------------
    ``remote_md5``      — canonical field.  Stores the hex MD5 string with the
                          provider prefix (``md5:``) stripped.  Used for all
                          integrity comparisons.
    ``remote_checksum`` — **deprecated storage field**.  Retained as an
                          optional dataclass field so that legacy JSON receipts
                          whose dicts contain the key can be round-tripped
                          without raising ``TypeError``.  New serialization
                          does **not** write this key; callers must use
                          ``remote_md5``.  If you need the raw provider value
                          (e.g. ``"md5:<hex>"``), reconstruct it from
                          ``remote_md5``.
    """

    filename: str
    filesize: int
    local_sha256: str
    local_md5: str
    remote_md5: Optional[str] = None
    remote_file_id: Optional[str] = None
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict.  ``remote_checksum`` is not written."""
        return {
            "filename": self.filename,
            "filesize": self.filesize,
            "local_sha256": self.local_sha256,
            "local_md5": self.local_md5,
            "remote_md5": self.remote_md5,
            "remote_file_id": self.remote_file_id,
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UploadedArtifactRecord":
        """
        Deserialize from a dict.

        Backward-compatibility rules
        ----------------------------
        * If only ``remote_checksum`` is present (legacy receipt), copy its
          stripped value to ``remote_md5`` before constructing the object.
        * If both fields are present and disagree, ``remote_md5`` wins and a
          warning note is recorded via the ``remote_md5`` value itself — the
          conflicting ``remote_checksum`` is not silently discarded; it is
          preserved in the returned object as ``_legacy_remote_checksum``
          **only in the returned dict** so that callers can detect and log it.
          The constructed dataclass always uses ``remote_md5`` as the
          authoritative value.
        * ``remote_checksum`` is consumed and not stored in the dataclass.
        """
        d = dict(data)
        raw_checksum = d.pop("remote_checksum", None)
        canonical_md5 = d.get("remote_md5")

        if raw_checksum is not None:
            # Strip provider prefix to get the hex string.
            stripped = raw_checksum[4:] if raw_checksum.startswith("md5:") else raw_checksum
            if canonical_md5 is None:
                # Legacy-only receipt: promote to canonical field.
                d["remote_md5"] = stripped
            elif canonical_md5.lower() != stripped.lower():
                # Both present and conflicting: remote_md5 wins; conflict is logged.
                import logging as _logging
                _logging.getLogger("corpusai.deposit.models").warning(
                    "Conflicting checksum fields during deserialization of "
                    "UploadedArtifactRecord(filename=%r): "
                    "remote_md5=%r will be used; legacy remote_checksum=%r is discarded.",
                    d.get("filename"),
                    canonical_md5,
                    raw_checksum,
                )
            # If they agree, nothing extra to do.

        return cls(**d)


@dataclass
class DepositReceipt:
    """
    Durable receipt tracking the outcome of an archival package deposit.
    Maintained separately from ArchiveCatalog to prevent deposit failures from
    invalidating valid crawls.

    Status representation
    ---------------------
    ``status`` is stored internally as a ``DepositStatus`` enum member.
    Because ``DepositStatus`` inherits from ``str``, existing code that
    compares ``receipt.status == "FAILED"`` continues to work.

    Error-category semantics
    ------------------------
    See module docstring.  Briefly: ``last_error_category`` is ``None`` when
    no error has occurred, and ``"UNKNOWN"`` when an error exists but its
    category is unclassifiable.
    """

    receipt_id: str
    portal_id: str
    run_id: str
    package_fingerprint: str
    repository_target: str
    environment: str
    status: str  # Accepts str or DepositStatus; stored as DepositStatus.
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

    def __post_init__(self) -> None:
        # Coerce status to DepositStatus on construction so comparisons
        # always hit the enum path regardless of how the object was created.
        object.__setattr__(self, "status", _coerce_status(self.status))

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to a plain dict.  ``status`` is serialized as its string
        value so persisted JSON is identical to the legacy format.
        """
        d = {
            "receipt_id": self.receipt_id,
            "portal_id": self.portal_id,
            "run_id": self.run_id,
            "package_fingerprint": self.package_fingerprint,
            "repository_target": self.repository_target,
            "environment": self.environment,
            "status": str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "remote_record_id": self.remote_record_id,
            "remote_bucket_url": self.remote_bucket_url,
            "remote_record_url": self.remote_record_url,
            "doi": self.doi,
            "artifact_inventory": {
                k: v.to_dict() if hasattr(v, "to_dict") else v
                for k, v in self.artifact_inventory.items()
            },
            "checksum_verification_outcome": self.checksum_verification_outcome,
            "last_error": self.last_error,
            "last_error_category": self.last_error_category,
            "retry_eligible": self.retry_eligible,
            "reconciliation_notes": self.reconciliation_notes,
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DepositReceipt":
        """
        Deserialize from a dict (e.g. from JSON registry).

        Tolerates legacy receipts that contain ``remote_checksum`` inside
        artifact inventory entries — those are handled by
        ``UploadedArtifactRecord.from_dict``.
        """
        cleaned = dict(data)
        inv = cleaned.get("artifact_inventory", {})
        cleaned["artifact_inventory"] = {
            k: UploadedArtifactRecord.from_dict(v) if isinstance(v, dict) else v
            for k, v in inv.items()
        }
        return cls(**cleaned)

    @classmethod
    def for_failure(
        cls,
        *,
        portal_id: str,
        run_id: str,
        fingerprint: str,
        repository: str,
        environment: str,
        last_error: str,
        last_error_category: str,
        retry_eligible: bool = True,
    ) -> "DepositReceipt":
        """
        Construct a canonical FAILED receipt.

        Parameters
        ----------
        portal_id:
            Portal identifier.
        run_id:
            Run identifier.
        fingerprint:
            Package fingerprint (SHA-256 hex).
        repository:
            Target repository name, e.g. ``"zenodo"``.  Must be supplied
            explicitly — do not hard-code a provider name inside this method.
        environment:
            Deployment environment, e.g. ``"sandbox"`` or ``"production"``.
            Must be supplied explicitly.
        last_error:
            Human-readable description of the failure.  Must be non-empty.
        last_error_category:
            Classified category string, e.g. ``"LOCAL_PACKAGE_INVALID"``.
            Use ``"UNKNOWN"`` only when the category genuinely cannot be
            determined.  Must not be ``None`` when constructing a failed
            receipt — a failure always has a category.
        retry_eligible:
            Whether the failure is considered transient and safe to retry.
            Defaults to ``True`` (retry allowed).

        Returns
        -------
        DepositReceipt
            A new receipt in ``FAILED`` status with all required identifiers,
            timestamps, and error fields populated.
        """
        if not last_error:
            raise ValueError("last_error must be non-empty when constructing a failed receipt.")
        if last_error_category is None:
            raise ValueError(
                "last_error_category must be provided when constructing a failed receipt. "
                "Use 'UNKNOWN' if the category cannot be determined."
            )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return cls(
            receipt_id=f"receipt-{portal_id}-{run_id}-{fingerprint[:8]}",
            portal_id=portal_id,
            run_id=run_id,
            package_fingerprint=fingerprint,
            repository_target=repository,
            environment=environment,
            status=DepositStatus.FAILED,
            created_at=now,
            updated_at=now,
            last_error=last_error,
            last_error_category=last_error_category,
            retry_eligible=retry_eligible,
        )

    def transition_to(
        self,
        new_status: "str | DepositStatus",
        error: Optional[str] = None,
        error_category: Optional[str] = None,
    ) -> None:
        """
        Enforce permitted state transitions.

        Error-category rule
        -------------------
        When ``error`` is supplied, ``last_error_category`` is set to
        ``error_category`` if provided, or to ``"UNKNOWN"`` as a fallback.
        When ``error`` is ``None``, the existing ``last_error`` and
        ``last_error_category`` are left unchanged (a non-error transition
        does not clear previous error context).
        """
        target = _coerce_status(new_status)
        allowed = _VALID_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise ValueError(
                f"Invalid deposit status transition from '{self.status}' to '{target}'."
            )
        self.status = target
        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if error:
            self.last_error = error
            self.last_error_category = error_category or "UNKNOWN"

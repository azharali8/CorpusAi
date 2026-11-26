"""
Backward-compatible shim for the Zenodo deposit subsystem.

Canonical implementations now live in ``src.zenodo_stuff``:
- ``src.zenodo_stuff.client``: ``ZenodoSandboxAdapter`` and Zenodo constants
- ``src.zenodo_stuff.client_contract``: ``RepositoryDepositClient``
- ``src.zenodo_stuff.fingerprint``: ``compute_package_fingerprint``
- ``src.zenodo_stuff.models``: ``DepositReceipt``, ``UploadedArtifactRecord``, status constants
- ``src.zenodo_stuff.orchestrator``: ``DepositOrchestrator``
- ``src.zenodo_stuff.registry``: ``DepositRegistry``
"""

from src.zenodo_stuff.client import (
    ZENODO_MAX_RECORD_BYTES,
    ZENODO_MAX_RECORD_FILES,
    ZENODO_MAX_SINGLE_FILE_BYTES,
    ZENODO_PRODUCTION_BASE_URL,
    ZENODO_SANDBOX_BASE_URL,
    ZenodoSandboxAdapter,
)
from src.zenodo_stuff.client_contract import RepositoryDepositClient
from src.zenodo_stuff.fingerprint import compute_package_fingerprint
from src.zenodo_stuff.models import (
    DepositReceipt,
    DepositStatus,
    STATUS_DRAFT_CREATED,
    STATUS_FAILED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    STATUS_VERIFIED,
    UploadedArtifactRecord,
)
from src.zenodo_stuff.orchestrator import DepositOrchestrator
from src.zenodo_stuff.registry import DepositRegistry

__all__ = [
    "DepositOrchestrator",
    "DepositReceipt",
    "DepositRegistry",
    "DepositStatus",
    "RepositoryDepositClient",
    "STATUS_DRAFT_CREATED",
    "STATUS_FAILED",
    "STATUS_NEEDS_RECONCILIATION",
    "STATUS_PENDING",
    "STATUS_PUBLISHED",
    "STATUS_UPLOADED",
    "STATUS_UPLOADING",
    "STATUS_VERIFIED",
    "UploadedArtifactRecord",
    "ZENODO_MAX_RECORD_BYTES",
    "ZENODO_MAX_RECORD_FILES",
    "ZENODO_MAX_SINGLE_FILE_BYTES",
    "ZENODO_PRODUCTION_BASE_URL",
    "ZENODO_SANDBOX_BASE_URL",
    "ZenodoSandboxAdapter",
    "compute_package_fingerprint",
]

"""
Archive Run metadata model for CorpusAI.

Defines typed, deterministic metadata structures for crawl and replay executions,
preserving provenance, configuration hashes, artifact references, and review statuses.
"""

from dataclasses import asdict, dataclass, field
import datetime
import hashlib
import json
import os
from typing import Any, Dict, List, Optional


def compute_config_hash(config: Dict[str, Any]) -> str:
    """Compute deterministic short SHA-256 hash of configuration parameters."""
    serialized = json.dumps(config, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:8]


def generate_run_id(timestamp_str: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> str:
    """
    Generate a unique, traceable run ID: <UTC-compact-timestamp>-<short-config-hash>.
    Combines timestamp with a deterministic configuration hash while avoiding personal machine details.
    """
    if not timestamp_str:
        timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    else:
        # Standardize if ISO string was provided
        try:
            dt = datetime.datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            timestamp_str = dt.strftime("%Y%m%dT%H%M%SZ")
        except Exception:
            pass

    cfg_hash = compute_config_hash(config or {})
    return f"run-{timestamp_str}-{cfg_hash}"


@dataclass
class ArchiveRun:
    """Represents a single executable crawl or archival run."""
    run_id: str
    target_urls: List[str]
    scope: str = "page"  # page, domain, prefix, graph
    crawler: str = "browsertrix-crawler"
    crawler_version: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    completed_at: Optional[str] = None
    configuration: Dict[str, Any] = field(default_factory=dict)
    output_artifacts: Dict[str, str] = field(default_factory=dict)  # relative paths
    checksums: Dict[str, str] = field(default_factory=dict)  # relative path -> sha256
    previous_run_id: Optional[str] = None
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED, RESOURCE_CONSTRAINT
    errors: List[str] = field(default_factory=list)
    manual_review_status: str = "PENDING"  # PENDING, VERIFIED, REJECTED, NOT_APPLICABLE

    def to_dict(self) -> Dict[str, Any]:
        """Convert ArchiveRun to a dictionary with relative paths and clean serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveRun":
        """Deserialize dictionary into an ArchiveRun instance."""
        return cls(**data)

    def save_to_json(self, file_path: str) -> None:
        """Persist ArchiveRun metadata to JSON file."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load_from_json(cls, file_path: str) -> "ArchiveRun":
        """Load ArchiveRun from JSON file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"ArchiveRun file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def mark_completed(self, artifacts: Dict[str, str], checksums: Dict[str, str]) -> None:
        """Mark run as successfully completed."""
        self.status = "COMPLETED"
        self.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.output_artifacts.update(artifacts)
        self.checksums.update(checksums)

    def mark_failed(self, error_message: str, status: str = "FAILED") -> None:
        """Mark run as failed or stopped due to resource constraints."""
        self.status = status
        self.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.errors.append(error_message)

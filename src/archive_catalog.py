"""
Archive Catalog module for CorpusAI Phase 6B.

Manages persistent multi-run catalog indexing for a portal, tracking the lineage
of all historical runs and the latest successful run pointer with atomic write semantics.
"""

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
from typing import Any, Dict, List, Optional


@dataclass
class CatalogRunEntry:
    """Summary of a run inside the catalog."""
    run_id: str
    previous_run_id: Optional[str]
    mode: str                             # BASELINE, INCREMENTAL
    started_at: str
    completed_at: Optional[str]
    status: str                           # CONVERGED, FAILED, BUDGET_EXHAUSTED, etc.
    article_count: int
    new_article_count: int
    changed_article_count: int
    run_dir: str                          # Relative path e.g. runs/<run-id>/
    manual_review_status: str = "PENDING"
    anomalies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CatalogRunEntry":
        import dataclasses
        valid_keys = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ArchiveCatalog:
    """
    Tracks all crawl runs for a target portal and points to the latest successful run.
    """
    portal_id: str
    latest_successful_run_id: Optional[str] = None
    first_capture: Optional[str] = None
    latest_capture: Optional[str] = None
    all_run_ids: List[str] = field(default_factory=list)
    runs: Dict[str, CatalogRunEntry] = field(default_factory=dict)  # run_id -> CatalogRunEntry
    schema_version: str = "1"
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portal_id": self.portal_id,
            "latest_successful_run_id": self.latest_successful_run_id,
            "first_capture": self.first_capture,
            "latest_capture": self.latest_capture,
            "all_run_ids": self.all_run_ids,
            "runs": {k: v.to_dict() for k, v in self.runs.items()},
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchiveCatalog":
        runs = {
            k: CatalogRunEntry.from_dict(v) if isinstance(v, dict) else v
            for k, v in data.get("runs", {}).items()
        }
        return cls(
            portal_id=data["portal_id"],
            latest_successful_run_id=data.get("latest_successful_run_id"),
            first_capture=data.get("first_capture"),
            latest_capture=data.get("latest_capture"),
            all_run_ids=data.get("all_run_ids", []),
            runs=runs,
            schema_version=data.get("schema_version", "1"),
            updated_at=data.get("updated_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )

    def register_run(self, entry: CatalogRunEntry, is_successful: bool = True) -> None:
        """
        Register a new crawl run entry.
        Only advances `latest_successful_run_id` if `is_successful` is True (e.g. CONVERGED).
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.updated_at = now

        if entry.run_id not in self.all_run_ids:
            self.all_run_ids.append(entry.run_id)

        self.runs[entry.run_id] = entry

        if not self.first_capture:
            self.first_capture = entry.started_at
        self.latest_capture = entry.completed_at or entry.started_at

        # Crucial safety rule: Failed runs MUST NEVER advance latest_successful_run_id
        if is_successful and entry.status in ("CONVERGED", "COMPLETED", "PILOT_SCOPE_CONVERGED", "MULTIPAGE_PILOT_COMPLETE"):
            self.latest_successful_run_id = entry.run_id

    def save_to_json(self, file_path: str) -> None:
        """Persist catalog atomically using tempfile + replace."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        tmp_path = f"{file_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    @classmethod
    def load_from_json(cls, file_path: str) -> "ArchiveCatalog":
        """Load catalog from JSON file, or create a fresh default catalog if missing."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Catalog file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def load_or_create(cls, file_path: str, portal_id: str) -> "ArchiveCatalog":
        """Load existing catalog or create a new empty catalog for the portal."""
        if os.path.exists(file_path):
            return cls.load_from_json(file_path)
        return cls(portal_id=portal_id)

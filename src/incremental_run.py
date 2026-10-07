"""
Incremental Run metadata model and result structures for CorpusAI Phase 6B.

Defines typed dataclasses for baseline and incremental crawl executions,
preserving run-to-run provenance, change metrics, anomaly detections, and run lineage.
"""

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
from typing import Any, Dict, List, Optional


@dataclass
class IncrementalRun:
    """Represents an incremental or baseline crawl run with lineage and change statistics."""
    run_id: str
    target_portal: str
    seed_urls: List[str]
    previous_run_id: Optional[str] = None
    mode: str = "BASELINE"                # BASELINE, INCREMENTAL
    started_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    completed_at: Optional[str] = None
    crawler_version: str = "corpusai-0.1.0"
    config_hash: str = ""
    crawl_scope: str = "archive_traversal"

    # Metric counts
    archive_page_count: int = 0
    pages_revisited: int = 0
    article_count: int = 0
    new_article_count: int = 0
    unchanged_article_count: int = 0
    changed_article_count: int = 0
    metadata_changed_article_count: int = 0
    missing_article_count: int = 0
    reordered_article_count: int = 0
    fetch_failure_count: int = 0
    requests_made: int = 0

    # Convergence & Boundary status
    convergence_status: str = "IN_PROGRESS"  # PILOT_SCOPE_CONVERGED, MULTIPAGE_PILOT_COMPLETE, CONVERGED, INCREMENTAL_CONVERGENCE_NOT_REACHED, BUDGET_EXHAUSTED, UNSTABLE_AT_TERMINATION
    scope_completion: str = "PILOT_SCOPE_CONVERGED"  # PILOT_SCOPE_CONVERGED, MULTIPAGE_PILOT_COMPLETE, FULL_PORTAL_CONVERGED, INCOMPLETE
    full_portal_coverage: bool = False
    boundary_status: str = "NOT_EVALUATED"   # BOUNDARY_RECONCILED, BOUNDARY_SHIFT_DETECTED, BOUNDARY_INCOMPLETE
    head_changed: bool = False

    # Anomalies & Review
    anomalies_detected: List[str] = field(default_factory=list)  # PORTAL_STRUCTURE_CHANGED, ROBOTS_POLICY_CHANGED, etc.
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manual_review_status: str = "PENDING"    # PENDING, VERIFIED, MANUAL_REVIEW_REQUIRED, NOT_APPLICABLE

    # Artifacts & Checksums (relative paths)
    artifact_paths: Dict[str, str] = field(default_factory=dict)
    checksums: Dict[str, str] = field(default_factory=dict)
    schema_version: str = "1"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IncrementalRun":
        import dataclasses
        valid_keys = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    def save_to_json(self, file_path: str) -> None:
        """Atomically persist IncrementalRun metadata to JSON."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        tmp_path = f"{file_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    @classmethod
    def load_from_json(cls, file_path: str) -> "IncrementalRun":
        """Load IncrementalRun from JSON file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"IncrementalRun file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def mark_completed(
        self,
        convergence_status: str = "CONVERGED",
        artifacts: Optional[Dict[str, str]] = None,
        checksums: Optional[Dict[str, str]] = None,
    ) -> None:
        """Mark run as finished, recording completion time and artifact mappings."""
        self.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.convergence_status = convergence_status
        if artifacts:
            self.artifact_paths.update(artifacts)
        if checksums:
            self.checksums.update(checksums)
        if self.anomalies_detected and self.manual_review_status == "PENDING":
            self.manual_review_status = "MANUAL_REVIEW_REQUIRED"

    def mark_failed(self, error_message: str, status: str = "FAILED") -> None:
        """Mark run as failed with explicit error."""
        self.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.convergence_status = status
        self.errors.append(error_message)
        self.manual_review_status = "MANUAL_REVIEW_REQUIRED"

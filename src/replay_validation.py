"""
Replay Validation and QA Analysis module for CorpusAI.

Validates archive manifests, verifies SHA-256 integrity, inspects resource
inventories, evaluates Browsertrix QA summaries, and tracks manual replay statuses.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from typing import Any, Dict, List, Optional


def compute_file_sha256(file_path: str) -> str:
    """Compute SHA-256 digest of a local file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ReplayValidationReport:
    """Represents a structured evaluation of replay readiness and archive integrity."""
    target_url: str
    manifest_valid: bool
    status: str  # ARCHIVE_CREATED, ARCHIVE_HASH_VALID, QA_COMPLETED, QA_FAILED, MANUAL_REPLAY_PENDING, MANUAL_REPLAY_VERIFIED, REPLAY_INCOMPLETE
    archive_artifacts: Dict[str, str] = field(default_factory=dict)
    checksums_verified: bool = False
    checksum_errors: List[str] = field(default_factory=list)
    qa_summary: Optional[Dict[str, Any]] = None
    manual_replay_verified: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReplayValidator:
    """Validates archive manifests and evaluates replay verification readiness."""

    @staticmethod
    def validate_manifest(
        manifest_data: Dict[str, Any],
        base_dir: str = "",
        explicit_manual_confirmation: bool = False,
    ) -> ReplayValidationReport:
        """
        Validate an archive manifest against filesystem artifacts and checksums.
        Ensures `manual_replay_verified` CANNOT become True without explicit manual confirmation.
        """
        target_url = manifest_data.get("article_url") or manifest_data.get("target_url") or ""
        errors = []
        checksum_errors = []
        artifacts = manifest_data.get("artifacts", {})
        checksums = manifest_data.get("checksums", {})

        if not target_url:
            errors.append("Target URL missing from manifest.")

        # Check artifact existence and verify checksums
        all_exist = True
        for name, rel_path in artifacts.items():
            full_path = os.path.join(base_dir, rel_path) if base_dir else rel_path
            if not os.path.exists(full_path):
                errors.append(f"Referenced artifact not found on disk: {rel_path}")
                all_exist = False
                continue

            expected_hash = checksums.get(rel_path) or checksums.get(name)
            if expected_hash:
                actual_hash = compute_file_sha256(full_path)
                if actual_hash.lower() != expected_hash.lower():
                    checksum_errors.append(
                        f"Checksum mismatch for {rel_path}: expected {expected_hash}, calculated {actual_hash}"
                    )

        checksums_verified = (len(checksum_errors) == 0 and len(checksums) > 0 and all_exist)

        # Status determination
        if errors:
            status = "REPLAY_INCOMPLETE"
        elif not checksums_verified:
            status = "REPLAY_INCOMPLETE" if not all_exist else "ARCHIVE_CREATED"
        else:
            status = "ARCHIVE_HASH_VALID"

        # Explicit manual review boundary:
        # If archive hash is valid, status transitions to MANUAL_REPLAY_PENDING
        # (or MANUAL_REPLAY_VERIFIED if and only if explicit manual confirmation is provided).
        is_manual_verified = False
        if status in ["ARCHIVE_HASH_VALID", "ARCHIVE_CREATED"]:
            if explicit_manual_confirmation:
                status = "MANUAL_REPLAY_VERIFIED"
                is_manual_verified = True
            else:
                status = "MANUAL_REPLAY_PENDING"
                is_manual_verified = False

        qa_data = manifest_data.get("qa_summary")

        return ReplayValidationReport(
            target_url=target_url,
            manifest_valid=(len(errors) == 0),
            status=status,
            archive_artifacts=artifacts,
            checksums_verified=checksums_verified,
            checksum_errors=checksum_errors,
            qa_summary=qa_data,
            manual_replay_verified=is_manual_verified,
            errors=errors,
        )


def generate_sha256_manifest(files_dict: Dict[str, str], base_dir: str = "") -> str:
    """
    Generate standard sha256 checksum lines: `<sha256>  <relative-path>`
    """
    lines = []
    for rel_path in sorted(files_dict.keys()):
        full_path = os.path.join(base_dir, rel_path) if base_dir else rel_path
        digest = compute_file_sha256(full_path)
        # Use forward slashes for portable checksum files
        clean_rel = rel_path.replace("\\", "/")
        lines.append(f"{digest}  {clean_rel}")
    return "\n".join(lines) + "\n"

"""
Archive Package module for CorpusAI Phase 7.

Implements verified archival run packaging according to docs/archive_storage_design.md:
- Deterministic run directory layout: archives/<portal_id>/runs/<run_id>/
- Clear separation of content (content/) vs replay (replay/) assets
- Generation of machine-readable manifest.json and environment.json
- Tamper-evident SHA-256 checksums.sha256 calculation and verification
- Atomic staging and promotion preventing corruption or overwriting of finalized runs
- Comprehensive verification detecting missing, modified, unexpected, or corrupt files
"""

from dataclasses import asdict, dataclass, field
import datetime
import hashlib
import json
import os
import platform
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.archive_run import ArchiveRun, compute_config_hash, generate_run_id
from src.deposit_metadata import DepositMetadata
from src.replay_validation import compute_file_sha256


# Package Layout Constants
CONTENT_DIR = "content"
REPLAY_DIR = "replay"
SCREENSHOTS_DIR = os.path.join("replay", "screenshots")
QA_DIR = "qa"

MANIFEST_FILENAME = "manifest.json"
ENVIRONMENT_FILENAME = "environment.json"
CHECKSUMS_FILENAME = "checksums.sha256"
DEPOSIT_METADATA_FILENAME = "deposit_metadata.json"

CONTENT_HTML_WARC = "content_html.warc.gz"
CONTENT_JSON_WARC = "content_json.warc.gz"
REPLAY_WACZ = "archive.wacz"


@dataclass
class PackageVerificationResult:
    """Detailed machine-readable report of package verification."""
    is_valid: bool
    run_id: str
    portal_id: str
    package_dir: str
    verified_files: List[str] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)
    checksum_mismatches: List[str] = field(default_factory=list)
    unexpected_files: List[str] = field(default_factory=list)
    metadata_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def capture_environment_metadata(
    crawler_name: str = "CorpusAI",
    crawler_version: str = "0.1.0",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Produce a deterministic environment description avoiding personal/machine secrets.
    """
    env_info: Dict[str, Any] = {
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "crawler_name": crawler_name,
        "crawler_version": crawler_version,
    }
    if extra_metadata:
        for k, v in extra_metadata.items():
            if k not in env_info:
                env_info[k] = v
    return env_info


class ArchivePackager:
    """
    Manages building, verifying, promoting, and maintaining durable run packages
    under archives/<portal_id>/runs/<run_id>/.
    """

    def __init__(self, archives_root: str):
        self.archives_root = os.path.abspath(archives_root)

    def get_portal_dir(self, portal_id: str) -> str:
        return os.path.join(self.archives_root, portal_id)

    def get_runs_dir(self, portal_id: str) -> str:
        return os.path.join(self.get_portal_dir(portal_id), "runs")

    def get_run_dir(self, portal_id: str, run_id: str) -> str:
        return os.path.join(self.get_runs_dir(portal_id), run_id)

    def package_run(
        self,
        portal_id: str,
        run_id: str,
        mode: str,
        status: str,
        started_at: str,
        completed_at: Optional[str] = None,
        target_urls: Optional[List[str]] = None,
        previous_run_id: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
        content_html_warc: Optional[str] = None,
        content_json_warc: Optional[str] = None,
        replay_wacz: Optional[str] = None,
        screenshots: Optional[Dict[str, str]] = None,  # filename -> source_path
        qa_files: Optional[Dict[str, str]] = None,       # filename -> source_path
        extra_artifacts: Optional[Dict[str, str]] = None,# rel_dest_path -> source_path
        crawler_name: str = "CorpusAI",
        crawler_version: str = "0.1.0",
        deposit_metadata: Optional[DepositMetadata] = None,
        catalog: Optional[ArchiveCatalog] = None,
        article_count: int = 0,
        new_article_count: int = 0,
        changed_article_count: int = 0,
        anomalies: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
        custom_manifest_fields: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, PackageVerificationResult]:
        """
        Build an archival run package in a staging directory, verify its integrity,
        and atomically promote it to archives/<portal_id>/runs/<run_id>/.

        Refuses to overwrite an existing finalized run directory.
        """
        final_run_dir = self.get_run_dir(portal_id, run_id)
        if os.path.exists(final_run_dir):
            raise FileExistsError(
                f"Cannot package run '{run_id}': run directory already exists and is immutable: {final_run_dir}"
            )

        runs_parent = self.get_runs_dir(portal_id)
        os.makedirs(runs_parent, exist_ok=True)

        staging_dir = tempfile.mkdtemp(prefix=f".stage_{run_id}_", dir=runs_parent)

        try:
            os.makedirs(os.path.join(staging_dir, CONTENT_DIR), exist_ok=True)
            os.makedirs(os.path.join(staging_dir, REPLAY_DIR), exist_ok=True)

            inventory: Dict[str, str] = {}  # artifact_key -> relative_path
            copied_files: Set[str] = set()   # relative_path

            # 1. Content Stream
            if content_html_warc and os.path.exists(content_html_warc):
                rel_path = f"{CONTENT_DIR}/{CONTENT_HTML_WARC}"
                dest = os.path.join(staging_dir, rel_path)
                shutil.copy2(content_html_warc, dest)
                inventory["content_html_warc"] = rel_path
                copied_files.add(rel_path)

            if content_json_warc and os.path.exists(content_json_warc):
                rel_path = f"{CONTENT_DIR}/{CONTENT_JSON_WARC}"
                dest = os.path.join(staging_dir, rel_path)
                shutil.copy2(content_json_warc, dest)
                inventory["content_json_warc"] = rel_path
                copied_files.add(rel_path)

            # 2. Replay Stream
            if replay_wacz and os.path.exists(replay_wacz):
                rel_path = f"{REPLAY_DIR}/{REPLAY_WACZ}"
                dest = os.path.join(staging_dir, rel_path)
                shutil.copy2(replay_wacz, dest)
                inventory["replay_wacz"] = rel_path
                copied_files.add(rel_path)

            if screenshots:
                shots_dest = os.path.join(staging_dir, SCREENSHOTS_DIR)
                os.makedirs(shots_dest, exist_ok=True)
                for shot_name, shot_src in screenshots.items():
                    if os.path.exists(shot_src):
                        rel_path = f"{SCREENSHOTS_DIR}/{shot_name}".replace("\\", "/")
                        dest = os.path.join(staging_dir, rel_path)
                        shutil.copy2(shot_src, dest)
                        copied_files.add(rel_path)

            if qa_files:
                qa_dest = os.path.join(staging_dir, QA_DIR)
                os.makedirs(qa_dest, exist_ok=True)
                for qa_name, qa_src in qa_files.items():
                    if os.path.exists(qa_src):
                        rel_path = f"{QA_DIR}/{qa_name}".replace("\\", "/")
                        dest = os.path.join(staging_dir, rel_path)
                        shutil.copy2(qa_src, dest)
                        copied_files.add(rel_path)

            if extra_artifacts:
                for rel_dest, src_path in extra_artifacts.items():
                    if os.path.exists(src_path):
                        clean_rel = rel_dest.replace("\\", "/").lstrip("/")
                        dest = os.path.join(staging_dir, clean_rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(src_path, dest)
                        copied_files.add(clean_rel)

            # 3. Environment Metadata
            env_metadata = capture_environment_metadata(
                crawler_name=crawler_name,
                crawler_version=crawler_version,
            )
            env_path = os.path.join(staging_dir, ENVIRONMENT_FILENAME)
            with open(env_path, "w", encoding="utf-8") as f:
                json.dump(env_metadata, f, indent=2, ensure_ascii=False)
            inventory["environment"] = ENVIRONMENT_FILENAME
            copied_files.add(ENVIRONMENT_FILENAME)

            # 4. Optional Deposit Metadata
            if deposit_metadata:
                dep_path = os.path.join(staging_dir, DEPOSIT_METADATA_FILENAME)
                deposit_metadata.save_to_json(dep_path)
                inventory["deposit_metadata"] = DEPOSIT_METADATA_FILENAME
                copied_files.add(DEPOSIT_METADATA_FILENAME)

            # 5. Manifest
            manifest_data: Dict[str, Any] = {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "portal_id": portal_id,
                "mode": mode,
                "status": status,
                "target_urls": target_urls or [],
                "previous_run_id": previous_run_id,
                "started_at": started_at,
                "completed_at": completed_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "configuration": configuration or {},
                "config_hash": compute_config_hash(configuration or {}),
                "article_count": article_count,
                "new_article_count": new_article_count,
                "changed_article_count": changed_article_count,
                "anomalies": anomalies or [],
                "errors": errors or [],
                "inventory": inventory,
            }
            if custom_manifest_fields:
                manifest_data.update(custom_manifest_fields)

            manifest_path = os.path.join(staging_dir, MANIFEST_FILENAME)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, indent=2, ensure_ascii=False)
            inventory["manifest"] = MANIFEST_FILENAME
            copied_files.add(MANIFEST_FILENAME)

            # 6. Generate Checksums (checksums.sha256 covers all files except itself)
            checksum_map: Dict[str, str] = {}
            for rel in sorted(copied_files):
                full_f = os.path.join(staging_dir, rel)
                checksum_map[rel] = compute_file_sha256(full_f)

            checksums_path = os.path.join(staging_dir, CHECKSUMS_FILENAME)
            with open(checksums_path, "w", encoding="utf-8") as f:
                for rel in sorted(checksum_map.keys()):
                    f.write(f"{checksum_map[rel]}  {rel}\n")

            # 7. Verification inside staging before promotion
            verification = (PackageVerifier.verify if hasattr(PackageVerifier, "verify") else PackageVerifier.verify_package)(staging_dir)
            if not verification.is_valid:
                raise ValueError(
                    f"Package integrity verification failed during staging: "
                    f"missing={verification.missing_files}, mismatches={verification.checksum_mismatches}, "
                    f"unexpected={verification.unexpected_files}, errors={verification.metadata_errors}"
                )

            # 8. Atomic Promotion
            # Use os.rename/replace to promote directory
            try:
                os.replace(staging_dir, final_run_dir)
            except OSError:
                # Fallback for Windows cross-volume/temp replace if needed
                shutil.move(staging_dir, final_run_dir)

            # 9. Register with ArchiveCatalog if supplied
            if catalog is not None:
                is_success = status in ("COMPLETED", "CONVERGED", "PILOT_SCOPE_CONVERGED", "MULTIPAGE_PILOT_COMPLETE")
                rel_run_dir = f"runs/{run_id}/"
                entry = CatalogRunEntry(
                    run_id=run_id,
                    previous_run_id=previous_run_id,
                    mode=mode,
                    started_at=started_at,
                    completed_at=completed_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    status=status,
                    article_count=article_count,
                    new_article_count=new_article_count,
                    changed_article_count=changed_article_count,
                    run_dir=rel_run_dir,
                    anomalies=anomalies or [],
                )
                catalog.register_run(entry, is_successful=is_success)
                catalog_file = os.path.join(self.get_portal_dir(portal_id), "catalog.json")
                catalog.save_to_json(catalog_file)

            # Re-verify at promoted destination
            final_verification = (PackageVerifier.verify if hasattr(PackageVerifier, "verify") else PackageVerifier.verify_package)(final_run_dir)
            return final_run_dir, final_verification

        except Exception:
            # Clean up staging directory on any error to avoid leaving half-baked packages
            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise


class PackageVerifier:
    """
    Independent verifier for finalized archival packages.
    """

    @staticmethod
    def verify_package(package_dir: str) -> PackageVerificationResult:
        package_dir = os.path.abspath(package_dir)
        if not os.path.isdir(package_dir):
            return PackageVerificationResult(
                is_valid=False,
                run_id="",
                portal_id="",
                package_dir=package_dir,
                metadata_errors=[f"Package directory does not exist or is not a directory: {package_dir}"],
            )

        manifest_path = os.path.join(package_dir, MANIFEST_FILENAME)
        checksums_path = os.path.join(package_dir, CHECKSUMS_FILENAME)
        env_path = os.path.join(package_dir, ENVIRONMENT_FILENAME)

        metadata_errors: List[str] = []
        missing_files: List[str] = []
        mismatches: List[str] = []
        unexpected_files: List[str] = []
        verified_files: List[str] = []

        # 1. Manifest verification
        run_id = os.path.basename(package_dir.rstrip("/\\"))
        portal_id = ""

        if not os.path.exists(manifest_path):
            metadata_errors.append(f"Missing mandatory manifest file: {MANIFEST_FILENAME}")
        else:
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                run_id = manifest_data.get("run_id", run_id)
                portal_id = manifest_data.get("portal_id", "")
                if not run_id:
                    metadata_errors.append("Manifest missing 'run_id'")
                if not portal_id:
                    metadata_errors.append("Manifest missing 'portal_id'")
            except Exception as e:
                metadata_errors.append(f"Invalid manifest JSON: {str(e)}")

        # 2. Environment metadata verification
        if not os.path.exists(env_path):
            metadata_errors.append(f"Missing mandatory environment file: {ENVIRONMENT_FILENAME}")
        else:
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    env_data = json.load(f)
                if not isinstance(env_data, dict) or "python_version" not in env_data:
                    metadata_errors.append("environment.json missing expected metadata fields")
            except Exception as e:
                metadata_errors.append(f"Invalid environment JSON: {str(e)}")

        # 3. Checksums verification
        recorded_checksums: Dict[str, str] = {}
        if not os.path.exists(checksums_path):
            metadata_errors.append(f"Missing mandatory checksums file: {CHECKSUMS_FILENAME}")
        else:
            try:
                with open(checksums_path, "r", encoding="utf-8") as f:
                    for line_idx, line in enumerate(f, start=1):
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(None, 1)
                        if len(parts) != 2:
                            metadata_errors.append(f"Malformed checksum line {line_idx}: {line}")
                            continue
                        digest, rel_path = parts[0], parts[1].strip()
                        clean_rel = rel_path.replace("\\", "/").lstrip("./")
                        if clean_rel == CHECKSUMS_FILENAME:
                            metadata_errors.append("checksums.sha256 must not contain its own hash")
                            continue
                        recorded_checksums[clean_rel] = digest.lower()
            except Exception as e:
                metadata_errors.append(f"Failed reading checksums file: {str(e)}")

        # Check all recorded checksum files exist and calculate hash
        for rel_path, expected_digest in recorded_checksums.items():
            full_path = os.path.join(package_dir, rel_path)
            if not os.path.exists(full_path):
                missing_files.append(rel_path)
                continue

            try:
                actual_digest = compute_file_sha256(full_path).lower()
                if actual_digest != expected_digest:
                    mismatches.append(
                        f"{rel_path}: expected {expected_digest}, computed {actual_digest}"
                    )
                else:
                    verified_files.append(rel_path)
            except Exception as e:
                mismatches.append(f"{rel_path}: error reading file: {str(e)}")

        # 4. Check for unexpected files in the package directory
        all_on_disk: Set[str] = set()
        for root, dirs, files in os.walk(package_dir):
            for file in files:
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, package_dir).replace("\\", "/")
                all_on_disk.add(rel_p)

        # Allow checksums.sha256 itself, plus all files declared in checksums.sha256
        allowed_files = set(recorded_checksums.keys())
        allowed_files.add(CHECKSUMS_FILENAME)

        for disk_file in sorted(all_on_disk):
            if disk_file not in allowed_files:
                unexpected_files.append(disk_file)

        is_valid = (
            len(metadata_errors) == 0
            and len(missing_files) == 0
            and len(mismatches) == 0
            and len(unexpected_files) == 0
        )

        return PackageVerificationResult(
            is_valid=is_valid,
            run_id=run_id,
            portal_id=portal_id,
            package_dir=package_dir,
            verified_files=verified_files,
            missing_files=missing_files,
            checksum_mismatches=mismatches,
            unexpected_files=unexpected_files,
            metadata_errors=metadata_errors,
        )

    # Convenient alias for verify_package
    verify = verify_package

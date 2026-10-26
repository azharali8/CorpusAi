"""
Catalog Reconciliation, Baseline Validation, and Retention Management for CorpusAI Phase 9B.

Provides:
- validate_baseline_integrity: Pre-flight gate verifying catalog, package, and state
  integrity before any incremental crawl begins.
- CatalogReconciler: Scans for orphaned verified packages, missing packages, or corrupted
  catalog pointers, producing dry-run audit reports and applying non-destructive repairs.
- StorageRetentionManager: Conservative staging directory cleanup policy that preserves
  all finalized packages and active incremental baseline dependencies.
"""

from dataclasses import dataclass, field
import datetime
import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.archive_package import PackageVerifier, PackageVerificationResult
from src.portal_state import PortalState

logger = logging.getLogger("corpusai.recovery")


class BaselineIntegrityError(Exception):
    """Raised when pre-run baseline validation fails."""
    pass


def validate_baseline_integrity(
    catalog: ArchiveCatalog,
    results_base_dir: str,
    portal_id: str,
) -> Tuple[str, str, PortalState]:
    """
    Strict pre-run baseline validation gate for incremental crawls.

    Verifies BEFORE any HTTP requests:
    1. Catalog has a valid latest_successful_run_id in catalog.runs.
    2. Referenced finalized package directory exists.
    3. PackageVerifier independently verifies package checksums and manifests.
    4. Authoritative portal_state.json exists and deserializes cleanly.
    5. portal_state matches expected portal_id and run_id.

    Returns:
        Tuple of (prev_run_id, prev_state_file_path, previous_portal_state)
    Raises:
        BaselineIntegrityError if any check fails.
    """
    prev_run_id = catalog.latest_successful_run_id
    if not prev_run_id:
        raise BaselineIntegrityError("Catalog has no latest_successful_run_id recorded.")

    if prev_run_id not in catalog.runs:
        raise BaselineIntegrityError(
            f"Catalog points to latest_successful_run_id='{prev_run_id}', but no entry exists in catalog.runs."
        )

    cat_entry = catalog.runs[prev_run_id]
    if cat_entry.status not in ("CONVERGED", "COMPLETED", "PILOT_SCOPE_CONVERGED", "MULTIPAGE_PILOT_COMPLETE"):
        raise BaselineIntegrityError(
            f"Baseline run '{prev_run_id}' has non-successful status '{cat_entry.status}'."
        )

    # Resolve finalized package directory
    pkg_dir = os.path.join(results_base_dir, "packages", portal_id, "runs", prev_run_id)
    if not os.path.exists(pkg_dir):
        raise BaselineIntegrityError(
            f"Finalized package directory not found for baseline run '{prev_run_id}' at '{pkg_dir}'."
        )

    # Verify package integrity with PackageVerifier
    verifier_fn = getattr(PackageVerifier, "verify", PackageVerifier.verify_package)
    res: PackageVerificationResult = verifier_fn(pkg_dir)
    if not res.is_valid:
        raise BaselineIntegrityError(
            f"Package verification failed for baseline run '{prev_run_id}' at '{pkg_dir}': "
            f"missing={res.missing_files}, mismatches={res.checksum_mismatches}, errors={res.metadata_errors}"
        )

    # Resolve portal_state.json from authoritative package location or staging run directory
    candidate_state_paths = [
        os.path.join(pkg_dir, "portal_state.json"),
        os.path.join(pkg_dir, "extra", "portal_state.json"),
        os.path.join(results_base_dir, cat_entry.run_dir, "portal_state.json"),
        os.path.join(results_base_dir, "runs", prev_run_id, "portal_state.json"),
        os.path.join(cat_entry.run_dir, "portal_state.json"),
    ]
    state_file = next((p for p in candidate_state_paths if os.path.exists(p)), None)
    if not state_file:
        raise BaselineIntegrityError(
            f"Authoritative portal_state.json not found for baseline run '{prev_run_id}' "
            f"(checked package extra and staging directories: {candidate_state_paths})."
        )

    try:
        portal_state = PortalState.load_from_json(state_file)
    except Exception as e:
        raise BaselineIntegrityError(
            f"Failed to deserialize portal_state.json from '{state_file}': {e}"
        )

    if portal_state.portal_id != portal_id:
        raise BaselineIntegrityError(
            f"PortalState portal_id mismatch: expected '{portal_id}', got '{portal_state.portal_id}'."
        )

    if portal_state.run_id != prev_run_id:
        raise BaselineIntegrityError(
            f"PortalState run_id mismatch: expected '{prev_run_id}', got '{portal_state.run_id}'."
        )

    return prev_run_id, state_file, portal_state


@dataclass
class ReconciliationReport:
    """Audit report detailing catalog inconsistencies and candidate repairs."""
    portal_id: str
    catalog_path: str
    packages_dir: str
    current_head: Optional[str]
    missing_packages: List[str] = field(default_factory=list)
    corrupted_packages: List[str] = field(default_factory=list)
    orphaned_verified_packages: List[str] = field(default_factory=list)
    ambiguous_candidates: List[str] = field(default_factory=list)
    proposed_new_head: Optional[str] = None
    repairable: bool = False
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portal_id": self.portal_id,
            "catalog_path": self.catalog_path,
            "packages_dir": self.packages_dir,
            "current_head": self.current_head,
            "missing_packages": self.missing_packages,
            "corrupted_packages": self.corrupted_packages,
            "orphaned_verified_packages": self.orphaned_verified_packages,
            "ambiguous_candidates": self.ambiguous_candidates,
            "proposed_new_head": self.proposed_new_head,
            "repairable": self.repairable,
            "details": self.details,
        }


class CatalogReconciler:
    """
    Reconciles discrepancy between on-disk verified packages and catalog.json.

    Can detect:
    - Orphaned packages: Verified packages on disk not registered in catalog.
    - Missing packages: Catalog entries whose package directory was removed.
    - Corrupt packages: Package fails PackageVerifier.
    - Missing or corrupt state files in package extra/ directory.
    """

    def __init__(self, results_base_dir: str, portal_id: str):
        self.results_base_dir = os.path.abspath(results_base_dir)
        self.portal_id = portal_id
        self.catalog_path = os.path.join(self.results_base_dir, "catalog.json")
        self.packages_dir = os.path.join(self.results_base_dir, "packages", self.portal_id, "runs")

    def inspect(self) -> ReconciliationReport:
        """Run read-only audit and return a dry-run ReconciliationReport."""
        catalog = ArchiveCatalog.load_or_create(self.catalog_path, self.portal_id)
        report = ReconciliationReport(
            portal_id=self.portal_id,
            catalog_path=self.catalog_path,
            packages_dir=self.packages_dir,
            current_head=catalog.latest_successful_run_id,
        )

        # Check existing catalog entries
        for run_id in catalog.all_run_ids:
            pkg_path = os.path.join(self.packages_dir, run_id)
            if not os.path.exists(pkg_path):
                report.missing_packages.append(run_id)
                report.details.append(f"Catalog entry '{run_id}' has no package directory at '{pkg_path}'.")
            else:
                verifier_fn = getattr(PackageVerifier, "verify", PackageVerifier.verify_package)
                res = verifier_fn(pkg_path)
                if not res.is_valid:
                    report.corrupted_packages.append(run_id)
                    report.details.append(f"Package '{run_id}' failed verification: {res.metadata_errors or res.missing_files}.")

        # Scan packages directory on disk for orphaned packages
        if os.path.exists(self.packages_dir):
            for entry in sorted(os.listdir(self.packages_dir)):
                pkg_path = os.path.join(self.packages_dir, entry)
                if not os.path.isdir(pkg_path):
                    continue

                if entry not in catalog.runs:
                    # Potential orphaned package. Verify it independently.
                    verifier_fn = getattr(PackageVerifier, "verify", PackageVerifier.verify_package)
                    res = verifier_fn(pkg_path)
                    if res.is_valid:
                        # Verify it has valid manifest and portal_state
                        manifest_file = os.path.join(pkg_path, "manifest.json")
                        state_file = os.path.join(pkg_path, "portal_state.json")
                        if not os.path.exists(state_file):
                            state_file = os.path.join(pkg_path, "extra", "portal_state.json")
                        if os.path.exists(manifest_file) and os.path.exists(state_file):
                            try:
                                with open(manifest_file, "r", encoding="utf-8") as f:
                                    mdata = json.load(f)
                                st = PortalState.load_from_json(state_file)
                                # Crucial: Check that the package represents a successfully completed crawl (CONVERGED/COMPLETED)
                                # Package integrity alone is NOT proof that the crawl itself completed successfully!
                                status = mdata.get("status")
                                if status not in ("CONVERGED", "COMPLETED", "PILOT_SCOPE_CONVERGED", "MULTIPAGE_PILOT_COMPLETE"):
                                    report.ambiguous_candidates.append(entry)
                                    report.details.append(
                                        f"Orphaned package '{entry}' has non-successful crawl status '{status}' "
                                        f"(cannot be adopted as catalog baseline)."
                                    )
                                elif mdata.get("anomalies"):
                                    report.ambiguous_candidates.append(entry)
                                    report.details.append(
                                        f"Orphaned package '{entry}' contains anomalies {mdata.get('anomalies')} "
                                        f"(requires manual review, cannot be auto-adopted)."
                                    )
                                elif mdata.get("portal_id") == self.portal_id and st.portal_id == self.portal_id:
                                    report.orphaned_verified_packages.append(entry)
                                    report.details.append(f"Found valid orphaned verified package: '{entry}'.")
                                else:
                                    report.ambiguous_candidates.append(entry)
                                    report.details.append(f"Orphaned package '{entry}' has portal_id mismatch.")
                            except Exception as e:
                                report.ambiguous_candidates.append(entry)
                                report.details.append(f"Orphaned package '{entry}' metadata unreadable: {e}")
                        else:
                            report.ambiguous_candidates.append(entry)
                            report.details.append(f"Orphaned package '{entry}' missing manifest or state snapshot.")
                    else:
                        report.corrupted_packages.append(entry)
                        report.details.append(f"Unreferenced package directory '{entry}' is corrupt/unverified.")

        # Determine proposed recovery action
        if report.orphaned_verified_packages:
            if len(report.orphaned_verified_packages) == 1:
                cand = report.orphaned_verified_packages[0]
                report.proposed_new_head = cand
                report.repairable = True
                report.details.append(f"Single valid orphaned package '{cand}' can be adopted into catalog.")
            else:
                report.repairable = False
                report.details.append(
                    f"Multiple orphaned verified packages found ({report.orphaned_verified_packages}). "
                    f"Ambiguous recovery requires manual operator review."
                )
        elif report.current_head in report.corrupted_packages or report.current_head in report.missing_packages:
            # Current head is corrupt or missing. Check if prior run in history can be safely restored.
            valid_prior = [
                rid for rid in catalog.all_run_ids
                if rid != report.current_head
                and rid not in report.corrupted_packages
                and rid not in report.missing_packages
                and catalog.runs[rid].status in ("CONVERGED", "COMPLETED")
            ]
            if valid_prior:
                report.proposed_new_head = valid_prior[-1]
                report.repairable = True
                report.details.append(f"Current head is invalid. Proposing rollback to last valid run: '{valid_prior[-1]}'.")
            else:
                report.repairable = False
                report.details.append("Current head is invalid and no valid historical prior baseline found.")

        return report

    def reconcile(self, dry_run: bool = True) -> ReconciliationReport:
        """
        Execute catalog reconciliation. If dry_run is True, returns audit without mutations.
        """
        report = self.inspect()
        if dry_run or not report.repairable or not report.proposed_new_head:
            return report

        catalog = ArchiveCatalog.load_or_create(self.catalog_path, self.portal_id)
        new_head = report.proposed_new_head

        # Case 1: Adopt orphaned package
        if new_head in report.orphaned_verified_packages:
            pkg_path = os.path.join(self.packages_dir, new_head)
            manifest_file = os.path.join(pkg_path, "manifest.json")
            with open(manifest_file, "r", encoding="utf-8") as f:
                mdata = json.load(f)

            entry = CatalogRunEntry(
                run_id=new_head,
                previous_run_id=mdata.get("previous_run_id"),
                mode=mdata.get("mode", "INCREMENTAL"),
                started_at=mdata.get("started_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
                completed_at=mdata.get("completed_at"),
                status=mdata.get("status", "CONVERGED"),
                article_count=mdata.get("article_count", 0),
                new_article_count=mdata.get("new_article_count", 0),
                changed_article_count=mdata.get("changed_article_count", 0),
                run_dir=f"packages/{self.portal_id}/runs/{new_head}/",
                anomalies=mdata.get("anomalies", []),
            )
            catalog.register_run(entry, is_successful=True)
            catalog.save_to_json(self.catalog_path)
            report.details.append(f"RECONCILED: Adopted orphaned package '{new_head}' as active head in catalog.")

        # Case 2: Rollback corrupt head to prior valid run
        elif new_head in catalog.runs:
            catalog.latest_successful_run_id = new_head
            catalog.save_to_json(self.catalog_path)
            report.details.append(f"RECONCILED: Rolled back catalog head pointer to '{new_head}'.")

        return report


@dataclass
class RetentionPlan:
    """Audit of directories considered for retention vs cleanup."""
    portal_id: str
    retained_runs: List[str] = field(default_factory=list)
    retained_packages: List[str] = field(default_factory=list)
    eligible_for_removal: List[str] = field(default_factory=list)
    protected_reasons: Dict[str, str] = field(default_factory=dict)
    bytes_reclaimable: int = 0


class StorageRetentionManager:
    """
    Conservative retention manager for raw staging crawl runs.

    Guarantees:
    - Finalized packages under packages/<portal>/runs/ are NEVER deleted.
    - Staging run directory required by catalog.latest_successful_run_id is NEVER deleted.
    - Runs flagged with manual review or anomalies are retained for diagnostics.
    - Only unreferenced staging runs older than min_keep_staging count are eligible.
    """

    def __init__(self, results_base_dir: str, portal_id: str):
        self.results_base_dir = os.path.abspath(results_base_dir)
        self.portal_id = portal_id
        self.catalog_path = os.path.join(self.results_base_dir, "catalog.json")
        self.runs_dir = os.path.join(self.results_base_dir, "runs")
        self.packages_dir = os.path.join(self.results_base_dir, "packages", self.portal_id, "runs")

    def plan_cleanup(self, min_keep_staging: int = 5) -> RetentionPlan:
        """Compute conservative cleanup plan."""
        plan = RetentionPlan(portal_id=self.portal_id)
        catalog = ArchiveCatalog.load_or_create(self.catalog_path, self.portal_id)
        current_head = catalog.latest_successful_run_id

        # 1. All packages are unconditionally protected
        if os.path.exists(self.packages_dir):
            for pkg in os.listdir(self.packages_dir):
                plan.retained_packages.append(pkg)
                plan.protected_reasons[pkg] = "Finalized immutable archival package (never deleted)"

        if not os.path.exists(self.runs_dir):
            return plan

        all_staging_folders = sorted(os.listdir(self.runs_dir))

        for folder in all_staging_folders:
            folder_path = os.path.join(self.runs_dir, folder)
            if not os.path.isdir(folder_path):
                continue

            # Check if this folder matches or contains current head run
            is_current_baseline = False
            if current_head:
                if current_head in folder or (current_head in catalog.runs and catalog.runs[current_head].run_dir.rstrip("/").endswith(folder)):
                    is_current_baseline = True

            if is_current_baseline:
                plan.retained_runs.append(folder)
                plan.protected_reasons[folder] = "Active incremental baseline dependency"
                continue

            # Check if run has anomalies or manual review
            summary_file = os.path.join(folder_path, "run_summary.json")
            if os.path.exists(summary_file):
                try:
                    with open(summary_file, "r", encoding="utf-8") as f:
                        sdata = json.load(f)
                    if sdata.get("anomalies") or sdata.get("errors"):
                        plan.retained_runs.append(folder)
                        plan.protected_reasons[folder] = "Diagnostic retention (contains errors or anomalies)"
                        continue
                except Exception:
                    pass

            plan.eligible_for_removal.append(folder)

        # Enforce min_keep_staging for recent runs
        if len(plan.eligible_for_removal) <= min_keep_staging:
            for f in list(plan.eligible_for_removal):
                plan.retained_runs.append(f)
                plan.protected_reasons[f] = "Protected by min_keep_staging count"
            plan.eligible_for_removal.clear()
        else:
            # Keep newest min_keep_staging
            to_keep = plan.eligible_for_removal[-min_keep_staging:]
            plan.eligible_for_removal = plan.eligible_for_removal[:-min_keep_staging]
            for f in to_keep:
                plan.retained_runs.append(f)
                plan.protected_reasons[f] = "Protected by min_keep_staging count"

        for f in plan.eligible_for_removal:
            folder_path = os.path.join(self.runs_dir, f)
            try:
                for root, _, files in os.walk(folder_path):
                    for file in files:
                        plan.bytes_reclaimable += os.path.getsize(os.path.join(root, file))
            except Exception:
                pass

        return plan

    def execute_cleanup(self, min_keep_staging: int = 5, dry_run: bool = True) -> RetentionPlan:
        """Execute cleanup according to conservative plan."""
        plan = self.plan_cleanup(min_keep_staging=min_keep_staging)
        if dry_run or not plan.eligible_for_removal:
            return plan

        for f in plan.eligible_for_removal:
            folder_path = os.path.join(self.runs_dir, f)
            try:
                shutil.rmtree(folder_path)
                logger.info(f"Cleaned up obsolete staging run directory: {folder_path}")
            except Exception as e:
                logger.warning(f"Failed to remove staging directory '{folder_path}': {e}")

        return plan

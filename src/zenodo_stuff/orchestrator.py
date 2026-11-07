"""
Deposit Orchestration sequencing package validation, draft creation, uploads, and verification.
"""

import datetime
import logging
from typing import Optional

from src.archive_package import PackageVerifier
from src.deposit_metadata import DepositMetadata
from src.zenodo_stuff.models import (
    DepositReceipt,
    STATUS_DRAFT_CREATED,
    STATUS_FAILED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PUBLISHED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    STATUS_VERIFIED,
)
from src.zenodo_stuff.registry import DepositRegistry

logger = logging.getLogger("corpusai.deposit")


class RepositoryDepositClient:
    """Abstract interface defining the repository deposit lifecycle contract."""

    def create_draft(
        self,
        metadata: DepositMetadata,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    def find_existing_draft(
        self,
        portal_id: str,
        run_id: str,
    ) -> Optional[DepositReceipt]:
        raise NotImplementedError

    def upload_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    def verify_remote_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:
        raise NotImplementedError

    def publish_deposit(
        self,
        receipt: DepositReceipt,
        allow_publish: bool = False,
    ) -> DepositReceipt:
        raise NotImplementedError


class DepositOrchestrator:
    """
    High-level orchestrator that executes safe, idempotent deposits of finalized
    archival packages to Zenodo Sandbox.
    """

    def __init__(
        self,
        client: RepositoryDepositClient,
        registry: DepositRegistry,
    ):
        self.client = client
        self.registry = registry

    def deposit_package(
        self,
        portal_id: str,
        run_id: str,
        package_dir: str,
        deposit_metadata: DepositMetadata,
    ) -> DepositReceipt:
        """
        Orchestrate deposit with idempotency, local verification, and safe retry.
        """
        from src.zenodo_deposit import compute_package_fingerprint

        package_fingerprint = compute_package_fingerprint(package_dir)

        # 1. Check existing receipt for duplicate protection & resumption
        existing = self.registry.get_receipt(portal_id, run_id)
        if existing:
            # If package modified, require reconciliation
            if existing.package_fingerprint != package_fingerprint:
                existing.transition_to(
                    STATUS_NEEDS_RECONCILIATION,
                    error=f"Local package fingerprint changed ({existing.package_fingerprint[:8]} -> {package_fingerprint[:8]}).",
                    error_category="PACKAGE_FINGERPRINT_CHANGED",
                )
                self.registry.save()
                return existing

            # If already verified or published, return existing receipt (idempotent)
            if existing.status in (STATUS_VERIFIED, STATUS_PUBLISHED):
                logger.info(
                    f"Run '{run_id}' already deposited and {existing.status}. Returning receipt."
                )
                return existing

            # Resume from existing draft
            receipt = existing
        else:
            # 2. Local verification before remote draft creation
            v_res = PackageVerifier.verify_package(package_dir)
            if not v_res.is_valid:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                err_msg = "; ".join(
                    v_res.metadata_errors
                    + v_res.missing_files
                    + v_res.checksum_mismatches
                    + v_res.unexpected_files
                )
                failed_receipt = DepositReceipt(
                    receipt_id=f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}",
                    portal_id=portal_id,
                    run_id=run_id,
                    package_fingerprint=package_fingerprint,
                    repository_target="zenodo",
                    environment="sandbox",
                    status=STATUS_FAILED,
                    created_at=now,
                    updated_at=now,
                    last_error=f"Local package verification failed: {err_msg}",
                    last_error_category="LOCAL_PACKAGE_INVALID",
                    retry_eligible=False,
                )
                self.registry.register_receipt(failed_receipt)
                return failed_receipt

            # 3. Check for existing remote draft to avoid duplicate creation if interrupted earlier
            existing_remote = self.client.find_existing_draft(portal_id, run_id)
            if existing_remote:
                existing_remote.package_fingerprint = package_fingerprint
                self.registry.register_receipt(existing_remote)
                receipt = existing_remote
            else:
                # Create remote draft
                try:
                    receipt = self.client.create_draft(
                        deposit_metadata, portal_id, run_id, package_fingerprint
                    )
                    self.registry.register_receipt(receipt)
                except Exception as e:
                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    failed_receipt = DepositReceipt(
                        receipt_id=f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}",
                        portal_id=portal_id,
                        run_id=run_id,
                        package_fingerprint=package_fingerprint,
                        repository_target="zenodo",
                        environment="sandbox",
                        status=STATUS_FAILED,
                        created_at=now,
                        updated_at=now,
                        last_error=str(e),
                        last_error_category="DRAFT_CREATION_FAILED",
                    )
                    self.registry.register_receipt(failed_receipt)
                    return failed_receipt

        # 4. Upload artifacts
        if receipt.status in (STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_FAILED):
            try:
                receipt = self.client.upload_artifacts(receipt, package_dir)
                self.registry.save()
            except Exception as e:
                receipt.transition_to(
                    STATUS_FAILED, error=str(e), error_category="UPLOAD_EXCEPTION"
                )
                self.registry.save()
                return receipt

        # 5. Verify remote artifacts
        if receipt.status == STATUS_UPLOADED:
            try:
                receipt = self.client.verify_remote_artifacts(receipt, package_dir)
                self.registry.save()
            except Exception as e:
                receipt.transition_to(
                    STATUS_FAILED,
                    error=str(e),
                    error_category="VERIFICATION_EXCEPTION",
                )
                self.registry.save()
                return receipt

        return receipt

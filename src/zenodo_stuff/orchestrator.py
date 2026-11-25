"""
Deposit Orchestration sequencing package validation, draft creation, uploads, and verification.
"""

from abc import ABC, abstractmethod
import itertools
import logging
from typing import Optional, Set

from src.archive_package import PackageVerifier
from src.deposit_metadata import DepositMetadata
from src.zenodo_stuff.models import (
    DepositReceipt,
    DepositStatus,
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

_UPLOADABLE_STATUSES: Set[str] = {
    STATUS_DRAFT_CREATED,
    STATUS_UPLOADING,
    STATUS_FAILED,
}


class RepositoryDepositClient(ABC):
    """Abstract interface defining the repository deposit lifecycle contract."""

    @property
    def repository_target(self) -> str:
        """Name of the target repository (e.g. 'zenodo')."""
        return "zenodo"

    @property
    def environment(self) -> str:
        """Environment name (e.g. 'sandbox' or 'production')."""
        return "sandbox"

    @abstractmethod
    def create_draft(
        self,
        metadata: DepositMetadata,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    @abstractmethod
    def find_existing_draft(
        self,
        portal_id: str,
        run_id: str,
    ) -> Optional[DepositReceipt]:
        raise NotImplementedError

    @abstractmethod
    def upload_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    @abstractmethod
    def verify_remote_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        raise NotImplementedError

    @abstractmethod
    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:
        raise NotImplementedError

    @abstractmethod
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
        repository_target: Optional[str] = None,
        environment: Optional[str] = None,
    ):
        self.client = client
        self.registry = registry
        self.repository_target = repository_target or getattr(client, "repository_target", "zenodo")
        self.environment = environment or getattr(client, "environment", "sandbox")

    def _resolve_repo_and_env(self, existing: Optional[DepositReceipt] = None) -> tuple[str, str]:
        """Resolve authoritative repository target and environment."""
        if existing is not None:
            repo = existing.repository_target or self.repository_target
            env = existing.environment or self.environment
            return repo, env
        return self.repository_target, self.environment

    def _validate_local_package(
        self,
        package_dir: str,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
    ) -> Optional[DepositReceipt]:
        """Validate package locally prior to remote draft operations."""
        v_res = PackageVerifier.verify_package(package_dir)
        if not v_res.is_valid:
            err_msg = "; ".join(
                itertools.chain(
                    v_res.metadata_errors,
                    v_res.missing_files,
                    v_res.checksum_mismatches,
                    v_res.unexpected_files,
                )
            )
            repo, env = self._resolve_repo_and_env()
            failed_receipt = DepositReceipt.for_failure(
                portal_id=portal_id,
                run_id=run_id,
                fingerprint=package_fingerprint,
                repository=repo,
                environment=env,
                last_error=f"Local package verification failed: {err_msg}",
                last_error_category="LOCAL_PACKAGE_INVALID",
                retry_eligible=False,
            )
            self.registry.register_receipt(failed_receipt)
            return failed_receipt
        return None

    def _prepare_draft(
        self,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
        deposit_metadata: DepositMetadata,
    ) -> DepositReceipt:
        """Discover existing remote draft or create a new remote draft."""
        existing_remote = self.client.find_existing_draft(portal_id, run_id)
        if existing_remote:
            existing_remote.package_fingerprint = package_fingerprint
            self.registry.register_receipt(existing_remote)
            return existing_remote

        try:
            receipt = self.client.create_draft(
                deposit_metadata, portal_id, run_id, package_fingerprint
            )
            self.registry.register_receipt(receipt)
            return receipt
        except Exception as e:
            repo, env = self._resolve_repo_and_env()
            failed_receipt = DepositReceipt.for_failure(
                portal_id=portal_id,
                run_id=run_id,
                fingerprint=package_fingerprint,
                repository=repo,
                environment=env,
                last_error=str(e),
                last_error_category="DRAFT_CREATION_FAILED",
            )
            self.registry.register_receipt(failed_receipt)
            return failed_receipt

    def _execute_upload(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        """Stream artifacts to repository bucket."""
        try:
            receipt = self.client.upload_artifacts(receipt, package_dir)
            self.registry.save()
            return receipt
        except Exception as e:
            receipt.transition_to(
                STATUS_FAILED, error=str(e), error_category="UPLOAD_EXCEPTION"
            )
            self.registry.save()
            return receipt

    def _execute_verification(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        """Verify remote artifacts against local checksum manifest."""
        try:
            receipt = self.client.verify_remote_artifacts(receipt, package_dir)
            self.registry.save()
            return receipt
        except Exception as e:
            receipt.transition_to(
                STATUS_FAILED,
                error=str(e),
                error_category="VERIFICATION_EXCEPTION",
            )
            self.registry.save()
            return receipt

    def deposit_package(
        self,
        portal_id: str,
        run_id: str,
        package_dir: str,
        deposit_metadata: DepositMetadata,
    ) -> DepositReceipt:
        """
        Orchestrate deposit with idempotency, local verification, safe retry,
        sequential upload, and immediate remote verification.
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

            # Respect retry eligibility if previously failed
            if existing.status == STATUS_FAILED and not existing.retry_eligible:
                logger.info(
                    f"Run '{run_id}' previously failed with retry_eligible=False. Rejecting retry."
                )
                return existing

            # Resume from existing receipt
            receipt = existing
        else:
            # 2. Local verification before remote draft creation
            failed_receipt = self._validate_local_package(
                package_dir, portal_id, run_id, package_fingerprint
            )
            if failed_receipt is not None:
                return failed_receipt

            # 3. Check for existing remote draft or create a new one
            receipt = self._prepare_draft(
                portal_id, run_id, package_fingerprint, deposit_metadata
            )
            if receipt.status == STATUS_FAILED:
                return receipt

        # 4. Upload artifacts if in uploadable state
        if receipt.status in _UPLOADABLE_STATUSES:
            receipt = self._execute_upload(receipt, package_dir)
            if receipt.status == STATUS_FAILED:
                return receipt

        # 5. Remote artifact verification: executed immediately after successful upload
        # or when resuming an existing receipt already in UPLOADED state
        if receipt.status == STATUS_UPLOADED:
            receipt = self._execute_verification(receipt, package_dir)

        return receipt

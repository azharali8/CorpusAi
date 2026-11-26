"""
Abstract repository deposit client contract.

Defines ``RepositoryDepositClient``, the interface that concrete repository
adapters (such as ``ZenodoSandboxAdapter``) must implement and that
``DepositOrchestrator`` depends on.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.deposit_metadata import DepositMetadata
from src.zenodo_stuff.models import DepositReceipt


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

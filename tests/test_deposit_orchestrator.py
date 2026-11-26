"""
Stage 8 Unit Tests: Repository Client Contract and Deposit Orchestrator.

Covers:
1. Abstract RepositoryDepositClient enforcement (cannot instantiate incomplete subclasses).
2. Concrete client implementations satisfy the contract.
3. itertools.chain error aggregation in _validate_local_package.
4. Set status membership in _UPLOADABLE_STATUSES.
5. Decomposed helper methods (_validate_local_package, _prepare_draft, _execute_upload, _execute_verification).
6. Orchestration lifecycle: local validation, draft discovery vs creation, sequential upload and immediate verification.
7. Resumption from UPLOADED state triggers verification directly.
8. Safe retry respecting retry_eligible flag.
9. Repository target and environment propagation (custom values, no hard-coded defaults).
10. Process interruption / persistence failure handling.
"""

from abc import ABC
import itertools
from pathlib import Path
from typing import Any, Dict, Optional
import pytest

from src.deposit_metadata import Creator, DepositMetadata
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
from src.zenodo_stuff.orchestrator import (
    DepositOrchestrator,
    RepositoryDepositClient,
    _UPLOADABLE_STATUSES,
)
from src.zenodo_stuff.registry import DepositRegistry
from src.zenodo_deposit import ZenodoSandboxAdapter


# ---------------------------------------------------------------------------
# Test Helpers & Dummy Implementations
# ---------------------------------------------------------------------------

def _dummy_metadata() -> DepositMetadata:
    return DepositMetadata(
        title="Test Archival Corpus",
        creators=[Creator(name="Test Researcher", affiliation="Lab")],
        description="A dataset for testing Stage 8 contract",
        software_version="1.0.0",
        source_domains=["example.com"],
        archive_formats=["WARC"],
        capture_period_start="2026-01-01",
        capture_period_end="2026-01-02",
        copyright_notice="CC0",
        license_status="Public Domain",
        checksum_manifest_path="checksums.sha256",
    )


class DummyCompleteClient(RepositoryDepositClient):
    """Complete implementation providing all abstract methods."""

    def __init__(self, repo: str = "custom_repo", env: str = "staging"):
        self._repo = repo
        self._env = env

    @property
    def repository_target(self) -> str:
        return self._repo

    @property
    def environment(self) -> str:
        return self._env

    def create_draft(self, metadata, portal_id, run_id, package_fingerprint) -> DepositReceipt:
        return DepositReceipt(
            receipt_id=f"receipt-{portal_id}-{run_id}-draft",
            portal_id=portal_id,
            run_id=run_id,
            package_fingerprint=package_fingerprint,
            repository_target=self.repository_target,
            environment=self.environment,
            status=STATUS_DRAFT_CREATED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )

    def find_existing_draft(self, portal_id, run_id) -> Optional[DepositReceipt]:
        return None

    def upload_artifacts(self, receipt, package_dir) -> DepositReceipt:
        receipt.transition_to(STATUS_UPLOADING)
        receipt.transition_to(STATUS_UPLOADED)
        return receipt

    def verify_remote_artifacts(self, receipt, package_dir) -> DepositReceipt:
        receipt.transition_to(STATUS_VERIFIED)
        return receipt

    def get_status(self, receipt) -> DepositReceipt:
        return receipt

    def publish_deposit(self, receipt, allow_publish=False) -> DepositReceipt:
        receipt.transition_to(STATUS_PUBLISHED)
        return receipt


# ---------------------------------------------------------------------------
# 1. ABC Contract Enforcement
# ---------------------------------------------------------------------------

class TestRepositoryDepositClientABC:

    def test_cannot_instantiate_abc_directly(self):
        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            RepositoryDepositClient()

    def test_incomplete_subclass_cannot_be_instantiated(self):
        class IncompleteClient(RepositoryDepositClient):
            def create_draft(self, metadata, portal_id, run_id, package_fingerprint):
                pass
            # Missing find_existing_draft, upload_artifacts, etc.

        with pytest.raises(TypeError, match="Can't instantiate abstract class"):
            IncompleteClient()

    def test_complete_subclass_can_be_instantiated(self):
        client = DummyCompleteClient()
        assert isinstance(client, RepositoryDepositClient)
        assert isinstance(client, ABC)
        assert client.repository_target == "custom_repo"
        assert client.environment == "staging"

    def test_zenodo_sandbox_adapter_satisfies_contract(self):
        adapter = ZenodoSandboxAdapter(api_token="test_token")
        assert isinstance(adapter, RepositoryDepositClient)
        assert adapter.repository_target == "zenodo"
        assert adapter.environment == "sandbox"


# ---------------------------------------------------------------------------
# 2. Membership & Error Aggregation Conventions
# ---------------------------------------------------------------------------

class TestConventionsAndAggregation:

    def test_uploadable_statuses_is_set_and_correct(self):
        assert isinstance(_UPLOADABLE_STATUSES, set)
        assert _UPLOADABLE_STATUSES == {
            STATUS_DRAFT_CREATED,
            STATUS_UPLOADING,
            STATUS_FAILED,
        }

    def test_itertools_chain_in_package_validation(self, tmp_path, monkeypatch):
        from src.archive_package import PackageVerifier, PackageVerificationResult

        # Mock PackageVerifier to return multiple errors across categories
        mock_result = PackageVerificationResult(
            is_valid=False,
            run_id="r1",
            portal_id="p1",
            package_dir=str(tmp_path),
            metadata_errors=["err_meta1"],
            missing_files=["err_file1"],
            checksum_mismatches=["err_check1"],
            unexpected_files=["err_unexp1"],
        )
        monkeypatch.setattr(PackageVerifier, "verify_package", lambda p: mock_result)

        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        client = DummyCompleteClient(repo="datacite", env="test_env")
        orchestrator = DepositOrchestrator(client, registry)

        receipt = orchestrator.deposit_package(
            portal_id="p1",
            run_id="r1",
            package_dir=str(tmp_path),
            deposit_metadata=_dummy_metadata(),
        )

        assert receipt.status == STATUS_FAILED
        assert receipt.last_error_category == "LOCAL_PACKAGE_INVALID"
        assert receipt.retry_eligible is False
        assert "err_meta1; err_file1; err_check1; err_unexp1" in receipt.last_error
        # Verify repository and environment preserved
        assert receipt.repository_target == "datacite"
        assert receipt.environment == "test_env"


# ---------------------------------------------------------------------------
# 3. Orchestrator Lifecycle & Upload-Verification Flow
# ---------------------------------------------------------------------------

class TestOrchestratorLifecycle:

    def test_successful_flow_executes_upload_and_verification_same_call(self, tmp_path, monkeypatch):
        from src.archive_package import PackageVerifier, PackageVerificationResult
        monkeypatch.setattr(
            PackageVerifier,
            "verify_package",
            lambda p: PackageVerificationResult(is_valid=True, run_id="run1", portal_id="portal1", package_dir=str(p)),
        )

        actions = []

        class TrackingClient(DummyCompleteClient):
            def upload_artifacts(self, receipt, package_dir):
                actions.append("upload")
                return super().upload_artifacts(receipt, package_dir)

            def verify_remote_artifacts(self, receipt, package_dir):
                actions.append("verify")
                return super().verify_remote_artifacts(receipt, package_dir)

        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        client = TrackingClient(repo="zenodo", env="sandbox")
        orchestrator = DepositOrchestrator(client, registry)

        receipt = orchestrator.deposit_package(
            portal_id="portal1",
            run_id="run1",
            package_dir=str(tmp_path),
            deposit_metadata=_dummy_metadata(),
        )

        assert actions == ["upload", "verify"]
        assert receipt.status == STATUS_VERIFIED
        # Ensure persisted in registry
        saved = registry.get_receipt("portal1", "run1")
        assert saved is not None
        assert saved.status == STATUS_VERIFIED

    def test_resumption_from_uploaded_triggers_verification(self, tmp_path):
        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        uploaded_receipt = DepositReceipt(
            receipt_id="receipt-p1-r1-up",
            portal_id="p1",
            run_id="r1",
            package_fingerprint="fp123456",
            repository_target="zenodo",
            environment="sandbox",
            status=STATUS_UPLOADED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        registry.register_receipt(uploaded_receipt)

        # Mock compute_package_fingerprint to match
        import src.zenodo_stuff.orchestrator
        original_fp = src.zenodo_stuff.orchestrator.compute_package_fingerprint
        src.zenodo_stuff.orchestrator.compute_package_fingerprint = lambda p: "fp123456"

        try:
            client = DummyCompleteClient()
            orchestrator = DepositOrchestrator(client, registry)
            receipt = orchestrator.deposit_package(
                portal_id="p1",
                run_id="r1",
                package_dir=str(tmp_path),
                deposit_metadata=_dummy_metadata(),
            )
            assert receipt.status == STATUS_VERIFIED
        finally:
            src.zenodo_stuff.orchestrator.compute_package_fingerprint = original_fp


class TestLegacyShimAndImportIndependence:
    """Verify legacy src.zenodo_deposit shim resolves to canonical objects without cycles."""

    def test_legacy_shim_exports_resolve_to_canonical_objects(self):
        import src.zenodo_deposit as legacy
        from src.zenodo_stuff.client import (
            ZENODO_PRODUCTION_BASE_URL,
            ZENODO_SANDBOX_BASE_URL,
            ZenodoSandboxAdapter,
        )
        from src.zenodo_stuff.client_contract import RepositoryDepositClient
        from src.zenodo_stuff.fingerprint import compute_package_fingerprint
        from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord
        from src.zenodo_stuff.orchestrator import DepositOrchestrator
        from src.zenodo_stuff.registry import DepositRegistry

        assert legacy.DepositOrchestrator is DepositOrchestrator
        assert legacy.ZenodoSandboxAdapter is ZenodoSandboxAdapter
        assert legacy.RepositoryDepositClient is RepositoryDepositClient
        assert legacy.compute_package_fingerprint is compute_package_fingerprint
        assert legacy.DepositReceipt is DepositReceipt
        assert legacy.UploadedArtifactRecord is UploadedArtifactRecord
        assert legacy.DepositRegistry is DepositRegistry
        assert legacy.ZENODO_SANDBOX_BASE_URL == ZENODO_SANDBOX_BASE_URL
        assert legacy.ZENODO_PRODUCTION_BASE_URL == ZENODO_PRODUCTION_BASE_URL

    def test_orchestrator_does_not_import_concrete_client(self):
        import subprocess
        import sys

        code = (
            "import sys; "
            "from src.zenodo_stuff.orchestrator import DepositOrchestrator; "
            "from src.zenodo_stuff.client_contract import RepositoryDepositClient; "
            "assert 'src.zenodo_stuff.client' not in sys.modules; "
            "assert 'src.zenodo_deposit' not in sys.modules"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_failed_receipt_non_retry_eligible_rejected_immediately(self, tmp_path):
        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        failed_receipt = DepositReceipt.for_failure(
            portal_id="p1",
            run_id="r1",
            fingerprint="fp_locked",
            repository="custom_repo",
            environment="production",
            last_error="Fatal schema corruption",
            last_error_category="FATAL_CORRUPTION",
            retry_eligible=False,
        )
        registry.register_receipt(failed_receipt)

        import src.zenodo_stuff.orchestrator
        original_fp = src.zenodo_stuff.orchestrator.compute_package_fingerprint
        src.zenodo_stuff.orchestrator.compute_package_fingerprint = lambda p: "fp_locked"

        try:
            client = DummyCompleteClient()
            orchestrator = DepositOrchestrator(client, registry)
            res = orchestrator.deposit_package(
                portal_id="p1",
                run_id="r1",
                package_dir=str(tmp_path),
                deposit_metadata=_dummy_metadata(),
            )
            assert res.status == STATUS_FAILED
            assert res.last_error_category == "FATAL_CORRUPTION"
            assert res.retry_eligible is False
        finally:
            src.zenodo_stuff.orchestrator.compute_package_fingerprint = original_fp


class TestLegacyShimAndImportIndependence:
    """Verify legacy src.zenodo_deposit shim resolves to canonical objects without cycles."""

    def test_legacy_shim_exports_resolve_to_canonical_objects(self):
        import src.zenodo_deposit as legacy
        from src.zenodo_stuff.client import (
            ZENODO_PRODUCTION_BASE_URL,
            ZENODO_SANDBOX_BASE_URL,
            ZenodoSandboxAdapter,
        )
        from src.zenodo_stuff.client_contract import RepositoryDepositClient
        from src.zenodo_stuff.fingerprint import compute_package_fingerprint
        from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord
        from src.zenodo_stuff.orchestrator import DepositOrchestrator
        from src.zenodo_stuff.registry import DepositRegistry

        assert legacy.DepositOrchestrator is DepositOrchestrator
        assert legacy.ZenodoSandboxAdapter is ZenodoSandboxAdapter
        assert legacy.RepositoryDepositClient is RepositoryDepositClient
        assert legacy.compute_package_fingerprint is compute_package_fingerprint
        assert legacy.DepositReceipt is DepositReceipt
        assert legacy.UploadedArtifactRecord is UploadedArtifactRecord
        assert legacy.DepositRegistry is DepositRegistry
        assert legacy.ZENODO_SANDBOX_BASE_URL == ZENODO_SANDBOX_BASE_URL
        assert legacy.ZENODO_PRODUCTION_BASE_URL == ZENODO_PRODUCTION_BASE_URL

    def test_orchestrator_does_not_import_concrete_client(self):
        import subprocess
        import sys

        code = (
            "import sys; "
            "from src.zenodo_stuff.orchestrator import DepositOrchestrator; "
            "from src.zenodo_stuff.client_contract import RepositoryDepositClient; "
            "assert 'src.zenodo_stuff.client' not in sys.modules; "
            "assert 'src.zenodo_deposit' not in sys.modules"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# 4. Repository & Environment Propagation
# ---------------------------------------------------------------------------

class TestRepositoryEnvironmentPropagation:

    def test_custom_repo_and_env_propagated_to_failure_receipts(self, tmp_path, monkeypatch):
        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        client = DummyCompleteClient(repo="dryad", env="production")
        orchestrator = DepositOrchestrator(
            client=client,
            registry=registry,
            repository_target="dryad",
            environment="production",
        )

        # Force failure in draft creation
        def fail_draft(*args, **kwargs):
            raise ConnectionError("DNS failure to dryad.org")

        monkeypatch.setattr(client, "create_draft", fail_draft)
        from src.archive_package import PackageVerifier, PackageVerificationResult
        monkeypatch.setattr(
            PackageVerifier,
            "verify_package",
            lambda p: PackageVerificationResult(is_valid=True, run_id="run_42", portal_id="news_portal", package_dir=str(p)),
        )

        receipt = orchestrator.deposit_package(
            portal_id="news_portal",
            run_id="run_42",
            package_dir=str(tmp_path),
            deposit_metadata=_dummy_metadata(),
        )

        assert receipt.status == STATUS_FAILED
        assert receipt.repository_target == "dryad"
        assert receipt.environment == "production"
        assert receipt.last_error_category == "DRAFT_CREATION_FAILED"
        assert "DNS failure to dryad.org" in receipt.last_error

    def test_existing_receipt_repo_and_env_preserved_on_resumption(self, tmp_path):
        registry = DepositRegistry(tmp_path / "registry.json", db_path=tmp_path / "deposit_state.db")
        orig_receipt = DepositReceipt(
            receipt_id="rec-1",
            portal_id="portal_x",
            run_id="run_x",
            package_fingerprint="match_fp",
            repository_target="osf",
            environment="osf_prod",
            status=STATUS_DRAFT_CREATED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        registry.register_receipt(orig_receipt)

        import src.zenodo_stuff.orchestrator
        original_fp = src.zenodo_stuff.orchestrator.compute_package_fingerprint
        src.zenodo_stuff.orchestrator.compute_package_fingerprint = lambda p: "match_fp"

        try:
            client = DummyCompleteClient(repo="default_repo", env="default_env")
            orchestrator = DepositOrchestrator(client, registry)
            res = orchestrator.deposit_package(
                portal_id="portal_x",
                run_id="run_x",
                package_dir=str(tmp_path),
                deposit_metadata=_dummy_metadata(),
            )
            # Verify it preserved 'osf' and 'osf_prod' from the original receipt
            assert res.repository_target == "osf"
            assert res.environment == "osf_prod"
            assert res.status == STATUS_VERIFIED
        finally:
            src.zenodo_stuff.orchestrator.compute_package_fingerprint = original_fp


class TestLegacyShimAndImportIndependence:
    """Verify legacy src.zenodo_deposit shim resolves to canonical objects without cycles."""

    def test_legacy_shim_exports_resolve_to_canonical_objects(self):
        import src.zenodo_deposit as legacy
        from src.zenodo_stuff.client import (
            ZENODO_PRODUCTION_BASE_URL,
            ZENODO_SANDBOX_BASE_URL,
            ZenodoSandboxAdapter,
        )
        from src.zenodo_stuff.client_contract import RepositoryDepositClient
        from src.zenodo_stuff.fingerprint import compute_package_fingerprint
        from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord
        from src.zenodo_stuff.orchestrator import DepositOrchestrator
        from src.zenodo_stuff.registry import DepositRegistry

        assert legacy.DepositOrchestrator is DepositOrchestrator
        assert legacy.ZenodoSandboxAdapter is ZenodoSandboxAdapter
        assert legacy.RepositoryDepositClient is RepositoryDepositClient
        assert legacy.compute_package_fingerprint is compute_package_fingerprint
        assert legacy.DepositReceipt is DepositReceipt
        assert legacy.UploadedArtifactRecord is UploadedArtifactRecord
        assert legacy.DepositRegistry is DepositRegistry
        assert legacy.ZENODO_SANDBOX_BASE_URL == ZENODO_SANDBOX_BASE_URL
        assert legacy.ZENODO_PRODUCTION_BASE_URL == ZENODO_PRODUCTION_BASE_URL

    def test_orchestrator_does_not_import_concrete_client(self):
        import subprocess
        import sys

        code = (
            "import sys; "
            "from src.zenodo_stuff.orchestrator import DepositOrchestrator; "
            "from src.zenodo_stuff.client_contract import RepositoryDepositClient; "
            "assert 'src.zenodo_stuff.client' not in sys.modules; "
            "assert 'src.zenodo_deposit' not in sys.modules"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

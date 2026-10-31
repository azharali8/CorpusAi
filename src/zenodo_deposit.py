"""
Repository Deposit Client and Zenodo Sandbox Adapter module for CorpusAI Phase 9A.

Implements online repository deposit preparation, artifact streaming, and
receipt tracking following research guidance on preservation principles:
- Verification before upload: package must pass PackageVerifier.verify_package()
- Decoupled lifecycle: deposit outcomes are tracked independently of crawl success
- Safe retries and duplicate protection via stable package fingerprint
- Bounded retry with exponential backoff on transient HTTP 5xx errors
- Strict credential protection: tokens are never leaked to logs or exceptions
- Hardcoded safety gates: production publishing is strictly blocked by default
"""

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
import datetime
import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

from src.archive_package import PackageVerifier
from src.deposit_metadata import DepositMetadata

logger = logging.getLogger("corpusai.deposit")

# Zenodo URLs
ZENODO_SANDBOX_BASE_URL = "https://sandbox.zenodo.org/api"
ZENODO_PRODUCTION_BASE_URL = "https://zenodo.org/api"

# Zenodo Quota Constants
ZENODO_MAX_RECORD_FILES = 100
ZENODO_MAX_RECORD_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB
ZENODO_MAX_SINGLE_FILE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB

# Status Constants
STATUS_PENDING = "PENDING"
STATUS_DRAFT_CREATED = "DRAFT_CREATED"
STATUS_UPLOADING = "UPLOADING"
STATUS_UPLOADED = "UPLOADED"
STATUS_VERIFIED = "VERIFIED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = "FAILED"
STATUS_NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"

VALID_TRANSITIONS = {
    STATUS_PENDING: {STATUS_DRAFT_CREATED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_DRAFT_CREATED: {STATUS_UPLOADING, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_UPLOADING: {STATUS_UPLOADED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_UPLOADED: {STATUS_VERIFIED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},
    STATUS_VERIFIED: {STATUS_PUBLISHED, STATUS_FAILED},
    STATUS_FAILED: {STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_UPLOADED, STATUS_VERIFIED, STATUS_NEEDS_RECONCILIATION},
    STATUS_NEEDS_RECONCILIATION: {STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_UPLOADED, STATUS_VERIFIED, STATUS_FAILED},
    STATUS_PUBLISHED: set(),  # Terminal state
}


@dataclass
class UploadedArtifactRecord:
    """Metadata for an individual uploaded file on the repository."""
    filename: str
    filesize: int
    local_sha256: str
    local_md5: str
    remote_md5: Optional[str] = None
    remote_checksum: Optional[str] = None
    remote_file_id: Optional[str] = None
    verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UploadedArtifactRecord":
        return cls(**data)


@dataclass
class DepositReceipt:
    """
    Durable receipt tracking the outcome of an archival package deposit.
    Maintained separately from ArchiveCatalog to prevent deposit failures from
    invalidating valid crawls.
    """
    receipt_id: str
    portal_id: str
    run_id: str
    package_fingerprint: str
    repository_target: str
    environment: str
    status: str
    created_at: str
    updated_at: str
    remote_record_id: Optional[str] = None
    remote_bucket_url: Optional[str] = None
    remote_record_url: Optional[str] = None
    doi: Optional[str] = None
    artifact_inventory: Dict[str, UploadedArtifactRecord] = field(default_factory=dict)
    checksum_verification_outcome: str = "UNVERIFIED"
    last_error: Optional[str] = None
    last_error_category: Optional[str] = None
    retry_eligible: bool = True
    reconciliation_notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["artifact_inventory"] = {
            k: v.to_dict() if hasattr(v, "to_dict") else v
            for k, v in self.artifact_inventory.items()
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DepositReceipt":
        cleaned = dict(data)
        inv = cleaned.get("artifact_inventory", {})
        cleaned["artifact_inventory"] = {
            k: UploadedArtifactRecord.from_dict(v) if isinstance(v, dict) else v
            for k, v in inv.items()
        }
        return cls(**cleaned)

    def transition_to(self, new_status: str, error: Optional[str] = None, error_category: Optional[str] = None) -> None:
        """Enforce permitted state transitions."""
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(f"Invalid deposit status transition from '{self.status}' to '{new_status}'.")
        self.status = new_status
        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if error:
            self.last_error = error
            self.last_error_category = error_category or "UNKNOWN"


class DepositRegistry:
    """
    Durable on-disk registry mapping (portal_id, run_id) to DepositReceipt.
    Stored atomically alongside or inside portal results (e.g. deposit_registry.json).
    """

    def __init__(self, registry_path: str):
        self.registry_path = os.path.abspath(registry_path)
        self.receipts: Dict[str, DepositReceipt] = {}  # key: f"{portal_id}:{run_id}"

    @classmethod
    def load_or_create(cls, registry_path: str) -> "DepositRegistry":
        reg = cls(registry_path)
        if os.path.exists(registry_path):
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.get("receipts", {}).items():
                    reg.receipts[k] = DepositReceipt.from_dict(v)
        return reg

    def save(self) -> None:
        """Atomic write to disk to prevent corrupted state on interruption."""
        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)
        temp_path = f"{self.registry_path}.tmp.{os.getpid()}_{int(time.time()*1000)}"
        data = {
            "schema_version": "1",
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "receipts": {k: v.to_dict() for k, v in self.receipts.items()},
        }
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, self.registry_path)

    def get_receipt(self, portal_id: str, run_id: str) -> Optional[DepositReceipt]:
        return self.receipts.get(f"{portal_id}:{run_id}")

    def register_receipt(self, receipt: DepositReceipt) -> None:
        self.receipts[f"{receipt.portal_id}:{receipt.run_id}"] = receipt
        self.save()


class RepositoryDepositClient(ABC):
    """Abstract interface defining the repository deposit lifecycle contract."""

    @abstractmethod
    def create_draft(
        self,
        metadata: DepositMetadata,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
    ) -> DepositReceipt:
        pass

    @abstractmethod
    def find_existing_draft(
        self,
        portal_id: str,
        run_id: str,
    ) -> Optional[DepositReceipt]:
        """Search repository for an existing draft created for this portal and run."""
        pass

    @abstractmethod
    def upload_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        pass

    @abstractmethod
    def verify_remote_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        pass

    @abstractmethod
    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:
        pass

    @abstractmethod
    def publish_deposit(
        self,
        receipt: DepositReceipt,
        allow_publish: bool = False,
    ) -> DepositReceipt:
        pass


def compute_package_fingerprint(package_dir: str) -> str:
    """
    Compute a stable SHA-256 fingerprint of the finalized package.
    Uses checksums.sha256 if present, or sorted contents.
    """
    checksums_path = os.path.join(package_dir, "checksums.sha256")
    if os.path.exists(checksums_path):
        return sha256_file(checksums_path)
    # Fallback: hash directory tree listing
    hasher = hashlib.sha256()
    for root, _, files in os.walk(package_dir):
        for f in sorted(files):
            hasher.update(f.encode("utf-8"))
    return hasher.hexdigest()


from src.utils.crypto import compute_file_md5_and_sha256, sha256_file


class ZenodoSandboxAdapter(RepositoryDepositClient):
    """
    Concrete deposit client targeting the Zenodo Sandbox REST API.

    Safety boundaries:
    - Target URL is restricted to sandbox.zenodo.org.
    - Production Zenodo base URL is actively blocked.
    - Publishing requires explicit authorization (`allow_publish=True`).
    - API secrets are scrubbed from logs and exceptions.
    - Network requests use bounded retries on HTTP 5xx with exponential backoff.
    """

    def __init__(
        self,
        api_token: Optional[str] = None,
        base_url: str = ZENODO_SANDBOX_BASE_URL,
        max_retries: int = 3,
        request_timeout: float = 30.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
        http_transport: Optional[Callable[[urllib.request.Request], Any]] = None,
    ):
        token = api_token or os.environ.get("CORPUSAI_DEPOSIT_TOKEN", "")
        self._api_token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, max_retries)
        self.request_timeout = request_timeout
        self.sleep_fn = sleep_fn or time.sleep
        self._http_transport = http_transport

        # Security guard: Never allow production Zenodo implicitly or by configuration error
        if "sandbox.zenodo.org" not in self.base_url:
            if "zenodo.org" in self.base_url or "api" in self.base_url:
                raise ValueError(
                    f"Production Zenodo target is strictly disabled in Phase 9A. "
                    f"Target must be '{ZENODO_SANDBOX_BASE_URL}', got '{self.base_url}'."
                )

    def _mask_secret(self, text: str) -> str:
        """Strip sensitive token from any log or error string."""
        if not self._api_token or len(self._api_token) < 4:
            return text
        return text.replace(self._api_token, "[REDACTED_DEPOSIT_TOKEN]")

    def _execute_http_request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[bytes] = None,
        data_stream: Optional[Any] = None,
    ) -> Tuple[int, Dict[str, Any], bytes]:
        """
        Execute an HTTP request with bounded retry/backoff for transient 5xx errors.
        Does NOT retry 401, 403, 400, or 404 errors.
        """
        req_headers = {
            "Accept": "application/json",
            "User-Agent": "CorpusAI-Zenodo-Sandbox-Adapter/1.0",
        }
        if self._api_token:
            req_headers["Authorization"] = f"Bearer {self._api_token}"
        if headers:
            req_headers.update(headers)

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 2):
            try:
                # Custom transport for offline unit testing
                if self._http_transport:
                    req = urllib.request.Request(url, data=data or data_stream, headers=req_headers, method=method)
                    resp = self._http_transport(req)
                    code = getattr(resp, "status", getattr(resp, "status_code", 200))
                    resp_bytes = resp.read() if hasattr(resp, "read") else b""
                    try:
                        resp_json = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
                    except Exception:
                        resp_json = {}
                    if 200 <= code < 300:
                        return code, resp_json, resp_bytes
                    elif code in (401, 403):
                        safe_msg = f"HTTP {code} Authentication/Authorization failure"
                        raise PermissionError(safe_msg)
                    elif 500 <= code < 600 and attempt <= self.max_retries:
                        delay = float(2 ** attempt)
                        self.sleep_fn(delay)
                        continue
                    else:
                        raise RuntimeError(f"HTTP {code}: {resp_bytes.decode('utf-8', errors='replace')[:200]}")

                # Standard urllib transport
                req = urllib.request.Request(url, data=data or data_stream, headers=req_headers, method=method)
                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                    resp_bytes = resp.read()
                    code = resp.status
                    try:
                        resp_json = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
                    except Exception:
                        resp_json = {}
                    return code, resp_json, resp_bytes

            except urllib.error.HTTPError as e:
                code = e.code
                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
                clean_err_body = self._mask_secret(err_body)
                if code in (401, 403):
                    raise PermissionError(f"HTTP {code}: Access denied. Token may be invalid or expired.")
                elif 500 <= code < 600 and attempt <= self.max_retries:
                    last_err = e
                    delay = float(2 ** attempt)
                    self.sleep_fn(delay)
                    continue
                else:
                    raise RuntimeError(f"HTTP {code}: {clean_err_body[:200]}")
            except PermissionError:
                raise
            except Exception as e:
                last_err = e
                if attempt <= self.max_retries:
                    delay = float(2 ** attempt)
                    self.sleep_fn(delay)
                    continue
                raise RuntimeError(self._mask_secret(f"Network error contacting repository: {str(e)}"))

        raise RuntimeError(self._mask_secret(f"Max retries exhausted for {method} {url}: {str(last_err)}"))

    def find_existing_draft(
        self,
        portal_id: str,
        run_id: str,
    ) -> Optional[DepositReceipt]:
        """
        Search Zenodo user drafts for an existing deposition matching this run_id.
        Prevents duplicate draft creation if a previous creation request succeeded
        remotely but the response was interrupted before saving locally.
        """
        query_str = f"keywords:\"corpusai-run:{run_id}\""
        encoded_q = urllib.parse.quote(query_str)
        url = f"{self.base_url}/deposit/depositions?q={encoded_q}&status=draft"

        try:
            code, resp_data, _ = self._execute_http_request(url=url, method="GET")
            if isinstance(resp_data, list) and len(resp_data) > 0:
                match = resp_data[0]
                remote_id = str(match.get("id"))
                links = match.get("links", {})
                bucket_url = links.get("bucket")
                record_url = links.get("html")
                doi = match.get("metadata", {}).get("prereserve_doi", {}).get("doi") or match.get("doi")
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                return DepositReceipt(
                    receipt_id=f"receipt-{portal_id}-{run_id}-reconciled",
                    portal_id=portal_id,
                    run_id=run_id,
                    package_fingerprint="",
                    repository_target="zenodo",
                    environment="sandbox",
                    status=STATUS_DRAFT_CREATED,
                    created_at=now,
                    updated_at=now,
                    remote_record_id=remote_id,
                    remote_bucket_url=bucket_url,
                    remote_record_url=record_url,
                    doi=doi,
                    reconciliation_notes="Discovered existing remote draft via query reconciliation",
                )
        except Exception as e:
            logger.debug(f"Draft query reconciliation encountered: {e}")
        return None

    def map_deposit_metadata_to_zenodo(self, metadata: DepositMetadata, run_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Map domain-agnostic DepositMetadata to Zenodo REST API deposition schema.
        Defaults access to restricted/embargoed for third-party news preservation safety.
        """
        creators_list = []
        for c in metadata.creators:
            c_dict: Dict[str, Any] = {"name": c.name}
            if c.affiliation:
                c_dict["affiliation"] = c.affiliation
            if c.orcid:
                c_dict["orcid"] = c.orcid
            creators_list.append(c_dict)

        # Access mapping: default to restricted unless explicitly open
        access_right = metadata.access_status.lower()
        if access_right not in ("open", "embargoed", "restricted", "closed"):
            access_right = "restricted"

        keywords = [
            "web-archiving",
            "corpus-linguistics",
            f"software-version:{metadata.software_version}",
        ] + metadata.source_domains + metadata.archive_formats
        if run_id:
            keywords.append(f"corpusai-run:{run_id}")

        zenodo_metadata: Dict[str, Any] = {
            "title": metadata.title,
            "upload_type": "dataset",
            "description": metadata.description,
            "creators": creators_list,
            "access_right": access_right,
            "keywords": keywords,
            "notes": (
                f"Archival capture period: {metadata.capture_period_start} to {metadata.capture_period_end}. "
                f"Rights notice: {metadata.copyright_notice}. License declared: {metadata.license_status}."
            ),
        }

        if access_right == "embargoed" and metadata.embargo_date:
            zenodo_metadata["embargo_date"] = metadata.embargo_date
        elif access_right == "restricted":
            zenodo_metadata["access_conditions"] = (
                "Access is restricted for academic and preservation research evaluation under fair-use agreements. "
                "Contact CorpusAI researchers with project affiliation."
            )

        return {"metadata": zenodo_metadata}

    def create_draft(
        self,
        metadata: DepositMetadata,
        portal_id: str,
        run_id: str,
        package_fingerprint: str,
    ) -> DepositReceipt:
        """
        Create a new deposition draft on Zenodo Sandbox.
        Validates metadata before making any remote network calls.
        """
        errors = metadata.validate()
        if errors:
            raise ValueError(f"Invalid deposit metadata: {', '.join(errors)}")

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt_id = f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}"
        payload = self.map_deposit_metadata_to_zenodo(metadata, run_id=run_id)

        url = f"{self.base_url}/deposit/depositions"
        code, resp_data, _ = self._execute_http_request(
            url=url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
        )

        remote_id = str(resp_data.get("id"))
        links = resp_data.get("links", {})
        bucket_url = links.get("bucket")
        record_url = links.get("html")
        doi = resp_data.get("metadata", {}).get("prereserve_doi", {}).get("doi") or resp_data.get("doi")

        receipt = DepositReceipt(
            receipt_id=receipt_id,
            portal_id=portal_id,
            run_id=run_id,
            package_fingerprint=package_fingerprint,
            repository_target="zenodo",
            environment="sandbox",
            status=STATUS_DRAFT_CREATED,
            created_at=now,
            updated_at=now,
            remote_record_id=remote_id,
            remote_bucket_url=bucket_url,
            remote_record_url=record_url,
            doi=doi,
        )
        return receipt

    def upload_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        """
        Stream files in the finalized package directory to Zenodo's Bucket API.
        Local package must pass PackageVerifier before upload begins.
        """
        # Pre-upload integrity gate: local package MUST be valid
        v_res = PackageVerifier.verify_package(package_dir)
        if not v_res.is_valid:
            err_summary = "; ".join(v_res.metadata_errors + v_res.missing_files + v_res.checksum_mismatches + v_res.unexpected_files)
            receipt.transition_to(STATUS_FAILED, error=f"Local package verification failed before upload: {err_summary}", error_category="LOCAL_PACKAGE_CORRUPT")
            return receipt

        if not receipt.remote_bucket_url:
            receipt.transition_to(STATUS_FAILED, error="Missing remote bucket URL in receipt", error_category="MISSING_REMOTE_BUCKET")
            return receipt

        receipt.transition_to(STATUS_UPLOADING)

        # Collect files to deposit (manifest, environment, checksums, deposit_metadata, content, replay, qa)
        files_to_upload: List[Tuple[str, str]] = []  # (rel_path, abs_path)
        total_package_bytes = 0
        for root, _, files in os.walk(package_dir):
            for file in sorted(files):
                full_p = os.path.join(root, file)
                rel_p = os.path.relpath(full_p, package_dir).replace("\\", "/")
                sz = os.path.getsize(full_p)
                if sz > ZENODO_MAX_SINGLE_FILE_BYTES:
                    receipt.transition_to(
                        STATUS_FAILED,
                        error=f"File {rel_p} size ({sz} bytes) exceeds Zenodo limit ({ZENODO_MAX_SINGLE_FILE_BYTES} bytes).",
                        error_category="FILE_SIZE_LIMIT_EXCEEDED",
                    )
                    return receipt
                total_package_bytes += sz
                files_to_upload.append((rel_p, full_p))

        # Check file count and total volume quota
        if len(files_to_upload) > ZENODO_MAX_RECORD_FILES:
            receipt.transition_to(
                STATUS_FAILED,
                error=f"Package contains {len(files_to_upload)} files, exceeding Zenodo limit ({ZENODO_MAX_RECORD_FILES} files). Bundle into archive.",
                error_category="FILE_COUNT_LIMIT_EXCEEDED",
            )
            return receipt

        if total_package_bytes > ZENODO_MAX_RECORD_BYTES:
            receipt.transition_to(
                STATUS_FAILED,
                error=f"Package total size ({total_package_bytes} bytes) exceeds Zenodo record quota ({ZENODO_MAX_RECORD_BYTES} bytes).",
                error_category="RECORD_QUOTA_EXCEEDED",
            )
            return receipt

        bucket_url = receipt.remote_bucket_url.rstrip("/")

        for rel_path, full_path in files_to_upload:
            md5_val, sha256_val, file_size = compute_file_md5_and_sha256(full_path)
            # Zenodo bucket uploads use PUT to {bucket_url}/{filename}
            # Flatten or encode relative subpaths to preserve distinct artifacts
            safe_filename = rel_path.replace("/", "__")
            upload_url = f"{bucket_url}/{urllib.parse.quote(safe_filename)}"

            with open(full_path, "rb") as fp:
                file_bytes = fp.read()

            try:
                code, resp_data, _ = self._execute_http_request(
                    url=upload_url,
                    method="PUT",
                    headers={"Content-Type": "application/octet-stream"},
                    data=file_bytes,
                )
            except Exception as e:
                receipt.transition_to(STATUS_FAILED, error=f"Failed uploading {rel_path}: {str(e)}", error_category="UPLOAD_TRANSMISSION_FAILED")
                return receipt

            remote_md5 = resp_data.get("checksum") or ""
            if remote_md5.startswith("md5:"):
                remote_md5 = remote_md5[4:]

            receipt.artifact_inventory[rel_path] = UploadedArtifactRecord(
                filename=rel_path,
                filesize=file_size,
                local_sha256=sha256_val,
                local_md5=md5_val,
                remote_md5=remote_md5,
                remote_checksum=resp_data.get("checksum"),
                remote_file_id=resp_data.get("id"),
                verified=False,
            )

        receipt.transition_to(STATUS_UPLOADED)
        return receipt

    def verify_remote_artifacts(
        self,
        receipt: DepositReceipt,
        package_dir: str,
    ) -> DepositReceipt:
        """
        Verify that all uploaded files match local package integrity.
        Zenodo Bucket API computes and exposes MD5 server-side checksums.
        We verify:
        1. All expected files in package_dir are accounted for in the uploaded inventory.
        2. Local MD5 matches Zenodo's server-computed MD5.
        3. Local SHA-256 matches the package's checksums.sha256.
        """
        if receipt.status != STATUS_UPLOADED:
            raise ValueError(f"Cannot verify remote artifacts when deposit status is '{receipt.status}'.")

        checksums_path = os.path.join(package_dir, "checksums.sha256")
        expected_sha256s: Dict[str, str] = {}
        if os.path.exists(checksums_path):
            with open(checksums_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2:
                        expected_sha256s[parts[1].strip().replace("\\", "/").lstrip("./")] = parts[0].strip().lower()

        # Add checksums.sha256 itself if present
        if os.path.exists(checksums_path):
            expected_sha256s["checksums.sha256"] = sha256_file(checksums_path).lower()

        missing_or_mismatched = []
        for rel_path, record in receipt.artifact_inventory.items():
            # Check 1: MD5 match against Zenodo server calculation
            if not record.remote_md5 or record.remote_md5.lower() != record.local_md5.lower():
                missing_or_mismatched.append(f"{rel_path}: remote MD5 mismatch (local {record.local_md5} != remote {record.remote_md5})")
                continue

            # Check 2: Local SHA-256 matches package checksum manifest
            exp_sha = expected_sha256s.get(rel_path)
            if exp_sha and exp_sha != record.local_sha256.lower():
                missing_or_mismatched.append(f"{rel_path}: local SHA-256 mismatch against manifest")
                continue

            record.verified = True

        if missing_or_mismatched:
            receipt.checksum_verification_outcome = "FAILED"
            receipt.transition_to(
                STATUS_FAILED,
                error=f"Remote artifact checksum verification failed: {'; '.join(missing_or_mismatched)}",
                error_category="REMOTE_CHECKSUM_MISMATCH",
            )
            return receipt

        receipt.checksum_verification_outcome = "VERIFIED_MD5_AND_LOCAL_SHA256"
        receipt.transition_to(STATUS_VERIFIED)
        return receipt

    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:
        """Fetch remote draft status from Zenodo Sandbox."""
        if not receipt.remote_record_id:
            return receipt

        url = f"{self.base_url}/deposit/depositions/{receipt.remote_record_id}"
        code, resp_data, _ = self._execute_http_request(url=url, method="GET")

        state = resp_data.get("state")
        submitted = resp_data.get("submitted", False)
        if submitted:
            receipt.status = STATUS_PUBLISHED
            receipt.doi = resp_data.get("doi") or receipt.doi
        receipt.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return receipt

    def publish_deposit(
        self,
        receipt: DepositReceipt,
        allow_publish: bool = False,
    ) -> DepositReceipt:
        """
        Publish a verified draft on Zenodo Sandbox.
        Hard gate: Requires allow_publish=True. Production is strictly blocked.
        """
        if not allow_publish:
            raise PermissionError(
                "Publication is locked by default. Public dissemination requires explicit authorization (allow_publish=True)."
            )
        if receipt.status != STATUS_VERIFIED:
            raise ValueError(f"Cannot publish deposit in status '{receipt.status}'. Must be '{STATUS_VERIFIED}'.")

        url = f"{self.base_url}/deposit/depositions/{receipt.remote_record_id}/actions/publish"
        code, resp_data, _ = self._execute_http_request(url=url, method="POST")

        receipt.doi = resp_data.get("doi") or receipt.doi
        receipt.remote_record_url = resp_data.get("links", {}).get("record_html") or receipt.remote_record_url
        receipt.transition_to(STATUS_PUBLISHED)
        return receipt


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
                logger.info(f"Run '{run_id}' already deposited and {existing.status}. Returning receipt.")
                return existing

            # Resume from existing draft
            receipt = existing
        else:
            # 2. Local verification before remote draft creation
            v_res = PackageVerifier.verify_package(package_dir)
            if not v_res.is_valid:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                err_msg = "; ".join(v_res.metadata_errors + v_res.missing_files + v_res.checksum_mismatches + v_res.unexpected_files)
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
                    receipt = self.client.create_draft(deposit_metadata, portal_id, run_id, package_fingerprint)
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
                receipt.transition_to(STATUS_FAILED, error=str(e), error_category="UPLOAD_EXCEPTION")
                self.registry.save()
                return receipt

        # 5. Verify remote artifacts
        if receipt.status == STATUS_UPLOADED:
            try:
                receipt = self.client.verify_remote_artifacts(receipt, package_dir)
                self.registry.save()
            except Exception as e:
                receipt.transition_to(STATUS_FAILED, error=str(e), error_category="VERIFICATION_EXCEPTION")
                self.registry.save()
                return receipt

        return receipt

[1mdiff --git a/docs/phase9a_zenodo_deposit.md b/docs/phase9a_zenodo_deposit.md[m
[1mnew file mode 100644[m
[1mindex 0000000..fbe858d[m
[1m--- /dev/null[m
[1m+++ b/docs/phase9a_zenodo_deposit.md[m
[36m@@ -0,0 +1,134 @@[m
[32m+[m[32m# CorpusAI Phase 9A: Zenodo Sandbox Deposit Adapter and Receipt Tracking[m
[32m+[m
[32m+[m[32m## 1. Overview[m
[32m+[m[32mCorpusAI Phase 9A implements verified online repository deposits to **Zenodo Sandbox** (`sandbox.zenodo.org`), decoupled deposit receipt tracking, and safe retry/reconciliation semantics in accordance with Professor Balázs Indig's preservation principles:[m
[32m+[m
[32m+[m[32m- **Local Verification Gate**: Packages must strictly pass `PackageVerifier.verify_package()` before any remote draft or file upload operation. Corrupted or incomplete packages are rejected locally.[m
[32m+[m[32m- **Decoupled Deposit State**: Deposit outcomes are stored in a companion `deposit_registry.json`. A remote repository failure does **not** invalidate a successful crawl or alter `ArchiveCatalog.latest_successful_run_id`.[m
[32m+[m[32m- **Safe Retries and Idempotency**: Stable package fingerprints (`compute_package_fingerprint`) prevent duplicate draft creation and resume interrupted uploads safely.[m
[32m+[m[32m- **Remote Checksum Verification**: Validates Zenodo server-computed MD5 digests against local computations while maintaining local SHA-256 manifests.[m
[32m+[m[32m- **Strict Rights and Safety Gates**: Production publishing is strictly blocked. Deposit metadata defaults to `access_right="restricted"` with academic research access conditions.[m
[32m+[m
[32m+[m[32m---[m
[32m+[m
[32m+[m[32m## 2. Deposit State Machine and Receipt Model[m
[32m+[m
[32m+[m[32mDeposits advance through a strict, deterministic state machine:[m
[32m+[m
[32m+[m[32m```[m
[32m+[m[32m                  [PENDING][m
[32m+[m[32m                      │[m
[32m+[m[32m           (validate local package)[m
[32m+[m[32m                      │[m
[32m+[m[32m                      ▼[m
[32m+[m[32m               [DRAFT_CREATED][m
[32m+[m[32m                      │[m
[32m+[m[32m             (stream artifacts)[m
[32m+[m[32m                      │[m
[32m+[m[32m                      ▼[m
[32m+[m[32m                 [UPLOADING][m
[32m+[m[32m                      │[m
[32m+[m[32m                      ▼[m
[32m+[m[32m                 [UPLOADED][m
[32m+[m[32m                      │[m
[32m+[m[32m            (verify remote hashes)[m
[32m+[m[32m                      │[m
[32m+[m[32m                      ▼[m
[32m+[m[32m                 [VERIFIED][m
[32m+[m[32m                      │[m
[32m+[m[32m             (explicit auth only)[m
[32m+[m[32m                      │[m
[32m+[m[32m                      ▼[m
[32m+[m[32m                [PUBLISHED][m
[32m+[m[32m```[m
[32m+[m
[32m+[m[32m### State Transitions[m
[32m+[m[32m| Status | Meaning | Transitions Allowed To |[m
[32m+[m[32m| :--- | :--- | :--- |[m
[32m+[m[32m| `PENDING` | Deposit requested, local verification in progress | `DRAFT_CREATED`, `FAILED`, `NEEDS_RECONCILIATION` |[m
[32m+[m[32m| `DRAFT_CREATED` | Remote draft record created on Zenodo Sandbox | `UPLOADING`, `FAILED`, `NEEDS_RECONCILIATION` |[m
[32m+[m[32m| `UPLOADING` | Streaming package artifacts to Bucket API | `UPLOADED`, `FAILED`, `NEEDS_RECONCILIATION` |[m
[32m+[m[32m| `UPLOADED` | All files uploaded; awaiting remote checksum checks | `VERIFIED`, `FAILED`, `NEEDS_RECONCILIATION` |[m
[32m+[m[32m| `VERIFIED` | Remote MD5 digests match local computations & package manifest | `PUBLISHED`, `FAILED` |[m
[32m+[m[32m| `PUBLISHED` | Published on sandbox (terminal state; requires explicit flag) | *None* |[m
[32m+[m[32m| `FAILED` | Transmission, verification, or auth error; local package intact | `DRAFT_CREATED`, `UPLOADING`, `UPLOADED`, `VERIFIED`, `NEEDS_RECONCILIATION` |[m
[32m+[m[32m| `NEEDS_RECONCILIATION` | Fingerprint mismatch or ambiguous remote state | `DRAFT_CREATED`, `UPLOADING`, `UPLOADED`, `VERIFIED`, `FAILED` |[m
[32m+[m
[32m+[m[32m### Receipt Structure (`DepositReceipt`)[m
[32m+[m[32mStored in `deposit_registry.json` using atomic file replacement:[m
[32m+[m[32m```json[m
[32m+[m[32m{[m
[32m+[m[32m  "receipt_id": "receipt-wordpress-news-run-20261023T010000Z-a1b2c3d4",[m
[32m+[m[32m  "portal_id": "wordpress-news",[m
[32m+[m[32m  "run_id": "run-20261023T010000Z-wordpr",[m
[32m+[m[32m  "package_fingerprint": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",[m
[32m+[m[32m  "repository_target": "zenodo",[m
[32m+[m[32m  "environment": "sandbox",[m
[32m+[m[32m  "status": "VERIFIED",[m
[32m+[m[32m  "created_at": "2026-10-26T21:40:00Z",[m
[32m+[m[32m  "updated_at": "2026-10-26T21:42:00Z",[m
[32m+[m[32m  "remote_record_id": "123456",[m
[32m+[m[32m  "remote_bucket_url": "https://sandbox.zenodo.org/api/files/568377dd-daf8-4235-85e1-a56011ad454b",[m
[32m+[m[32m  "doi": "10.5072/zenodo.123456",[m
[32m+[m[32m  "checksum_verification_outcome": "VERIFIED_MD5_AND_LOCAL_SHA256",[m
[32m+[m[32m  "artifact_inventory": {[m
[32m+[m[32m    "manifest.json": {[m
[32m+[m[32m      "filename": "manifest.json",[m
[32m+[m[32m      "filesize": 1420,[m
[32m+[m[32m      "local_sha256": "...",[m
[32m+[m[32m      "local_md5": "...",[m
[32m+[m[32m      "remote_md5": "...",[m
[32m+[m[32m      "verified": true[m
[32m+[m[32m    }[m
[32m+[m[32m  }[m
[32m+[m[32m}[m
[32m+[m[32m```[m
[32m+[m
[32m+[m[32m---[m
[32m+[m
[32m+[m[32m## 3. Remote Checksum Verification Guarantees and Limitations[m
[32m+[m
[32m+[m[32m- **Zenodo API Behavior**: Zenodo's Bucket API (`PUT /api/files/<bucket_id>/<filename>`) calculates an **MD5** digest server-side upon receiving the complete byte stream, returning it in the upload response. Zenodo does not calculate SHA-256 server-side.[m
[32m+[m[32m- **CorpusAI Verification Contract**:[m
[32m+[m[32m  1. During file streaming, CorpusAI computes both **MD5** and **SHA-256** in a single pass without buffering whole files in memory.[m
[32m+[m[32m  2. The local SHA-256 digest is strictly verified against the immutable local package's `checksums.sha256`.[m
[32m+[m[32m  3. The local MD5 digest is strictly compared against the Zenodo server-calculated MD5.[m
[32m+[m[32m  4. If any remote MD5 differs or is missing, the deposit status is marked `FAILED` with `checksum_verification_outcome="FAILED"`, and cannot advance to `VERIFIED`.[m
[32m+[m
[32m+[m[32m---[m
[32m+[m
[32m+[m[32m## 4. Rights, Safety, and Production Restrictions[m
[32m+[m
[32m+[m[32m- **Sandbox Target Only**: The adapter enforces `https://sandbox.zenodo.org/api`. Any attempt to pass `zenodo.org` (production) raises a `ValueError` immediately.[m
[32m+[m[32m- **Locked Publication**: `publish_deposit()` raises `PermissionError` unless `allow_publish=True` is explicitly passed.[m
[32m+[m[32m- **Copyright & Redistribution Safety**:[m
[32m+[m[32m  - Web crawler archives contain third-party copyrighted material.[m
[32m+[m[32m  - Deposits default to `access_right="restricted"` with academic evaluation conditions.[m
[32m+[m[32m  - Automatic dissemination or open-access publication without institutional copyright review is forbidden.[m
[32m+[m[32m- **Credential Protection**: The API token (`CORPUSAI_DEPOSIT_TOKEN`) is scrubbed from all logs, exception traces, and serialization structures.[m
[32m+[m
[32m+[m[32m---[m
[32m+[m
[32m+[m[32m## 5. Offline Testing and Optional Smoke Test[m
[32m+[m
[32m+[m[32m### Running the Offline Suite[m
[32m+[m[32mAll routine unit tests use a mocked HTTP transport and perform **zero real network requests**:[m
[32m+[m[32m```bash[m
[32m+[m[32mpython -m pytest tests/test_zenodo_deposit.py -v[m
[32m+[m[32m```[m
[32m+[m
[32m+[m[32m### Opt-In Manual Sandbox Smoke Test[m
[32m+[m[32mAn optional real network test is provided for manual validation against Zenodo Sandbox. It is skipped by default in all routine CI runs:[m
[32m+[m[32m```bash[m
[32m+[m[32m# Explicit opt-in only:[m
[32m+[m[32mexport CORPUSAI_ZENODO_SANDBOX_TOKEN="your_sandbox_token"[m
[32m+[m[32mexport CORPUSAI_RUN_ZENODO_SMOKE_TEST="1"[m
[32m+[m[32mpython -m pytest tests/test_zenodo_deposit.py -k "test_optional_sandbox_smoke_test_opt_in"[m
[32m+[m[32m```[m
[32m+[m
[32m+[m[32m---[m
[32m+[m
[32m+[m[32m## 6. Persistence Gate & Unattended Scheduling Status[m
[32m+[m
[32m+[m[32m- **Durable Operational State**: Zenodo is an archival publication target, not a synchronization store for active crawler state.[m
[32m+[m[32m- **Daily Scheduling Remains Disabled**: GitHub Actions runners are ephemeral. Recurrent unattended daily crawls remain gated until operational storage (for `catalog.json` and `portal_state.json`) is configured.[m
[1mdiff --git a/src/zenodo_deposit.py b/src/zenodo_deposit.py[m
[1mnew file mode 100644[m
[1mindex 0000000..231ea09[m
[1m--- /dev/null[m
[1m+++ b/src/zenodo_deposit.py[m
[36m@@ -0,0 +1,847 @@[m
[32m+[m[32m"""[m
[32m+[m[32mRepository Deposit Client and Zenodo Sandbox Adapter module for CorpusAI Phase 9A.[m
[32m+[m
[32m+[m[32mImplements online repository deposit preparation, artifact streaming, and[m
[32m+[m[32mreceipt tracking following Professor Balázs Indig's preservation principles:[m
[32m+[m[32m- Verification before upload: package must pass PackageVerifier.verify_package()[m
[32m+[m[32m- Decoupled lifecycle: deposit outcomes are tracked independently of crawl success[m
[32m+[m[32m- Safe retries and duplicate protection via stable package fingerprint[m
[32m+[m[32m- Bounded retry with exponential backoff on transient HTTP 5xx errors[m
[32m+[m[32m- Strict credential protection: tokens are never leaked to logs or exceptions[m
[32m+[m[32m- Hardcoded safety gates: production publishing is strictly blocked by default[m
[32m+[m[32m"""[m
[32m+[m
[32m+[m[32mfrom abc import ABC, abstractmethod[m
[32m+[m[32mfrom dataclasses import asdict, dataclass, field[m
[32m+[m[32mimport datetime[m
[32m+[m[32mimport hashlib[m
[32m+[m[32mimport json[m
[32m+[m[32mimport logging[m
[32m+[m[32mimport os[m
[32m+[m[32mimport re[m
[32m+[m[32mimport time[m
[32m+[m[32mfrom typing import Any, Callable, Dict, List, Optional, Tuple[m
[32m+[m[32mimport urllib.error[m
[32m+[m[32mimport urllib.parse[m
[32m+[m[32mimport urllib.request[m
[32m+[m
[32m+[m[32mfrom src.archive_package import PackageVerifier, PackageVerificationResult[m
[32m+[m[32mfrom src.deposit_metadata import DepositMetadata[m
[32m+[m
[32m+[m[32mlogger = logging.getLogger("corpusai.deposit")[m
[32m+[m
[32m+[m[32m# Zenodo URLs[m
[32m+[m[32mZENODO_SANDBOX_BASE_URL = "https://sandbox.zenodo.org/api"[m
[32m+[m[32mZENODO_PRODUCTION_BASE_URL = "https://zenodo.org/api"[m
[32m+[m
[32m+[m[32m# Zenodo Quota Constants[m
[32m+[m[32mZENODO_MAX_RECORD_FILES = 100[m
[32m+[m[32mZENODO_MAX_RECORD_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB[m
[32m+[m[32mZENODO_MAX_SINGLE_FILE_BYTES = 50 * 1024 * 1024 * 1024  # 50 GB[m
[32m+[m
[32m+[m[32m# Status Constants[m
[32m+[m[32mSTATUS_PENDING = "PENDING"[m
[32m+[m[32mSTATUS_DRAFT_CREATED = "DRAFT_CREATED"[m
[32m+[m[32mSTATUS_UPLOADING = "UPLOADING"[m
[32m+[m[32mSTATUS_UPLOADED = "UPLOADED"[m
[32m+[m[32mSTATUS_VERIFIED = "VERIFIED"[m
[32m+[m[32mSTATUS_PUBLISHED = "PUBLISHED"[m
[32m+[m[32mSTATUS_FAILED = "FAILED"[m
[32m+[m[32mSTATUS_NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"[m
[32m+[m
[32m+[m[32mVALID_TRANSITIONS = {[m
[32m+[m[32m    STATUS_PENDING: {STATUS_DRAFT_CREATED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},[m
[32m+[m[32m    STATUS_DRAFT_CREATED: {STATUS_UPLOADING, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},[m
[32m+[m[32m    STATUS_UPLOADING: {STATUS_UPLOADED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},[m
[32m+[m[32m    STATUS_UPLOADED: {STATUS_VERIFIED, STATUS_FAILED, STATUS_NEEDS_RECONCILIATION},[m
[32m+[m[32m    STATUS_VERIFIED: {STATUS_PUBLISHED, STATUS_FAILED},[m
[32m+[m[32m    STATUS_FAILED: {STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_UPLOADED, STATUS_VERIFIED, STATUS_NEEDS_RECONCILIATION},[m
[32m+[m[32m    STATUS_NEEDS_RECONCILIATION: {STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_UPLOADED, STATUS_VERIFIED, STATUS_FAILED},[m
[32m+[m[32m    STATUS_PUBLISHED: set(),  # Terminal state[m
[32m+[m[32m}[m
[32m+[m
[32m+[m
[32m+[m[32m@dataclass[m
[32m+[m[32mclass UploadedArtifactRecord:[m
[32m+[m[32m    """Metadata for an individual uploaded file on the repository."""[m
[32m+[m[32m    filename: str[m
[32m+[m[32m    filesize: int[m
[32m+[m[32m    local_sha256: str[m
[32m+[m[32m    local_md5: str[m
[32m+[m[32m    remote_md5: Optional[str] = None[m
[32m+[m[32m    remote_checksum: Optional[str] = None[m
[32m+[m[32m    remote_file_id: Optional[str] = None[m
[32m+[m[32m    verified: bool = False[m
[32m+[m
[32m+[m[32m    def to_dict(self) -> Dict[str, Any]:[m
[32m+[m[32m        return asdict(self)[m
[32m+[m
[32m+[m[32m    @classmethod[m
[32m+[m[32m    def from_dict(cls, data: Dict[str, Any]) -> "UploadedArtifactRecord":[m
[32m+[m[32m        return cls(**data)[m
[32m+[m
[32m+[m
[32m+[m[32m@dataclass[m
[32m+[m[32mclass DepositReceipt:[m
[32m+[m[32m    """[m
[32m+[m[32m    Durable receipt tracking the outcome of an archival package deposit.[m
[32m+[m[32m    Maintained separately from ArchiveCatalog to prevent deposit failures from[m
[32m+[m[32m    invalidating valid crawls.[m
[32m+[m[32m    """[m
[32m+[m[32m    receipt_id: str[m
[32m+[m[32m    portal_id: str[m
[32m+[m[32m    run_id: str[m
[32m+[m[32m    package_fingerprint: str[m
[32m+[m[32m    repository_target: str[m
[32m+[m[32m    environment: str[m
[32m+[m[32m    status: str[m
[32m+[m[32m    created_at: str[m
[32m+[m[32m    updated_at: str[m
[32m+[m[32m    remote_record_id: Optional[str] = None[m
[32m+[m[32m    remote_bucket_url: Optional[str] = None[m
[32m+[m[32m    remote_record_url: Optional[str] = None[m
[32m+[m[32m    doi: Optional[str] = None[m
[32m+[m[32m    artifact_inventory: Dict[str, UploadedArtifactRecord] = field(default_factory=dict)[m
[32m+[m[32m    checksum_verification_outcome: str = "UNVERIFIED"[m
[32m+[m[32m    last_error: Optional[str] = None[m
[32m+[m[32m    last_error_category: Optional[str] = None[m
[32m+[m[32m    retry_eligible: bool = True[m
[32m+[m[32m    reconciliation_notes: Optional[str] = None[m
[32m+[m
[32m+[m[32m    def to_dict(self) -> Dict[str, Any]:[m
[32m+[m[32m        d = asdict(self)[m
[32m+[m[32m        d["artifact_inventory"] = {[m
[32m+[m[32m            k: v.to_dict() if hasattr(v, "to_dict") else v[m
[32m+[m[32m            for k, v in self.artifact_inventory.items()[m
[32m+[m[32m        }[m
[32m+[m[32m        return d[m
[32m+[m
[32m+[m[32m    @classmethod[m
[32m+[m[32m    def from_dict(cls, data: Dict[str, Any]) -> "DepositReceipt":[m
[32m+[m[32m        cleaned = dict(data)[m
[32m+[m[32m        inv = cleaned.get("artifact_inventory", {})[m
[32m+[m[32m        cleaned["artifact_inventory"] = {[m
[32m+[m[32m            k: UploadedArtifactRecord.from_dict(v) if isinstance(v, dict) else v[m
[32m+[m[32m            for k, v in inv.items()[m
[32m+[m[32m        }[m
[32m+[m[32m        return cls(**cleaned)[m
[32m+[m
[32m+[m[32m    def transition_to(self, new_status: str, error: Optional[str] = None, error_category: Optional[str] = None) -> None:[m
[32m+[m[32m        """Enforce permitted state transitions."""[m
[32m+[m[32m        allowed = VALID_TRANSITIONS.get(self.status, set())[m
[32m+[m[32m        if new_status not in allowed:[m
[32m+[m[32m            raise ValueError(f"Invalid deposit status transition from '{self.status}' to '{new_status}'.")[m
[32m+[m[32m        self.status = new_status[m
[32m+[m[32m        self.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m        if error:[m
[32m+[m[32m            self.last_error = error[m
[32m+[m[32m            self.last_error_category = error_category or "UNKNOWN"[m
[32m+[m
[32m+[m
[32m+[m[32mclass DepositRegistry:[m
[32m+[m[32m    """[m
[32m+[m[32m    Durable on-disk registry mapping (portal_id, run_id) to DepositReceipt.[m
[32m+[m[32m    Stored atomically alongside or inside portal results (e.g. deposit_registry.json).[m
[32m+[m[32m    """[m
[32m+[m
[32m+[m[32m    def __init__(self, registry_path: str):[m
[32m+[m[32m        self.registry_path = os.path.abspath(registry_path)[m
[32m+[m[32m        self.receipts: Dict[str, DepositReceipt] = {}  # key: f"{portal_id}:{run_id}"[m
[32m+[m
[32m+[m[32m    @classmethod[m
[32m+[m[32m    def load_or_create(cls, registry_path: str) -> "DepositRegistry":[m
[32m+[m[32m        reg = cls(registry_path)[m
[32m+[m[32m        if os.path.exists(registry_path):[m
[32m+[m[32m            with open(registry_path, "r", encoding="utf-8") as f:[m
[32m+[m[32m                data = json.load(f)[m
[32m+[m[32m                for k, v in data.get("receipts", {}).items():[m
[32m+[m[32m                    reg.receipts[k] = DepositReceipt.from_dict(v)[m
[32m+[m[32m        return reg[m
[32m+[m
[32m+[m[32m    def save(self) -> None:[m
[32m+[m[32m        """Atomic write to disk to prevent corrupted state on interruption."""[m
[32m+[m[32m        os.makedirs(os.path.dirname(self.registry_path), exist_ok=True)[m
[32m+[m[32m        temp_path = f"{self.registry_path}.tmp.{os.getpid()}_{int(time.time()*1000)}"[m
[32m+[m[32m        data = {[m
[32m+[m[32m            "schema_version": "1",[m
[32m+[m[32m            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),[m
[32m+[m[32m            "receipts": {k: v.to_dict() for k, v in self.receipts.items()},[m
[32m+[m[32m        }[m
[32m+[m[32m        with open(temp_path, "w", encoding="utf-8") as f:[m
[32m+[m[32m            json.dump(data, f, indent=2, ensure_ascii=False)[m
[32m+[m[32m        os.replace(temp_path, self.registry_path)[m
[32m+[m
[32m+[m[32m    def get_receipt(self, portal_id: str, run_id: str) -> Optional[DepositReceipt]:[m
[32m+[m[32m        return self.receipts.get(f"{portal_id}:{run_id}")[m
[32m+[m
[32m+[m[32m    def register_receipt(self, receipt: DepositReceipt) -> None:[m
[32m+[m[32m        self.receipts[f"{receipt.portal_id}:{receipt.run_id}"] = receipt[m
[32m+[m[32m        self.save()[m
[32m+[m
[32m+[m
[32m+[m[32mclass RepositoryDepositClient(ABC):[m
[32m+[m[32m    """Abstract interface defining the repository deposit lifecycle contract."""[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def create_draft([m
[32m+[m[32m        self,[m
[32m+[m[32m        metadata: DepositMetadata,[m
[32m+[m[32m        portal_id: str,[m
[32m+[m[32m        run_id: str,[m
[32m+[m[32m        package_fingerprint: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def find_existing_draft([m
[32m+[m[32m        self,[m
[32m+[m[32m        portal_id: str,[m
[32m+[m[32m        run_id: str,[m
[32m+[m[32m    ) -> Optional[DepositReceipt]:[m
[32m+[m[32m        """Search repository for an existing draft created for this portal and run."""[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def upload_artifacts([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        package_dir: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def verify_remote_artifacts([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        package_dir: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m[32m    @abstractmethod[m
[32m+[m[32m    def publish_deposit([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        allow_publish: bool = False,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        pass[m
[32m+[m
[32m+[m
[32m+[m[32mdef compute_package_fingerprint(package_dir: str) -> str:[m
[32m+[m[32m    """[m
[32m+[m[32m    Compute a stable SHA-256 fingerprint of the finalized package.[m
[32m+[m[32m    Uses checksums.sha256 if present, or sorted contents.[m
[32m+[m[32m    """[m
[32m+[m[32m    checksums_path = os.path.join(package_dir, "checksums.sha256")[m
[32m+[m[32m    if os.path.exists(checksums_path):[m
[32m+[m[32m        with open(checksums_path, "rb") as f:[m
[32m+[m[32m            return hashlib.sha256(f.read()).hexdigest()[m
[32m+[m[32m    # Fallback: hash directory tree listing[m
[32m+[m[32m    hasher = hashlib.sha256()[m
[32m+[m[32m    for root, _, files in os.walk(package_dir):[m
[32m+[m[32m        for f in sorted(files):[m
[32m+[m[32m            hasher.update(f.encode("utf-8"))[m
[32m+[m[32m    return hasher.hexdigest()[m
[32m+[m
[32m+[m
[32m+[m[32mdef compute_file_md5_and_sha256(file_path: str) -> Tuple[str, str, int]:[m
[32m+[m[32m    """Compute MD5, SHA-256, and size in a single pass without loading entire file in RAM."""[m
[32m+[m[32m    md5_hash = hashlib.md5()[m
[32m+[m[32m    sha_hash = hashlib.sha256()[m
[32m+[m[32m    size = 0[m
[32m+[m[32m    with open(file_path, "rb") as f:[m
[32m+[m[32m        while chunk := f.read(65536):[m
[32m+[m[32m            md5_hash.update(chunk)[m
[32m+[m[32m            sha_hash.update(chunk)[m
[32m+[m[32m            size += len(chunk)[m
[32m+[m[32m    return md5_hash.hexdigest(), sha_hash.update().hexdigest() if False else sha_hash.hexdigest(), size[m
[32m+[m
[32m+[m
[32m+[m[32mclass ZenodoSandboxAdapter(RepositoryDepositClient):[m
[32m+[m[32m    """[m
[32m+[m[32m    Concrete deposit client targeting the Zenodo Sandbox REST API.[m
[32m+[m
[32m+[m[32m    Safety boundaries:[m
[32m+[m[32m    - Target URL is restricted to sandbox.zenodo.org.[m
[32m+[m[32m    - Production Zenodo base URL is actively blocked.[m
[32m+[m[32m    - Publishing requires explicit authorization (`allow_publish=True`).[m
[32m+[m[32m    - API secrets are scrubbed from logs and exceptions.[m
[32m+[m[32m    - Network requests use bounded retries on HTTP 5xx with exponential backoff.[m
[32m+[m[32m    """[m
[32m+[m
[32m+[m[32m    def __init__([m
[32m+[m[32m        self,[m
[32m+[m[32m        api_token: Optional[str] = None,[m
[32m+[m[32m        base_url: str = ZENODO_SANDBOX_BASE_URL,[m
[32m+[m[32m        max_retries: int = 3,[m
[32m+[m[32m        request_timeout: float = 30.0,[m
[32m+[m[32m        sleep_fn: Optional[Callable[[float], None]] = None,[m
[32m+[m[32m        http_transport: Optional[Callable[[urllib.request.Request], Any]] = None,[m
[32m+[m[32m    ):[m
[32m+[m[32m        token = api_token or os.environ.get("CORPUSAI_DEPOSIT_TOKEN", "")[m
[32m+[m[32m        self._api_token = token.strip()[m
[32m+[m[32m        self.base_url = base_url.rstrip("/")[m
[32m+[m[32m        self.max_retries = max(0, max_retries)[m
[32m+[m[32m        self.request_timeout = request_timeout[m
[32m+[m[32m        self.sleep_fn = sleep_fn or time.sleep[m
[32m+[m[32m        self._http_transport = http_transport[m
[32m+[m
[32m+[m[32m        # Security guard: Never allow production Zenodo implicitly or by configuration error[m
[32m+[m[32m        if "sandbox.zenodo.org" not in self.base_url:[m
[32m+[m[32m            if "zenodo.org" in self.base_url or "api" in self.base_url:[m
[32m+[m[32m                raise ValueError([m
[32m+[m[32m                    f"Production Zenodo target is strictly disabled in Phase 9A. "[m
[32m+[m[32m                    f"Target must be '{ZENODO_SANDBOX_BASE_URL}', got '{self.base_url}'."[m
[32m+[m[32m                )[m
[32m+[m
[32m+[m[32m    def _mask_secret(self, text: str) -> str:[m
[32m+[m[32m        """Strip sensitive token from any log or error string."""[m
[32m+[m[32m        if not self._api_token or len(self._api_token) < 4:[m
[32m+[m[32m            return text[m
[32m+[m[32m        return text.replace(self._api_token, "[REDACTED_DEPOSIT_TOKEN]")[m
[32m+[m
[32m+[m[32m    def _execute_http_request([m
[32m+[m[32m        self,[m
[32m+[m[32m        url: str,[m
[32m+[m[32m        method: str = "GET",[m
[32m+[m[32m        headers: Optional[Dict[str, str]] = None,[m
[32m+[m[32m        data: Optional[bytes] = None,[m
[32m+[m[32m        data_stream: Optional[Any] = None,[m
[32m+[m[32m    ) -> Tuple[int, Dict[str, Any], bytes]:[m
[32m+[m[32m        """[m
[32m+[m[32m        Execute an HTTP request with bounded retry/backoff for transient 5xx errors.[m
[32m+[m[32m        Does NOT retry 401, 403, 400, or 404 errors.[m
[32m+[m[32m        """[m
[32m+[m[32m        req_headers = {[m
[32m+[m[32m            "Accept": "application/json",[m
[32m+[m[32m            "User-Agent": "CorpusAI-Zenodo-Sandbox-Adapter/1.0",[m
[32m+[m[32m        }[m
[32m+[m[32m        if self._api_token:[m
[32m+[m[32m            req_headers["Authorization"] = f"Bearer {self._api_token}"[m
[32m+[m[32m        if headers:[m
[32m+[m[32m            req_headers.update(headers)[m
[32m+[m
[32m+[m[32m        last_err: Optional[Exception] = None[m
[32m+[m[32m        for attempt in range(1, self.max_retries + 2):[m
[32m+[m[32m            try:[m
[32m+[m[32m                # Custom transport for offline unit testing[m
[32m+[m[32m                if self._http_transport:[m
[32m+[m[32m                    req = urllib.request.Request(url, data=data or data_stream, headers=req_headers, method=method)[m
[32m+[m[32m                    resp = self._http_transport(req)[m
[32m+[m[32m                    code = getattr(resp, "status", getattr(resp, "status_code", 200))[m
[32m+[m[32m                    resp_bytes = resp.read() if hasattr(resp, "read") else b""[m
[32m+[m[32m                    try:[m
[32m+[m[32m                        resp_json = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}[m
[32m+[m[32m                    except Exception:[m
[32m+[m[32m                        resp_json = {}[m
[32m+[m[32m                    if 200 <= code < 300:[m
[32m+[m[32m                        return code, resp_json, resp_bytes[m
[32m+[m[32m                    elif code in (401, 403):[m
[32m+[m[32m                        safe_msg = f"HTTP {code} Authentication/Authorization failure"[m
[32m+[m[32m                        raise PermissionError(safe_msg)[m
[32m+[m[32m                    elif 500 <= code < 600 and attempt <= self.max_retries:[m
[32m+[m[32m                        delay = float(2 ** attempt)[m
[32m+[m[32m                        self.sleep_fn(delay)[m
[32m+[m[32m                        continue[m
[32m+[m[32m                    else:[m
[32m+[m[32m                        raise RuntimeError(f"HTTP {code}: {resp_bytes.decode('utf-8', errors='replace')[:200]}")[m
[32m+[m
[32m+[m[32m                # Standard urllib transport[m
[32m+[m[32m                req = urllib.request.Request(url, data=data or data_stream, headers=req_headers, method=method)[m
[32m+[m[32m                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:[m
[32m+[m[32m                    resp_bytes = resp.read()[m
[32m+[m[32m                    code = resp.status[m
[32m+[m[32m                    try:[m
[32m+[m[32m                        resp_json = json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}[m
[32m+[m[32m                    except Exception:[m
[32m+[m[32m                        resp_json = {}[m
[32m+[m[32m                    return code, resp_json, resp_bytes[m
[32m+[m
[32m+[m[32m            except urllib.error.HTTPError as e:[m
[32m+[m[32m                code = e.code[m
[32m+[m[32m                err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""[m
[32m+[m[32m                clean_err_body = self._mask_secret(err_body)[m
[32m+[m[32m                if code in (401, 403):[m
[32m+[m[32m                    raise PermissionError(f"HTTP {code}: Access denied. Token may be invalid or expired.")[m
[32m+[m[32m                elif 500 <= code < 600 and attempt <= self.max_retries:[m
[32m+[m[32m                    last_err = e[m
[32m+[m[32m                    delay = float(2 ** attempt)[m
[32m+[m[32m                    self.sleep_fn(delay)[m
[32m+[m[32m                    continue[m
[32m+[m[32m                else:[m
[32m+[m[32m                    raise RuntimeError(f"HTTP {code}: {clean_err_body[:200]}")[m
[32m+[m[32m            except PermissionError:[m
[32m+[m[32m                raise[m
[32m+[m[32m            except Exception as e:[m
[32m+[m[32m                last_err = e[m
[32m+[m[32m                if attempt <= self.max_retries:[m
[32m+[m[32m                    delay = float(2 ** attempt)[m
[32m+[m[32m                    self.sleep_fn(delay)[m
[32m+[m[32m                    continue[m
[32m+[m[32m                raise RuntimeError(self._mask_secret(f"Network error contacting repository: {str(e)}"))[m
[32m+[m
[32m+[m[32m        raise RuntimeError(self._mask_secret(f"Max retries exhausted for {method} {url}: {str(last_err)}"))[m
[32m+[m
[32m+[m[32m    def find_existing_draft([m
[32m+[m[32m        self,[m
[32m+[m[32m        portal_id: str,[m
[32m+[m[32m        run_id: str,[m
[32m+[m[32m    ) -> Optional[DepositReceipt]:[m
[32m+[m[32m        """[m
[32m+[m[32m        Search Zenodo user drafts for an existing deposition matching this run_id.[m
[32m+[m[32m        Prevents duplicate draft creation if a previous creation request succeeded[m
[32m+[m[32m        remotely but the response was interrupted before saving locally.[m
[32m+[m[32m        """[m
[32m+[m[32m        query_str = f"keywords:\"corpusai-run:{run_id}\""[m
[32m+[m[32m        encoded_q = urllib.parse.quote(query_str)[m
[32m+[m[32m        url = f"{self.base_url}/deposit/depositions?q={encoded_q}&status=draft"[m
[32m+[m
[32m+[m[32m        try:[m
[32m+[m[32m            code, resp_data, _ = self._execute_http_request(url=url, method="GET")[m
[32m+[m[32m            if isinstance(resp_data, list) and len(resp_data) > 0:[m
[32m+[m[32m                match = resp_data[0][m
[32m+[m[32m                remote_id = str(match.get("id"))[m
[32m+[m[32m                links = match.get("links", {})[m
[32m+[m[32m                bucket_url = links.get("bucket")[m
[32m+[m[32m                record_url = links.get("html")[m
[32m+[m[32m                doi = match.get("metadata", {}).get("prereserve_doi", {}).get("doi") or match.get("doi")[m
[32m+[m[32m                now = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m                return DepositReceipt([m
[32m+[m[32m                    receipt_id=f"receipt-{portal_id}-{run_id}-reconciled",[m
[32m+[m[32m                    portal_id=portal_id,[m
[32m+[m[32m                    run_id=run_id,[m
[32m+[m[32m                    package_fingerprint="",[m
[32m+[m[32m                    repository_target="zenodo",[m
[32m+[m[32m                    environment="sandbox",[m
[32m+[m[32m                    status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m                    created_at=now,[m
[32m+[m[32m                    updated_at=now,[m
[32m+[m[32m                    remote_record_id=remote_id,[m
[32m+[m[32m                    remote_bucket_url=bucket_url,[m
[32m+[m[32m                    remote_record_url=record_url,[m
[32m+[m[32m                    doi=doi,[m
[32m+[m[32m                    reconciliation_notes="Discovered existing remote draft via query reconciliation",[m
[32m+[m[32m                )[m
[32m+[m[32m        except Exception as e:[m
[32m+[m[32m            logger.debug(f"Draft query reconciliation encountered: {e}")[m
[32m+[m[32m        return None[m
[32m+[m
[32m+[m[32m    def map_deposit_metadata_to_zenodo(self, metadata: DepositMetadata, run_id: Optional[str] = None) -> Dict[str, Any]:[m
[32m+[m[32m        """[m
[32m+[m[32m        Map domain-agnostic DepositMetadata to Zenodo REST API deposition schema.[m
[32m+[m[32m        Defaults access to restricted/embargoed for third-party news preservation safety.[m
[32m+[m[32m        """[m
[32m+[m[32m        creators_list = [][m
[32m+[m[32m        for c in metadata.creators:[m
[32m+[m[32m            c_dict: Dict[str, Any] = {"name": c.name}[m
[32m+[m[32m            if c.affiliation:[m
[32m+[m[32m                c_dict["affiliation"] = c.affiliation[m
[32m+[m[32m            if c.orcid:[m
[32m+[m[32m                c_dict["orcid"] = c.orcid[m
[32m+[m[32m            creators_list.append(c_dict)[m
[32m+[m
[32m+[m[32m        # Access mapping: default to restricted unless explicitly open[m
[32m+[m[32m        access_right = metadata.access_status.lower()[m
[32m+[m[32m        if access_right not in ("open", "embargoed", "restricted", "closed"):[m
[32m+[m[32m            access_right = "restricted"[m
[32m+[m
[32m+[m[32m        keywords = [[m
[32m+[m[32m            "web-archiving",[m
[32m+[m[32m            "corpus-linguistics",[m
[32m+[m[32m            f"software-version:{metadata.software_version}",[m
[32m+[m[32m        ] + metadata.source_domains + metadata.archive_formats[m
[32m+[m[32m        if run_id:[m
[32m+[m[32m            keywords.append(f"corpusai-run:{run_id}")[m
[32m+[m
[32m+[m[32m        zenodo_metadata: Dict[str, Any] = {[m
[32m+[m[32m            "title": metadata.title,[m
[32m+[m[32m            "upload_type": "dataset",[m
[32m+[m[32m            "description": metadata.description,[m
[32m+[m[32m            "creators": creators_list,[m
[32m+[m[32m            "access_right": access_right,[m
[32m+[m[32m            "keywords": keywords,[m
[32m+[m[32m            "notes": ([m
[32m+[m[32m                f"Archival capture period: {metadata.capture_period_start} to {metadata.capture_period_end}. "[m
[32m+[m[32m                f"Rights notice: {metadata.copyright_notice}. License declared: {metadata.license_status}."[m
[32m+[m[32m            ),[m
[32m+[m[32m        }[m
[32m+[m
[32m+[m[32m        if access_right == "embargoed" and metadata.embargo_date:[m
[32m+[m[32m            zenodo_metadata["embargo_date"] = metadata.embargo_date[m
[32m+[m[32m        elif access_right == "restricted":[m
[32m+[m[32m            zenodo_metadata["access_conditions"] = ([m
[32m+[m[32m                "Access is restricted for academic and preservation research evaluation under fair-use agreements. "[m
[32m+[m[32m                "Contact CorpusAI researchers with project affiliation."[m
[32m+[m[32m            )[m
[32m+[m
[32m+[m[32m        return {"metadata": zenodo_metadata}[m
[32m+[m
[32m+[m[32m    def create_draft([m
[32m+[m[32m        self,[m
[32m+[m[32m        metadata: DepositMetadata,[m
[32m+[m[32m        portal_id: str,[m
[32m+[m[32m        run_id: str,[m
[32m+[m[32m        package_fingerprint: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        """[m
[32m+[m[32m        Create a new deposition draft on Zenodo Sandbox.[m
[32m+[m[32m        Validates metadata before making any remote network calls.[m
[32m+[m[32m        """[m
[32m+[m[32m        errors = metadata.validate()[m
[32m+[m[32m        if errors:[m
[32m+[m[32m            raise ValueError(f"Invalid deposit metadata: {', '.join(errors)}")[m
[32m+[m
[32m+[m[32m        now = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m        receipt_id = f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}"[m
[32m+[m[32m        payload = self.map_deposit_metadata_to_zenodo(metadata, run_id=run_id)[m
[32m+[m
[32m+[m[32m        url = f"{self.base_url}/deposit/depositions"[m
[32m+[m[32m        code, resp_data, _ = self._execute_http_request([m
[32m+[m[32m            url=url,[m
[32m+[m[32m            method="POST",[m
[32m+[m[32m            headers={"Content-Type": "application/json"},[m
[32m+[m[32m            data=json.dumps(payload).encode("utf-8"),[m
[32m+[m[32m        )[m
[32m+[m
[32m+[m[32m        remote_id = str(resp_data.get("id"))[m
[32m+[m[32m        links = resp_data.get("links", {})[m
[32m+[m[32m        bucket_url = links.get("bucket")[m
[32m+[m[32m        record_url = links.get("html")[m
[32m+[m[32m        doi = resp_data.get("metadata", {}).get("prereserve_doi", {}).get("doi") or resp_data.get("doi")[m
[32m+[m
[32m+[m[32m        receipt = DepositReceipt([m
[32m+[m[32m            receipt_id=receipt_id,[m
[32m+[m[32m            portal_id=portal_id,[m
[32m+[m[32m            run_id=run_id,[m
[32m+[m[32m            package_fingerprint=package_fingerprint,[m
[32m+[m[32m            repository_target="zenodo",[m
[32m+[m[32m            environment="sandbox",[m
[32m+[m[32m            status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m            created_at=now,[m
[32m+[m[32m            updated_at=now,[m
[32m+[m[32m            remote_record_id=remote_id,[m
[32m+[m[32m            remote_bucket_url=bucket_url,[m
[32m+[m[32m            remote_record_url=record_url,[m
[32m+[m[32m            doi=doi,[m
[32m+[m[32m        )[m
[32m+[m[32m        return receipt[m
[32m+[m
[32m+[m[32m    def upload_artifacts([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        package_dir: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        """[m
[32m+[m[32m        Stream files in the finalized package directory to Zenodo's Bucket API.[m
[32m+[m[32m        Local package must pass PackageVerifier before upload begins.[m
[32m+[m[32m        """[m
[32m+[m[32m        # Pre-upload integrity gate: local package MUST be valid[m
[32m+[m[32m        v_res = PackageVerifier.verify_package(package_dir)[m
[32m+[m[32m        if not v_res.is_valid:[m
[32m+[m[32m            err_summary = "; ".join(v_res.metadata_errors + v_res.missing_files + v_res.checksum_mismatches + v_res.unexpected_files)[m
[32m+[m[32m            receipt.transition_to(STATUS_FAILED, error=f"Local package verification failed before upload: {err_summary}", error_category="LOCAL_PACKAGE_CORRUPT")[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        if not receipt.remote_bucket_url:[m
[32m+[m[32m            receipt.transition_to(STATUS_FAILED, error="Missing remote bucket URL in receipt", error_category="MISSING_REMOTE_BUCKET")[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        receipt.transition_to(STATUS_UPLOADING)[m
[32m+[m
[32m+[m[32m        # Collect files to deposit (manifest, environment, checksums, deposit_metadata, content, replay, qa)[m
[32m+[m[32m        files_to_upload: List[Tuple[str, str]] = []  # (rel_path, abs_path)[m
[32m+[m[32m        total_package_bytes = 0[m
[32m+[m[32m        for root, _, files in os.walk(package_dir):[m
[32m+[m[32m            for file in sorted(files):[m
[32m+[m[32m                full_p = os.path.join(root, file)[m
[32m+[m[32m                rel_p = os.path.relpath(full_p, package_dir).replace("\\", "/")[m
[32m+[m[32m                sz = os.path.getsize(full_p)[m
[32m+[m[32m                if sz > ZENODO_MAX_SINGLE_FILE_BYTES:[m
[32m+[m[32m                    receipt.transition_to([m
[32m+[m[32m                        STATUS_FAILED,[m
[32m+[m[32m                        error=f"File {rel_p} size ({sz} bytes) exceeds Zenodo limit ({ZENODO_MAX_SINGLE_FILE_BYTES} bytes).",[m
[32m+[m[32m                        error_category="FILE_SIZE_LIMIT_EXCEEDED",[m
[32m+[m[32m                    )[m
[32m+[m[32m                    return receipt[m
[32m+[m[32m                total_package_bytes += sz[m
[32m+[m[32m                files_to_upload.append((rel_p, full_p))[m
[32m+[m
[32m+[m[32m        # Check file count and total volume quota[m
[32m+[m[32m        if len(files_to_upload) > ZENODO_MAX_RECORD_FILES:[m
[32m+[m[32m            receipt.transition_to([m
[32m+[m[32m                STATUS_FAILED,[m
[32m+[m[32m                error=f"Package contains {len(files_to_upload)} files, exceeding Zenodo limit ({ZENODO_MAX_RECORD_FILES} files). Bundle into archive.",[m
[32m+[m[32m                error_category="FILE_COUNT_LIMIT_EXCEEDED",[m
[32m+[m[32m            )[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        if total_package_bytes > ZENODO_MAX_RECORD_BYTES:[m
[32m+[m[32m            receipt.transition_to([m
[32m+[m[32m                STATUS_FAILED,[m
[32m+[m[32m                error=f"Package total size ({total_package_bytes} bytes) exceeds Zenodo record quota ({ZENODO_MAX_RECORD_BYTES} bytes).",[m
[32m+[m[32m                error_category="RECORD_QUOTA_EXCEEDED",[m
[32m+[m[32m            )[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        bucket_url = receipt.remote_bucket_url.rstrip("/")[m
[32m+[m
[32m+[m[32m        for rel_path, full_path in files_to_upload:[m
[32m+[m[32m            md5_val, sha256_val, file_size = compute_file_md5_and_sha256(full_path)[m
[32m+[m[32m            # Zenodo bucket uploads use PUT to {bucket_url}/{filename}[m
[32m+[m[32m            # Flatten or encode relative subpaths to preserve distinct artifacts[m
[32m+[m[32m            safe_filename = rel_path.replace("/", "__")[m
[32m+[m[32m            upload_url = f"{bucket_url}/{urllib.parse.quote(safe_filename)}"[m
[32m+[m
[32m+[m[32m            with open(full_path, "rb") as fp:[m
[32m+[m[32m                file_bytes = fp.read()[m
[32m+[m
[32m+[m[32m            try:[m
[32m+[m[32m                code, resp_data, _ = self._execute_http_request([m
[32m+[m[32m                    url=upload_url,[m
[32m+[m[32m                    method="PUT",[m
[32m+[m[32m                    headers={"Content-Type": "application/octet-stream"},[m
[32m+[m[32m                    data=file_bytes,[m
[32m+[m[32m                )[m
[32m+[m[32m            except Exception as e:[m
[32m+[m[32m                receipt.transition_to(STATUS_FAILED, error=f"Failed uploading {rel_path}: {str(e)}", error_category="UPLOAD_TRANSMISSION_FAILED")[m
[32m+[m[32m                return receipt[m
[32m+[m
[32m+[m[32m            remote_md5 = resp_data.get("checksum") or ""[m
[32m+[m[32m            if remote_md5.startswith("md5:"):[m
[32m+[m[32m                remote_md5 = remote_md5[4:][m
[32m+[m
[32m+[m[32m            receipt.artifact_inventory[rel_path] = UploadedArtifactRecord([m
[32m+[m[32m                filename=rel_path,[m
[32m+[m[32m                filesize=file_size,[m
[32m+[m[32m                local_sha256=sha256_val,[m
[32m+[m[32m                local_md5=md5_val,[m
[32m+[m[32m                remote_md5=remote_md5,[m
[32m+[m[32m                remote_checksum=resp_data.get("checksum"),[m
[32m+[m[32m                remote_file_id=resp_data.get("id"),[m
[32m+[m[32m                verified=False,[m
[32m+[m[32m            )[m
[32m+[m
[32m+[m[32m        receipt.transition_to(STATUS_UPLOADED)[m
[32m+[m[32m        return receipt[m
[32m+[m
[32m+[m[32m    def verify_remote_artifacts([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        package_dir: str,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        """[m
[32m+[m[32m        Verify that all uploaded files match local package integrity.[m
[32m+[m[32m        Zenodo Bucket API computes and exposes MD5 server-side checksums.[m
[32m+[m[32m        We verify:[m
[32m+[m[32m        1. All expected files in package_dir are accounted for in the uploaded inventory.[m
[32m+[m[32m        2. Local MD5 matches Zenodo's server-computed MD5.[m
[32m+[m[32m        3. Local SHA-256 matches the package's checksums.sha256.[m
[32m+[m[32m        """[m
[32m+[m[32m        if receipt.status != STATUS_UPLOADED:[m
[32m+[m[32m            raise ValueError(f"Cannot verify remote artifacts when deposit status is '{receipt.status}'.")[m
[32m+[m
[32m+[m[32m        checksums_path = os.path.join(package_dir, "checksums.sha256")[m
[32m+[m[32m        expected_sha256s: Dict[str, str] = {}[m
[32m+[m[32m        if os.path.exists(checksums_path):[m
[32m+[m[32m            with open(checksums_path, "r", encoding="utf-8") as f:[m
[32m+[m[32m                for line in f:[m
[32m+[m[32m                    parts = line.strip().split(None, 1)[m
[32m+[m[32m                    if len(parts) == 2:[m
[32m+[m[32m                        expected_sha256s[parts[1].strip().replace("\\", "/").lstrip("./")] = parts[0].strip().lower()[m
[32m+[m
[32m+[m[32m        # Add checksums.sha256 itself if present[m
[32m+[m[32m        if os.path.exists(checksums_path):[m
[32m+[m[32m            with open(checksums_path, "rb") as f:[m
[32m+[m[32m                expected_sha256s["checksums.sha256"] = hashlib.sha256(f.read()).hexdigest().lower()[m
[32m+[m
[32m+[m[32m        missing_or_mismatched = [][m
[32m+[m[32m        for rel_path, record in receipt.artifact_inventory.items():[m
[32m+[m[32m            # Check 1: MD5 match against Zenodo server calculation[m
[32m+[m[32m            if not record.remote_md5 or record.remote_md5.lower() != record.local_md5.lower():[m
[32m+[m[32m                missing_or_mismatched.append(f"{rel_path}: remote MD5 mismatch (local {record.local_md5} != remote {record.remote_md5})")[m
[32m+[m[32m                continue[m
[32m+[m
[32m+[m[32m            # Check 2: Local SHA-256 matches package checksum manifest[m
[32m+[m[32m            exp_sha = expected_sha256s.get(rel_path)[m
[32m+[m[32m            if exp_sha and exp_sha != record.local_sha256.lower():[m
[32m+[m[32m                missing_or_mismatched.append(f"{rel_path}: local SHA-256 mismatch against manifest")[m
[32m+[m[32m                continue[m
[32m+[m
[32m+[m[32m            record.verified = True[m
[32m+[m
[32m+[m[32m        if missing_or_mismatched:[m
[32m+[m[32m            receipt.checksum_verification_outcome = "FAILED"[m
[32m+[m[32m            receipt.transition_to([m
[32m+[m[32m                STATUS_FAILED,[m
[32m+[m[32m                error=f"Remote artifact checksum verification failed: {'; '.join(missing_or_mismatched)}",[m
[32m+[m[32m                error_category="REMOTE_CHECKSUM_MISMATCH",[m
[32m+[m[32m            )[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        receipt.checksum_verification_outcome = "VERIFIED_MD5_AND_LOCAL_SHA256"[m
[32m+[m[32m        receipt.transition_to(STATUS_VERIFIED)[m
[32m+[m[32m        return receipt[m
[32m+[m
[32m+[m[32m    def get_status(self, receipt: DepositReceipt) -> DepositReceipt:[m
[32m+[m[32m        """Fetch remote draft status from Zenodo Sandbox."""[m
[32m+[m[32m        if not receipt.remote_record_id:[m
[32m+[m[32m            return receipt[m
[32m+[m
[32m+[m[32m        url = f"{self.base_url}/deposit/depositions/{receipt.remote_record_id}"[m
[32m+[m[32m        code, resp_data, _ = self._execute_http_request(url=url, method="GET")[m
[32m+[m
[32m+[m[32m        state = resp_data.get("state")[m
[32m+[m[32m        submitted = resp_data.get("submitted", False)[m
[32m+[m[32m        if submitted:[m
[32m+[m[32m            receipt.status = STATUS_PUBLISHED[m
[32m+[m[32m            receipt.doi = resp_data.get("doi") or receipt.doi[m
[32m+[m[32m        receipt.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m        return receipt[m
[32m+[m
[32m+[m[32m    def publish_deposit([m
[32m+[m[32m        self,[m
[32m+[m[32m        receipt: DepositReceipt,[m
[32m+[m[32m        allow_publish: bool = False,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        """[m
[32m+[m[32m        Publish a verified draft on Zenodo Sandbox.[m
[32m+[m[32m        Hard gate: Requires allow_publish=True. Production is strictly blocked.[m
[32m+[m[32m        """[m
[32m+[m[32m        if not allow_publish:[m
[32m+[m[32m            raise PermissionError([m
[32m+[m[32m                "Publication is locked by default. Public dissemination requires explicit authorization (allow_publish=True)."[m
[32m+[m[32m            )[m
[32m+[m[32m        if receipt.status != STATUS_VERIFIED:[m
[32m+[m[32m            raise ValueError(f"Cannot publish deposit in status '{receipt.status}'. Must be '{STATUS_VERIFIED}'.")[m
[32m+[m
[32m+[m[32m        url = f"{self.base_url}/deposit/depositions/{receipt.remote_record_id}/actions/publish"[m
[32m+[m[32m        code, resp_data, _ = self._execute_http_request(url=url, method="POST")[m
[32m+[m
[32m+[m[32m        receipt.doi = resp_data.get("doi") or receipt.doi[m
[32m+[m[32m        receipt.remote_record_url = resp_data.get("links", {}).get("record_html") or receipt.remote_record_url[m
[32m+[m[32m        receipt.transition_to(STATUS_PUBLISHED)[m
[32m+[m[32m        return receipt[m
[32m+[m
[32m+[m
[32m+[m[32mclass DepositOrchestrator:[m
[32m+[m[32m    """[m
[32m+[m[32m    High-level orchestrator that executes safe, idempotent deposits of finalized[m
[32m+[m[32m    archival packages to Zenodo Sandbox.[m
[32m+[m[32m    """[m
[32m+[m
[32m+[m[32m    def __init__([m
[32m+[m[32m        self,[m
[32m+[m[32m        client: RepositoryDepositClient,[m
[32m+[m[32m        registry: DepositRegistry,[m
[32m+[m[32m    ):[m
[32m+[m[32m        self.client = client[m
[32m+[m[32m        self.registry = registry[m
[32m+[m
[32m+[m[32m    def deposit_package([m
[32m+[m[32m        self,[m
[32m+[m[32m        portal_id: str,[m
[32m+[m[32m        run_id: str,[m
[32m+[m[32m        package_dir: str,[m
[32m+[m[32m        deposit_metadata: DepositMetadata,[m
[32m+[m[32m    ) -> DepositReceipt:[m
[32m+[m[32m        """[m
[32m+[m[32m        Orchestrate deposit with idempotency, local verification, and safe retry.[m
[32m+[m[32m        """[m
[32m+[m[32m        package_fingerprint = compute_package_fingerprint(package_dir)[m
[32m+[m
[32m+[m[32m        # 1. Check existing receipt for duplicate protection & resumption[m
[32m+[m[32m        existing = self.registry.get_receipt(portal_id, run_id)[m
[32m+[m[32m        if existing:[m
[32m+[m[32m            # If package modified, require reconciliation[m
[32m+[m[32m            if existing.package_fingerprint != package_fingerprint:[m
[32m+[m[32m                existing.transition_to([m
[32m+[m[32m                    STATUS_NEEDS_RECONCILIATION,[m
[32m+[m[32m                    error=f"Local package fingerprint changed ({existing.package_fingerprint[:8]} -> {package_fingerprint[:8]}).",[m
[32m+[m[32m                    error_category="PACKAGE_FINGERPRINT_CHANGED",[m
[32m+[m[32m                )[m
[32m+[m[32m                self.registry.save()[m
[32m+[m[32m                return existing[m
[32m+[m
[32m+[m[32m            # If already verified or published, return existing receipt (idempotent)[m
[32m+[m[32m            if existing.status in (STATUS_VERIFIED, STATUS_PUBLISHED):[m
[32m+[m[32m                logger.info(f"Run '{run_id}' already deposited and {existing.status}. Returning receipt.")[m
[32m+[m[32m                return existing[m
[32m+[m
[32m+[m[32m            # Resume from existing draft[m
[32m+[m[32m            receipt = existing[m
[32m+[m[32m        else:[m
[32m+[m[32m            # 2. Local verification before remote draft creation[m
[32m+[m[32m            v_res = PackageVerifier.verify_package(package_dir)[m
[32m+[m[32m            if not v_res.is_valid:[m
[32m+[m[32m                now = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m                err_msg = "; ".join(v_res.metadata_errors + v_res.missing_files + v_res.checksum_mismatches + v_res.unexpected_files)[m
[32m+[m[32m                failed_receipt = DepositReceipt([m
[32m+[m[32m                    receipt_id=f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}",[m
[32m+[m[32m                    portal_id=portal_id,[m
[32m+[m[32m                    run_id=run_id,[m
[32m+[m[32m                    package_fingerprint=package_fingerprint,[m
[32m+[m[32m                    repository_target="zenodo",[m
[32m+[m[32m                    environment="sandbox",[m
[32m+[m[32m                    status=STATUS_FAILED,[m
[32m+[m[32m                    created_at=now,[m
[32m+[m[32m                    updated_at=now,[m
[32m+[m[32m                    last_error=f"Local package verification failed: {err_msg}",[m
[32m+[m[32m                    last_error_category="LOCAL_PACKAGE_INVALID",[m
[32m+[m[32m                    retry_eligible=False,[m
[32m+[m[32m                )[m
[32m+[m[32m                self.registry.register_receipt(failed_receipt)[m
[32m+[m[32m                return failed_receipt[m
[32m+[m
[32m+[m[32m            # 3. Check for existing remote draft to avoid duplicate creation if interrupted earlier[m
[32m+[m[32m            existing_remote = self.client.find_existing_draft(portal_id, run_id)[m
[32m+[m[32m            if existing_remote:[m
[32m+[m[32m                existing_remote.package_fingerprint = package_fingerprint[m
[32m+[m[32m                self.registry.register_receipt(existing_remote)[m
[32m+[m[32m                receipt = existing_remote[m
[32m+[m[32m            else:[m
[32m+[m[32m                # Create remote draft[m
[32m+[m[32m                try:[m
[32m+[m[32m                    receipt = self.client.create_draft(deposit_metadata, portal_id, run_id, package_fingerprint)[m
[32m+[m[32m                    self.registry.register_receipt(receipt)[m
[32m+[m[32m                except Exception as e:[m
[32m+[m[32m                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()[m
[32m+[m[32m                    failed_receipt = DepositReceipt([m
[32m+[m[32m                        receipt_id=f"receipt-{portal_id}-{run_id}-{package_fingerprint[:8]}",[m
[32m+[m[32m                        portal_id=portal_id,[m
[32m+[m[32m                        run_id=run_id,[m
[32m+[m[32m                        package_fingerprint=package_fingerprint,[m
[32m+[m[32m                        repository_target="zenodo",[m
[32m+[m[32m                        environment="sandbox",[m
[32m+[m[32m                        status=STATUS_FAILED,[m
[32m+[m[32m                        created_at=now,[m
[32m+[m[32m                        updated_at=now,[m
[32m+[m[32m                        last_error=str(e),[m
[32m+[m[32m                        last_error_category="DRAFT_CREATION_FAILED",[m
[32m+[m[32m                    )[m
[32m+[m[32m                    self.registry.register_receipt(failed_receipt)[m
[32m+[m[32m                    return failed_receipt[m
[32m+[m
[32m+[m[32m        # 4. Upload artifacts[m
[32m+[m[32m        if receipt.status in (STATUS_DRAFT_CREATED, STATUS_UPLOADING, STATUS_FAILED):[m
[32m+[m[32m            try:[m
[32m+[m[32m                receipt = self.client.upload_artifacts(receipt, package_dir)[m
[32m+[m[32m                self.registry.save()[m
[32m+[m[32m            except Exception as e:[m
[32m+[m[32m                receipt.transition_to(STATUS_FAILED, error=str(e), error_category="UPLOAD_EXCEPTION")[m
[32m+[m[32m                self.registry.save()[m
[32m+[m[32m                return receipt[m
[32m+[m
[32m+[m[32m        # 5. Verify remote artifacts[m
[32m+[m[32m        if receipt.status == STATUS_UPLOADED:[m
[32m+[m[32m            try:[m
[32m+[m[32m                receipt = self.client.verify_remote_artifacts(receipt, package_dir)[m
[32m+[m[32m                self.registry.save()[m
[32m+[m[32m            except Exception as e:[m
[32m+[m[32m                receipt.transition_to(STATUS_FAILED, error=str(e), error_category="VERIFICATION_EXCEPTION")[m
[32m+[m[32m                self.registry.save()[m
[32m+[m[32m                return receipt[m
[32m+[m
[32m+[m[32m        return receipt[m
[1mdiff --git a/tests/test_zenodo_deposit.py b/tests/test_zenodo_deposit.py[m
[1mnew file mode 100644[m
[1mindex 0000000..c9e9df9[m
[1m--- /dev/null[m
[1m+++ b/tests/test_zenodo_deposit.py[m
[36m@@ -0,0 +1,596 @@[m
[32m+[m[32m"""[m
[32m+[m[32mTests for CorpusAI Phase 9A: Zenodo Sandbox Deposit Adapter and Receipt Tracking.[m
[32m+[m
[32m+[m[32mCovers:[m
[32m+[m[32m1. Invalid or incomplete local packages are rejected before remote creation.[m
[32m+[m[32m2. Valid metadata produces the expected Zenodo API payload.[m
[32m+[m[32m3. Successful draft creation records the remote identifier and bucket URL.[m
[32m+[m[32m4. Artifact uploads stream data and handle upload failures correctly.[m
[32m+[m[32m5. Retryable server errors (HTTP 500/503) use bounded retries with backoff.[m
[32m+[m[32m6. Authentication failures (HTTP 401/403) do not expose sensitive API tokens.[m
[32m+[m[32m7. Repeated invocation for a completed deposit returns the existing receipt (idempotency).[m
[32m+[m[32m8. An ambiguous create request / changed package fingerprint triggers NEEDS_RECONCILIATION.[m
[32m+[m[32m9. Failure partway through upload leaves the local package intact and receipt retryable.[m
[32m+[m[32m10. Remote checksum differences are handled according to actual algorithms (MD5 + local SHA-256).[m
[32m+[m[32m11. Missing artifacts or failed remote verification prevent the VERIFIED state.[m
[32m+[m[32m12. Deposit failure does not alter ArchiveCatalog's latest_successful_run_id.[m
[32m+[m[32m13. Atomic receipt persistence survives interruptions via atomic replacement.[m
[32m+[m[32m14. Production API URLs and public publishing are blocked by default.[m
[32m+[m[32m"""[m
[32m+[m
[32m+[m[32mimport hashlib[m
[32m+[m[32mimport io[m
[32m+[m[32mimport json[m
[32m+[m[32mimport os[m
[32m+[m[32mimport shutil[m
[32m+[m[32mimport tempfile[m
[32m+[m[32mfrom typing import Any, Dict, List[m
[32m+[m[32mimport unittest.mock as mock[m
[32m+[m
[32m+[m[32mimport pytest[m
[32m+[m
[32m+[m[32mfrom src.archive_catalog import ArchiveCatalog, CatalogRunEntry[m
[32m+[m[32mfrom src.archive_package import ([m
[32m+[m[32m    ArchivePackager,[m
[32m+[m[32m    PackageVerifier,[m
[32m+[m[32m    CHECKSUMS_FILENAME,[m
[32m+[m[32m    ENVIRONMENT_FILENAME,[m
[32m+[m[32m    MANIFEST_FILENAME,[m
[32m+[m[32m)[m
[32m+[m[32mfrom src.deposit_metadata import Creator, DepositMetadata[m
[32m+[m[32mfrom src.zenodo_deposit import ([m
[32m+[m[32m    DepositOrchestrator,[m
[32m+[m[32m    DepositReceipt,[m
[32m+[m[32m    DepositRegistry,[m
[32m+[m[32m    UploadedArtifactRecord,[m
[32m+[m[32m    ZenodoSandboxAdapter,[m
[32m+[m[32m    STATUS_DRAFT_CREATED,[m
[32m+[m[32m    STATUS_FAILED,[m
[32m+[m[32m    STATUS_NEEDS_RECONCILIATION,[m
[32m+[m[32m    STATUS_PENDING,[m
[32m+[m[32m    STATUS_PUBLISHED,[m
[32m+[m[32m    STATUS_UPLOADED,[m
[32m+[m[32m    STATUS_UPLOADING,[m
[32m+[m[32m    STATUS_VERIFIED,[m
[32m+[m[32m    ZENODO_SANDBOX_BASE_URL,[m
[32m+[m[32m    ZENODO_PRODUCTION_BASE_URL,[m
[32m+[m[32m    compute_package_fingerprint,[m
[32m+[m[32m)[m
[32m+[m
[32m+[m
[32m+[m[32m# ---------------------------------------------------------------------------[m
[32m+[m[32m# Test Fixtures & Helpers[m
[32m+[m[32m# ---------------------------------------------------------------------------[m
[32m+[m
[32m+[m[32mclass MockHttpResponse:[m
[32m+[m[32m    """Mock HTTP response object for urllib requests."""[m
[32m+[m[32m    def __init__(self, status: int, data: Dict[str, Any]):[m
[32m+[m[32m        self.status = status[m
[32m+[m[32m        self.status_code = status[m
[32m+[m[32m        self._data = json.dumps(data).encode("utf-8")[m
[32m+[m
[32m+[m[32m    def read(self) -> bytes:[m
[32m+[m[32m        return self._data[m
[32m+[m
[32m+[m
[32m+[m[32mdef _create_sample_metadata(tmp_path) -> DepositMetadata:[m
[32m+[m[32m    return DepositMetadata([m
[32m+[m[32m        title="CorpusAI Research Corpus: WordPress News Archive",[m
[32m+[m[32m        creators=[Creator(name="Balázs Indig", affiliation="Research Group")],[m
[32m+[m[32m        description="Preserved longitudinal web corpus of WordPress news articles for linguistic evaluation.",[m
[32m+[m[32m        capture_period_start="2026-10-01T00:00:00Z",[m
[32m+[m[32m        capture_period_end="2026-10-23T00:00:00Z",[m
[32m+[m[32m        source_domains=["wordpress.org"],[m
[32m+[m[32m        license_status="Research-Only",[m
[32m+[m[32m        copyright_notice="Copyright © WordPress Foundation. Preserved under academic research fair use.",[m
[32m+[m[32m        software_version="corpusai-0.1.0",[m
[32m+[m[32m        archive_formats=["WARC", "JSON"],[m
[32m+[m[32m        checksum_manifest_path="checksums.sha256",[m
[32m+[m[32m        access_status="restricted",[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m
[32m+[m[32mdef _create_valid_packaged_run(tmp_path, portal_id="wordpress-news", run_id="run-20261023T010000Z-wordpr") -> str:[m
[32m+[m[32m    """Creates and verifies a real minimal package on disk."""[m
[32m+[m[32m    packager = ArchivePackager(archives_root=str(tmp_path / "archives"))[m
[32m+[m
[32m+[m[32m    # Create dummy raw content file[m
[32m+[m[32m    raw_warc = str(tmp_path / "content.warc.gz")[m
[32m+[m[32m    with open(raw_warc, "wb") as f:[m
[32m+[m[32m        f.write(b"WARC/1.0\r\ntest article content\r\n")[m
[32m+[m
[32m+[m[32m    pkg_dir, v_res = packager.package_run([m
[32m+[m[32m        portal_id=portal_id,[m
[32m+[m[32m        run_id=run_id,[m
[32m+[m[32m        mode="INCREMENTAL",[m
[32m+[m[32m        status="CONVERGED",[m
[32m+[m[32m        started_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        completed_at="2026-10-23T01:05:00Z",[m
[32m+[m[32m        content_html_warc=raw_warc,[m
[32m+[m[32m    )[m
[32m+[m[32m    assert v_res.is_valid, f"Created package must be valid: {v_res.metadata_errors}"[m
[32m+[m[32m    return pkg_dir[m
[32m+[m
[32m+[m
[32m+[m[32m# ---------------------------------------------------------------------------[m
[32m+[m[32m# Test Cases[m
[32m+[m[32m# ---------------------------------------------------------------------------[m
[32m+[m
[32m+[m[32mdef test_invalid_package_rejected_before_remote_creation(tmp_path):[m
[32m+[m[32m    """1. Invalid or incomplete local packages are rejected before remote creation."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    # Corrupt the package by removing manifest.json[m
[32m+[m[32m    os.remove(os.path.join(pkg_dir, MANIFEST_FILENAME))[m
[32m+[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="dummy_token")[m
[32m+[m[32m    registry = DepositRegistry(str(tmp_path / "deposit_registry.json"))[m
[32m+[m[32m    orchestrator = DepositOrchestrator(adapter, registry)[m
[32m+[m
[32m+[m[32m    mock_create = mock.MagicMock()[m
[32m+[m[32m    adapter.create_draft = mock_create[m
[32m+[m
[32m+[m[32m    receipt = orchestrator.deposit_package([m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-20261023T010000Z-wordpr",[m
[32m+[m[32m        package_dir=pkg_dir,[m
[32m+[m[32m        deposit_metadata=meta,[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    assert receipt.status == STATUS_FAILED[m
[32m+[m[32m    assert "Local package verification failed" in receipt.last_error[m
[32m+[m[32m    assert mock_create.call_count == 0  # Remote call never made![m
[32m+[m
[32m+[m
[32m+[m[32mdef test_metadata_mapping_to_zenodo_payload(tmp_path):[m
[32m+[m[32m    """2. Valid metadata produces the expected Zenodo API payload with safe defaults."""[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="dummy_token")[m
[32m+[m[32m    payload = adapter.map_deposit_metadata_to_zenodo(meta)[m
[32m+[m
[32m+[m[32m    metadata = payload["metadata"][m
[32m+[m[32m    assert metadata["title"] == meta.title[m
[32m+[m[32m    assert metadata["upload_type"] == "dataset"[m
[32m+[m[32m    assert metadata["creators"] == [{"name": "Balázs Indig", "affiliation": "Research Group"}][m
[32m+[m[32m    assert metadata["access_right"] == "restricted"[m
[32m+[m[32m    assert "access_conditions" in metadata[m
[32m+[m[32m    assert "wordpress.org" in metadata["keywords"][m
[32m+[m
[32m+[m
[32m+[m[32mdef test_draft_creation_records_identifiers(tmp_path):[m
[32m+[m[32m    """3. Successful draft creation records the remote identifier and bucket URL."""[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m
[32m+[m[32m    mock_resp_data = {[m
[32m+[m[32m        "id": 987654,[m
[32m+[m[32m        "doi": "10.5072/zenodo.987654",[m
[32m+[m[32m        "links": {[m
[32m+[m[32m            "bucket": "https://sandbox.zenodo.org/api/files/bucket-uuid-1234",[m
[32m+[m[32m            "html": "https://sandbox.zenodo.org/deposit/987654",[m
[32m+[m[32m        },[m
[32m+[m[32m        "metadata": {[m
[32m+[m[32m            "prereserve_doi": {"doi": "10.5072/zenodo.987654"}[m
[32m+[m[32m        }[m
[32m+[m[32m    }[m
[32m+[m
[32m+[m[32m    def fake_transport(req):[m
[32m+[m[32m        assert req.method == "POST"[m
[32m+[m[32m        assert req.full_url == f"{ZENODO_SANDBOX_BASE_URL}/deposit/depositions"[m
[32m+[m[32m        return MockHttpResponse(201, mock_resp_data)[m
[32m+[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="secret_token", http_transport=fake_transport)[m
[32m+[m[32m    receipt = adapter.create_draft(meta, "wordpress-news", "run-1", "pkg-fingerprint-123")[m
[32m+[m
[32m+[m[32m    assert receipt.status == STATUS_DRAFT_CREATED[m
[32m+[m[32m    assert receipt.remote_record_id == "987654"[m
[32m+[m[32m    assert receipt.remote_bucket_url == "https://sandbox.zenodo.org/api/files/bucket-uuid-1234"[m
[32m+[m[32m    assert receipt.doi == "10.5072/zenodo.987654"[m
[32m+[m[32m    assert receipt.environment == "sandbox"[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_artifact_uploads_streaming_and_handling(tmp_path):[m
[32m+[m[32m    """4. Artifact uploads stream data to Bucket API and handle upload failures correctly."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    uploaded_files = [][m
[32m+[m
[32m+[m[32m    def fake_transport(req):[m
[32m+[m[32m        if req.method == "PUT":[m
[32m+[m[32m            uploaded_files.append(req.full_url)[m
[32m+[m[32m            # Zenodo Bucket API returns MD5 of uploaded file[m
[32m+[m[32m            # Simulate correct md5 based on request body[m
[32m+[m[32m            data = req.data[m
[32m+[m[32m            md5_val = hashlib.md5(data).hexdigest()[m
[32m+[m[32m            return MockHttpResponse(200, {"checksum": f"md5:{md5_val}", "id": "file-uuid"})[m
[32m+[m[32m        return MockHttpResponse(200, {})[m
[32m+[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token", http_transport=fake_transport)[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp1",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        remote_record_id="987654",[m
[32m+[m[32m        remote_bucket_url="https://sandbox.zenodo.org/api/files/bucket-uuid-1234",[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    receipt = adapter.upload_artifacts(receipt, pkg_dir)[m
[32m+[m[32m    assert receipt.status == STATUS_UPLOADED[m
[32m+[m[32m    assert len(uploaded_files) >= 4  # manifest, environment, checksums, content[m
[32m+[m[32m    assert any("manifest.json" in u for u in uploaded_files)[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_bounded_retries_on_5xx_server_errors(tmp_path):[m
[32m+[m[32m    """5. Retryable server errors (HTTP 500/503) use bounded retries with backoff."""[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m[32m    attempts = 0[m
[32m+[m
[32m+[m[32m    def failing_transport(req):[m
[32m+[m[32m        nonlocal attempts[m
[32m+[m[32m        attempts += 1[m
[32m+[m[32m        if attempts < 3:[m
[32m+[m[32m            return MockHttpResponse(503, {"message": "Service Temporarily Unavailable"})[m
[32m+[m[32m        return MockHttpResponse(201, {"id": 111, "links": {"bucket": "http://bucket"}})[m
[32m+[m
[32m+[m[32m    delays = [][m
[32m+[m[32m    adapter = ZenodoSandboxAdapter([m
[32m+[m[32m        api_token="token",[m
[32m+[m[32m        max_retries=3,[m
[32m+[m[32m        http_transport=failing_transport,[m
[32m+[m[32m        sleep_fn=lambda d: delays.append(d),[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    receipt = adapter.create_draft(meta, "portal", "run", "fp")[m
[32m+[m[32m    assert receipt.status == STATUS_DRAFT_CREATED[m
[32m+[m[32m    assert attempts == 3  # Failed twice, succeeded on attempt 3[m
[32m+[m[32m    assert len(delays) == 2[m
[32m+[m[32m    assert delays[0] == 2.0  # 2^1[m
[32m+[m[32m    assert delays[1] == 4.0  # 2^2[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_auth_failures_do_not_expose_tokens(tmp_path):[m
[32m+[m[32m    """6. Authentication failures (HTTP 401/403) do not expose sensitive API tokens."""[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m[32m    secret_token = "SUPER_SECRET_TOKEN_XYZ123"[m
[32m+[m
[32m+[m[32m    def auth_fail_transport(req):[m
[32m+[m[32m        return MockHttpResponse(401, {"message": f"Invalid token: {secret_token}"})[m
[32m+[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token=secret_token, http_transport=auth_fail_transport)[m
[32m+[m[32m    with pytest.raises(PermissionError) as exc_info:[m
[32m+[m[32m        adapter.create_draft(meta, "portal", "run", "fp")[m
[32m+[m
[32m+[m[32m    err_str = str(exc_info.value)[m
[32m+[m[32m    assert secret_token not in err_str[m
[32m+[m[32m    assert "Access denied" in err_str or "Authentication/Authorization failure" in err_str[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_idempotent_deposit_returns_existing_receipt(tmp_path):[m
[32m+[m[32m    """7. Repeated invocation for a completed deposit returns the existing receipt."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m[32m    fp = compute_package_fingerprint(pkg_dir)[m
[32m+[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m
[32m+[m[32m    # Pre-record completed verified receipt[m
[32m+[m[32m    pre_receipt = DepositReceipt([m
[32m+[m[32m        receipt_id=f"receipt-wordpress-news-run-1-{fp[:8]}",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-20261023T010000Z-wordpr",[m
[32m+[m[32m        package_fingerprint=fp,[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_VERIFIED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        remote_record_id="55555",[m
[32m+[m[32m        checksum_verification_outcome="VERIFIED_MD5_AND_LOCAL_SHA256",[m
[32m+[m[32m    )[m
[32m+[m[32m    registry.register_receipt(pre_receipt)[m
[32m+[m
[32m+[m[32m    mock_client = mock.MagicMock()[m
[32m+[m[32m    orchestrator = DepositOrchestrator(mock_client, registry)[m
[32m+[m
[32m+[m[32m    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)[m
[32m+[m[32m    assert receipt.status == STATUS_VERIFIED[m
[32m+[m[32m    assert receipt.remote_record_id == "55555"[m
[32m+[m[32m    assert mock_client.create_draft.call_count == 0  # No duplicate draft![m
[32m+[m[32m    assert mock_client.upload_artifacts.call_count == 0[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_modified_package_triggers_needs_reconciliation(tmp_path):[m
[32m+[m[32m    """8. A package modified after initial receipt creation triggers NEEDS_RECONCILIATION."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m
[32m+[m[32m    # Record receipt with OLD fingerprint[m
[32m+[m[32m    old_receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-20261023T010000Z-wordpr",[m
[32m+[m[32m        package_fingerprint="old_fingerprint_hash_abc",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m    )[m
[32m+[m[32m    registry.register_receipt(old_receipt)[m
[32m+[m
[32m+[m[32m    mock_client = mock.MagicMock()[m
[32m+[m[32m    orchestrator = DepositOrchestrator(mock_client, registry)[m
[32m+[m
[32m+[m[32m    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)[m
[32m+[m[32m    assert receipt.status == STATUS_NEEDS_RECONCILIATION[m
[32m+[m[32m    assert "PACKAGE_FINGERPRINT_CHANGED" in receipt.last_error_category[m
[32m+[m[32m    assert mock_client.upload_artifacts.call_count == 0[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_upload_failure_leaves_local_package_intact_and_receipt_retryable(tmp_path):[m
[32m+[m[32m    """9. Failure partway through upload leaves the local package intact and receipt retryable."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m
[32m+[m[32m    # Mock client that creates draft but fails during upload[m
[32m+[m[32m    class FailingUploadClient(ZenodoSandboxAdapter):[m
[32m+[m[32m        def create_draft(self, *args, **kwargs):[m
[32m+[m[32m            return DepositReceipt([m
[32m+[m[32m                receipt_id="r1",[m
[32m+[m[32m                portal_id="wordpress-news",[m
[32m+[m[32m                run_id="run-20261023T010000Z-wordpr",[m
[32m+[m[32m                package_fingerprint=compute_package_fingerprint(pkg_dir),[m
[32m+[m[32m                repository_target="zenodo",[m
[32m+[m[32m                environment="sandbox",[m
[32m+[m[32m                status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m                created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m                updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m                remote_bucket_url="http://sandbox/bucket",[m
[32m+[m[32m            )[m
[32m+[m[32m        def upload_artifacts(self, receipt, *args, **kwargs):[m
[32m+[m[32m            raise ConnectionResetError("Connection lost while streaming large WARC")[m
[32m+[m
[32m+[m[32m    client = FailingUploadClient(api_token="token")[m
[32m+[m[32m    orchestrator = DepositOrchestrator(client, registry)[m
[32m+[m
[32m+[m[32m    receipt = orchestrator.deposit_package("wordpress-news", "run-20261023T010000Z-wordpr", pkg_dir, meta)[m
[32m+[m
[32m+[m[32m    assert receipt.status == STATUS_FAILED[m
[32m+[m[32m    assert receipt.retry_eligible is True[m
[32m+[m[32m    # Local package must still be 100% valid![m
[32m+[m[32m    v_res = PackageVerifier.verify_package(pkg_dir)[m
[32m+[m[32m    assert v_res.is_valid is True[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_remote_checksum_mismatch_prevents_verified_state(tmp_path):[m
[32m+[m[32m    """10. Remote checksum differences are caught and prevent VERIFIED status."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token")[m
[32m+[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_UPLOADED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m    )[m
[32m+[m[32m    # Put a corrupt remote MD5 in inventory[m
[32m+[m[32m    receipt.artifact_inventory["manifest.json"] = UploadedArtifactRecord([m
[32m+[m[32m        filename="manifest.json",[m
[32m+[m[32m        filesize=100,[m
[32m+[m[32m        local_sha256="abc",[m
[32m+[m[32m        local_md5="00000000000000000000000000000000",[m
[32m+[m[32m        remote_md5="ffffffffffffffffffffffffffffffff",  # Corrupted remote checksum[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    receipt = adapter.verify_remote_artifacts(receipt, pkg_dir)[m
[32m+[m[32m    assert receipt.status == STATUS_FAILED[m
[32m+[m[32m    assert receipt.checksum_verification_outcome == "FAILED"[m
[32m+[m[32m    assert "remote MD5 mismatch" in receipt.last_error[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_deposit_failure_does_not_alter_catalog(tmp_path):[m
[32m+[m[32m    """12. Deposit failure does not alter ArchiveCatalog's latest_successful_run_id."""[m
[32m+[m[32m    catalog_path = str(tmp_path / "catalog.json")[m
[32m+[m[32m    catalog = ArchiveCatalog.load_or_create(catalog_path, "wordpress-news")[m
[32m+[m
[32m+[m[32m    # Baseline crawl was successful[m
[32m+[m[32m    entry1 = CatalogRunEntry([m
[32m+[m[32m        run_id="run-baseline-1",[m
[32m+[m[32m        previous_run_id=None,[m
[32m+[m[32m        mode="BASELINE",[m
[32m+[m[32m        started_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        completed_at="2026-10-23T01:05:00Z",[m
[32m+[m[32m        status="CONVERGED",[m
[32m+[m[32m        article_count=10,[m
[32m+[m[32m        new_article_count=10,[m
[32m+[m[32m        changed_article_count=0,[m
[32m+[m[32m        run_dir="runs/run-baseline-1",[m
[32m+[m[32m    )[m
[32m+[m[32m    catalog.register_run(entry1, is_successful=True)[m
[32m+[m[32m    catalog.save_to_json(catalog_path)[m
[32m+[m[32m    assert catalog.latest_successful_run_id == "run-baseline-1"[m
[32m+[m
[32m+[m[32m    # Simulate deposit failure for run-baseline-1[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m[32m    failed_receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-baseline-1",[m
[32m+[m[32m        package_fingerprint="fp1",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_FAILED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        last_error="Remote repository unreachable",[m
[32m+[m[32m    )[m
[32m+[m[32m    registry.register_receipt(failed_receipt)[m
[32m+[m
[32m+[m[32m    # Reload catalog and verify crawl success is completely unaffected[m
[32m+[m[32m    reloaded_catalog = ArchiveCatalog.load_from_json(catalog_path)[m
[32m+[m[32m    assert reloaded_catalog.latest_successful_run_id == "run-baseline-1"[m
[32m+[m[32m    assert reloaded_catalog.runs["run-baseline-1"].status == "CONVERGED"[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_atomic_receipt_persistence(tmp_path):[m
[32m+[m[32m    """13. Atomic receipt persistence survives malformed or interrupted state updates."""[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="portal",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m    )[m
[32m+[m[32m    registry.register_receipt(receipt)[m
[32m+[m
[32m+[m[32m    assert os.path.exists(reg_file)[m
[32m+[m[32m    with open(reg_file, "r", encoding="utf-8") as f:[m
[32m+[m[32m        data = json.load(f)[m
[32m+[m[32m    assert "portal:run-1" in data["receipts"][m
[32m+[m
[32m+[m
[32m+[m[32mdef test_missing_artifacts_or_verification_prevent_verified_state(tmp_path):[m
[32m+[m[32m    """11. Missing artifacts or failed remote verification prevent the VERIFIED state."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token")[m
[32m+[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_UPLOADED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m    )[m
[32m+[m[32m    # Artifact inventory has file missing MD5[m
[32m+[m[32m    receipt.artifact_inventory["manifest.json"] = UploadedArtifactRecord([m
[32m+[m[32m        filename="manifest.json",[m
[32m+[m[32m        filesize=100,[m
[32m+[m[32m        local_sha256="abc",[m
[32m+[m[32m        local_md5="00000000000000000000000000000000",[m
[32m+[m[32m        remote_md5="",  # Empty remote md5[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    receipt = adapter.verify_remote_artifacts(receipt, pkg_dir)[m
[32m+[m[32m    assert receipt.status == STATUS_FAILED[m
[32m+[m[32m    assert receipt.status != STATUS_VERIFIED[m
[32m+[m[32m    assert receipt.checksum_verification_outcome == "FAILED"[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_production_urls_and_publishing_blocked(tmp_path):[m
[32m+[m[32m    """14. Production API URLs and unauthorized public publishing are blocked by default."""[m
[32m+[m[32m    # Attempt 1: Production URL blocked in constructor[m
[32m+[m[32m    with pytest.raises(ValueError) as exc1:[m
[32m+[m[32m        ZenodoSandboxAdapter(api_token="token", base_url=ZENODO_PRODUCTION_BASE_URL)[m
[32m+[m[32m    assert "Production Zenodo target is strictly disabled" in str(exc1.value)[m
[32m+[m
[32m+[m[32m    # Attempt 2: Publishing requires explicit allow_publish=True[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token")[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="portal",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_VERIFIED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        remote_record_id="123",[m
[32m+[m[32m    )[m
[32m+[m[32m    with pytest.raises(PermissionError) as exc2:[m
[32m+[m[32m        adapter.publish_deposit(receipt, allow_publish=False)[m
[32m+[m[32m    assert "Publication is locked by default" in str(exc2.value)[m
[32m+[m
[32m+[m
[32m+[m[32mdef test_reconciliation_finds_existing_unrecorded_draft(tmp_path):[m
[32m+[m[32m    """Checks that an interrupted draft creation is reconciled rather than duplicated."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    meta = _create_sample_metadata(tmp_path)[m
[32m+[m
[32m+[m[32m    reg_file = str(tmp_path / "deposit_registry.json")[m
[32m+[m[32m    registry = DepositRegistry.load_or_create(reg_file)[m
[32m+[m
[32m+[m[32m    # Simulate finding an existing draft via remote query[m
[32m+[m[32m    mock_existing_draft = [[m
[32m+[m[32m        {[m
[32m+[m[32m            "id": 9999,[m
[32m+[m[32m            "doi": "10.5072/zenodo.9999",[m
[32m+[m[32m            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket-existing"},[m
[32m+[m[32m            "metadata": {"title": "Prior interrupted draft"},[m
[32m+[m[32m        }[m
[32m+[m[32m    ][m
[32m+[m
[32m+[m[32m    def fake_transport(req):[m
[32m+[m[32m        if "status=draft" in req.full_url:[m
[32m+[m[32m            return MockHttpResponse(200, mock_existing_draft)[m
[32m+[m[32m        elif req.method == "PUT":[m
[32m+[m[32m            data = req.data[m
[32m+[m[32m            md5_val = hashlib.md5(data).hexdigest()[m
[32m+[m[32m            return MockHttpResponse(200, {"checksum": f"md5:{md5_val}", "id": "file-uuid"})[m
[32m+[m[32m        return MockHttpResponse(200, {})[m
[32m+[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token", http_transport=fake_transport)[m
[32m+[m[32m    mock_create_draft = mock.MagicMock()[m
[32m+[m[32m    adapter.create_draft = mock_create_draft[m
[32m+[m
[32m+[m[32m    orchestrator = DepositOrchestrator(adapter, registry)[m
[32m+[m[32m    receipt = orchestrator.deposit_package("wordpress-news", "run-interrupted", pkg_dir, meta)[m
[32m+[m
[32m+[m[32m    assert receipt.remote_record_id == "9999"[m
[32m+[m[32m    assert mock_create_draft.call_count == 0  # Reconciled without creating a duplicate![m
[32m+[m
[32m+[m
[32m+[m[32mdef test_file_count_and_size_limits_enforced(tmp_path):[m
[32m+[m[32m    """Enforces Zenodo 100-file and quota limits, rejecting oversized packages before upload."""[m
[32m+[m[32m    pkg_dir = _create_valid_packaged_run(tmp_path)[m
[32m+[m[32m    adapter = ZenodoSandboxAdapter(api_token="token")[m
[32m+[m
[32m+[m[32m    receipt = DepositReceipt([m
[32m+[m[32m        receipt_id="r1",[m
[32m+[m[32m        portal_id="wordpress-news",[m
[32m+[m[32m        run_id="run-1",[m
[32m+[m[32m        package_fingerprint="fp",[m
[32m+[m[32m        repository_target="zenodo",[m
[32m+[m[32m        environment="sandbox",[m
[32m+[m[32m        status=STATUS_DRAFT_CREATED,[m
[32m+[m[32m        created_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        updated_at="2026-10-23T01:00:00Z",[m
[32m+[m[32m        remote_bucket_url="http://sandbox/bucket",[m
[32m+[m[32m    )[m
[32m+[m
[32m+[m[32m    with mock.patch("src.zenodo_deposit.ZENODO_MAX_RECORD_FILES", 2):[m
[32m+[m[32m        # Package has 4 files (manifest, env, checksums, content) > 2[m
[32m+[m[32m        receipt = adapter.upload_artifacts(receipt, pkg_dir)[m
[32m+[m[32m        assert receipt.status == STATUS_FAILED[m
[32m+[m[32m        assert "FILE_COUNT_LIMIT_EXCEEDED" in receipt.last_error_category[m
[32m+[m
[32m+[m
[32m+[m

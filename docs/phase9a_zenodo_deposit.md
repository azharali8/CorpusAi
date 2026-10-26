# CorpusAI Phase 9A: Zenodo Sandbox Deposit Adapter and Receipt Tracking

## 1. Overview
CorpusAI Phase 9A implements verified online repository deposits to **Zenodo Sandbox** (`sandbox.zenodo.org`), decoupled deposit receipt tracking, and safe retry/reconciliation semantics in accordance with Professor Balázs Indig's preservation principles:

- **Local Verification Gate**: Packages must strictly pass `PackageVerifier.verify_package()` before any remote draft or file upload operation. Corrupted or incomplete packages are rejected locally.
- **Decoupled Deposit State**: Deposit outcomes are stored in a companion `deposit_registry.json`. A remote repository failure does **not** invalidate a successful crawl or alter `ArchiveCatalog.latest_successful_run_id`.
- **Safe Retries and Idempotency**: Stable package fingerprints (`compute_package_fingerprint`) prevent duplicate draft creation and resume interrupted uploads safely.
- **Remote Checksum Verification**: Validates Zenodo server-computed MD5 digests against local computations while maintaining local SHA-256 manifests.
- **Strict Rights and Safety Gates**: Production publishing is strictly blocked. Deposit metadata defaults to `access_right="restricted"` with academic research access conditions.

---

## 2. Deposit State Machine and Receipt Model

Deposits advance through a strict, deterministic state machine:

```
                  [PENDING]
                      │
           (validate local package)
                      │
                      ▼
               [DRAFT_CREATED]
                      │
             (stream artifacts)
                      │
                      ▼
                 [UPLOADING]
                      │
                      ▼
                 [UPLOADED]
                      │
            (verify remote hashes)
                      │
                      ▼
                 [VERIFIED]
                      │
             (explicit auth only)
                      │
                      ▼
                [PUBLISHED]
```

### State Transitions
| Status | Meaning | Transitions Allowed To |
| :--- | :--- | :--- |
| `PENDING` | Deposit requested, local verification in progress | `DRAFT_CREATED`, `FAILED`, `NEEDS_RECONCILIATION` |
| `DRAFT_CREATED` | Remote draft record created on Zenodo Sandbox | `UPLOADING`, `FAILED`, `NEEDS_RECONCILIATION` |
| `UPLOADING` | Streaming package artifacts to Bucket API | `UPLOADED`, `FAILED`, `NEEDS_RECONCILIATION` |
| `UPLOADED` | All files uploaded; awaiting remote checksum checks | `VERIFIED`, `FAILED`, `NEEDS_RECONCILIATION` |
| `VERIFIED` | Remote MD5 digests match local computations & package manifest | `PUBLISHED`, `FAILED` |
| `PUBLISHED` | Published on sandbox (terminal state; requires explicit flag) | *None* |
| `FAILED` | Transmission, verification, or auth error; local package intact | `DRAFT_CREATED`, `UPLOADING`, `UPLOADED`, `VERIFIED`, `NEEDS_RECONCILIATION` |
| `NEEDS_RECONCILIATION` | Fingerprint mismatch or ambiguous remote state | `DRAFT_CREATED`, `UPLOADING`, `UPLOADED`, `VERIFIED`, `FAILED` |

### Receipt Structure (`DepositReceipt`)
Stored in `deposit_registry.json` using atomic file replacement:
```json
{
  "receipt_id": "receipt-wordpress-news-run-20261023T010000Z-a1b2c3d4",
  "portal_id": "wordpress-news",
  "run_id": "run-20261023T010000Z-wordpr",
  "package_fingerprint": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "repository_target": "zenodo",
  "environment": "sandbox",
  "status": "VERIFIED",
  "created_at": "2026-10-26T21:40:00Z",
  "updated_at": "2026-10-26T21:42:00Z",
  "remote_record_id": "123456",
  "remote_bucket_url": "https://sandbox.zenodo.org/api/files/568377dd-daf8-4235-85e1-a56011ad454b",
  "doi": "10.5072/zenodo.123456",
  "checksum_verification_outcome": "VERIFIED_MD5_AND_LOCAL_SHA256",
  "artifact_inventory": {
    "manifest.json": {
      "filename": "manifest.json",
      "filesize": 1420,
      "local_sha256": "...",
      "local_md5": "...",
      "remote_md5": "...",
      "verified": true
    }
  }
}
```

---

## 3. Remote Checksum Verification Guarantees and Limitations

- **Zenodo API Behavior**: Zenodo's Bucket API (`PUT /api/files/<bucket_id>/<filename>`) calculates an **MD5** digest server-side upon receiving the complete byte stream, returning it in the upload response. Zenodo does not calculate SHA-256 server-side.
- **CorpusAI Verification Contract**:
  1. During file streaming, CorpusAI computes both **MD5** and **SHA-256** in a single pass without buffering whole files in memory.
  2. The local SHA-256 digest is strictly verified against the immutable local package's `checksums.sha256`.
  3. The local MD5 digest is strictly compared against the Zenodo server-calculated MD5.
  4. If any remote MD5 differs or is missing, the deposit status is marked `FAILED` with `checksum_verification_outcome="FAILED"`, and cannot advance to `VERIFIED`.

---

## 4. Rights, Safety, and Production Restrictions

- **Sandbox Target Only**: The adapter enforces `https://sandbox.zenodo.org/api`. Any attempt to pass `zenodo.org` (production) raises a `ValueError` immediately.
- **Locked Publication**: `publish_deposit()` raises `PermissionError` unless `allow_publish=True` is explicitly passed.
- **Copyright & Redistribution Safety**:
  - Web crawler archives contain third-party copyrighted material.
  - Deposits default to `access_right="restricted"` with academic evaluation conditions.
  - Automatic dissemination or open-access publication without institutional copyright review is forbidden.
- **Credential Protection**: The API token (`CORPUSAI_DEPOSIT_TOKEN`) is scrubbed from all logs, exception traces, and serialization structures.

---

## 5. Offline Testing and Optional Smoke Test

### Running the Offline Suite
All routine unit tests use a mocked HTTP transport and perform **zero real network requests**:
```bash
python -m pytest tests/test_zenodo_deposit.py -v
```

### Opt-In Manual Sandbox Smoke Test
An optional real network test is provided for manual validation against Zenodo Sandbox. It is skipped by default in all routine CI runs:
```bash
# Explicit opt-in only:
export CORPUSAI_ZENODO_SANDBOX_TOKEN="your_sandbox_token"
export CORPUSAI_RUN_ZENODO_SMOKE_TEST="1"
python -m pytest tests/test_zenodo_deposit.py -k "test_optional_sandbox_smoke_test_opt_in"
```

---

## 6. Persistence Gate & Unattended Scheduling Status

- **Durable Operational State**: Zenodo is an archival publication target, not a synchronization store for active crawler state.
- **Daily Scheduling Remains Disabled**: GitHub Actions runners are ephemeral. Recurrent unattended daily crawls remain gated until operational storage (for `catalog.json` and `portal_state.json`) is configured.

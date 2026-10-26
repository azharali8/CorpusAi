# CorpusAI Phase 9B: Durable State, Concurrency Protection, and Catalog Recovery

## 1. Overview
CorpusAI Phase 9B establishes operational state durability, cross-platform concurrency protection, pre-flight baseline integrity verification, catalog reconciliation, and conservative retention management in accordance with Professor Balázs Indig's preservation principles:

- **OS Advisory Locking**: Exclusive per-portal locking via `PortalLock` (`src/storage_backend.py`), using kernel-level locks (`msvcrt.locking` on Windows, `fcntl.flock` on Linux/POSIX). Locks have process-lifetime ownership and are automatically reclaimed by the OS kernel upon normal exit or process crashes.
- **Baseline Integrity Gate**: Incremental crawling runs `validate_baseline_integrity()` before issuing any network requests, independently verifying catalog pointers, package manifests, and `portal_state.json` consistency via `PackageVerifier.verify_package()`.
- **Catalog Reconciliation**: `CatalogReconciler` (`src/catalog_recovery.py`) inspects and safely heals discrepancies between physical packages on disk and `catalog.json`, with dry-run audit reporting and protection against ambiguous multiple-orphan situations.
- **Conservative Retention**: `StorageRetentionManager` safeguards finalized immutable archive packages and active baseline dependencies while safely reclaiming obsolete raw staging directories.
- **Decoupled Architecture**: Crawl state lineage and `DepositRegistry` receipts remain completely independent; failures in repository uploads or local synchronization do not corrupt crawl provenance.

---

## 2. Architecture and Storage Boundaries

The operational storage layout follows a three-tier hierarchy:

```
<results-base-dir>/
├── <portal-id>.lock                            # OS advisory file lock (kernel-level process ownership)
├── catalog.json                                # Central crawl history and active head pointer
├── deposit_registry.json                       # Decoupled repository deposit receipts (Phase 9A)
├── runs/                                       # Raw execution staging directories (reclaimable)
│   └── <mode>-<timestamp>/
│       ├── run.log                             # Dedicated operational log
│       ├── portal_state.json                   # Serialized ArticleState snapshot
│       ├── run_manifest.json                   # IncrementalRun traversal metrics
│       ├── run_summary.json                    # High-level outcome summary
│       ├── checksums.sha256                    # Checksums of staging directory
│       └── content_html.warc.gz                # Captured HTML WARC stream
└── packages/                                   # Verified immutable archival packages (NEVER deleted)
    └── <portal-id>/runs/<run-id>/
        ├── manifest.json                       # Canonical ArchivePackage manifest
        ├── checksums.sha256                    # Tamper-evident relative SHA-256 manifest
        ├── environment.json                    # Execution environment metadata
        ├── content/content_html.warc.gz        # Packaged WARC text records
        └── extra/portal_state.json             # Authoritative state snapshot
```

---

## 3. Concurrency Protection (`PortalLock`)

### Cross-Platform Implementation
- **Windows**: Uses Python's `msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)` locking the first byte of `<portal-id>.lock`.
- **Linux / macOS**: Uses `fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)` on `<portal-id>.lock`.

### Concurrency Guarantees
1. **Process-Lifetime Exclusivity**: The advisory lock is held by the operating system kernel for the duration of the open file descriptor.
2. **Crash Resilience**: If a runner process is killed (`SIGKILL`, unhandled exception, power loss), the kernel automatically releases the lock descriptor. Subsequent runs can acquire the lock immediately without requiring manual deletion of a stale marker file.
3. **No Time-Based Eviction**: An active long-running crawl will never lose its lock due to a fixed timeout expiration.
4. **Exit Code 8**: When a process attempts to crawl a portal whose lock is held by another process, it immediately halts and exits with deterministic exit code **8** (`LOCK_ACQUISITION_FAILED`).

---

## 4. Pre-Flight Baseline Integrity Gate

Before any network request is issued during an `--mode incremental` crawl, `validate_baseline_integrity()` performs seven mandatory checks:

1. **Catalog Pointer Existence**: `catalog.json` must record a non-empty `latest_successful_run_id`.
2. **Catalog History Match**: The ID must exist in `catalog.runs` with a recognized successful status (`CONVERGED`, `COMPLETED`).
3. **Physical Package Existence**: `packages/<portal-id>/runs/<run-id>/` must exist on disk.
4. **Independent Package Verification**: `PackageVerifier.verify_package()` must confirm that all files listed in `checksums.sha256` exist and match their SHA-256 digests, with no unexpected or missing files.
5. **Authoritative State Resolution**: `portal_state.json` is loaded from the verified package directory or staging run folder.
6. **State Deserialization**: The file must parse into a valid `PortalState` dataclass.
7. **Identity Verification**: `portal_state.portal_id` and `portal_state.run_id` must match the expected baseline parameters.

If any check fails, `validate_baseline_integrity()` raises `BaselineIntegrityError`, halting the runner with exit code **4** before any network sockets are opened.

---

## 5. Catalog Reconciliation and Recovery (`CatalogReconciler`)

### Detected Inconsistencies
| Condition | Description | Recovery Strategy |
| :--- | :--- | :--- |
| **Orphaned Verified Package** | A valid, verified package exists in `packages/` but has no entry in `catalog.json` (e.g. runner was killed after packaging but before catalog write). | If exactly one valid orphan exists, `reconcile()` adopts it as the active head in `catalog.json`. |
| **Ambiguous Multiple Orphans** | Multiple uncataloged packages exist. | Flagged as `repairable=False`; requires operator review to prevent silent pointer misassignment. |
| **Corrupted Active Head** | The active head package fails checksum verification or state loading. | If valid historical runs exist, `reconcile()` rolls back the head pointer to the most recent valid converged run. |
| **Missing Package** | A catalog entry references a package directory that was deleted. | Catalog entry is flagged for audit; active head is adjusted if affected. |

### CLI Usage
```bash
# Run pre-flight catalog audit and repair before crawling
python -m src.corpusai_run --portal wordpress-news --mode incremental --reconcile-catalog
```

---

## 6. Conservative Retention Policy (`StorageRetentionManager`)

### Safeguards and Invariants
1. **Immutable Packages Protected**: Directories in `packages/<portal-id>/runs/` are unconditionally excluded from cleanup (`never deleted`).
2. **Incremental Baseline Protected**: The staging folder corresponding to `catalog.latest_successful_run_id` is unconditionally protected.
3. **Diagnostic Retention**: Staging runs with recorded `anomalies` or `errors` in `run_summary.json` are retained for debugging.
4. **Bounded Staging Pruning**: Only unreferenced staging runs exceeding `min_keep_staging` (default: 5) are eligible for cleanup.

### CLI Usage
```bash
# Execute conservative staging cleanup alongside a crawl
python -m src.corpusai_run --portal wordpress-news --mode baseline --cleanup-staging
```

---

## 7. Operational Exit Codes

| Code | Meaning |
| :---: | :--- |
| **0** | Crawl converged and package verified |
| **1** | Crawl failed or unrecoverable error |
| **2** | Invalid CLI arguments or unknown portal |
| **3** | Robots.txt disallowed crawling |
| **4** | Missing or corrupt incremental baseline |
| **5** | Package verification failed |
| **6** | Request budget exhausted (checkpoint saved) |
| **7** | Manual review required (anomalies detected) |
| **8** | Concurrency lock acquisition failed (another process holds portal lock) |

---

## 8. Cross-Runner Ephemeral Storage Notice

> [!IMPORTANT]
> Local filesystem persistence and OS advisory locks protect continuous local hosts, long-running servers, and container volumes.
>
> However, **GitHub Actions runners are ephemeral virtual machines** that discard local disk state upon workflow completion. Scheduled automated daily crawling (`schedule: cron`) remains **deliberately disabled** until an external cross-runner durable storage backend (such as S3-compatible object storage or a persistent attached volume) is configured to synchronize `catalog.json`, `deposit_registry.json`, and archive packages across runner lifecycles.

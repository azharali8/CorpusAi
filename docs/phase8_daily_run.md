# CorpusAI Phase 8: Daily Run Orchestration, Recovery, and Operational Logging

## 1. Overview
CorpusAI Phase 8 provides a production-grade CLI runner (`corpusai_run.py`), intra-run transient recovery policies, structured operational logging, and workflow automation for daily news portal archival crawls.

The orchestrator integrates the entire CorpusAI stack:
- Ethical crawl compliance via `robots.txt` enforcement
- Adaptive rate limiting and polite request throttling
- Incremental discovery, pagination traversal, and body capture
- Intra-run exponential backoff retry for transient network and server errors
- Packaging and verification via `ArchivePackager` and `PackageVerifier`
- Run catalog updates with stateful pointer advancement
- Strict operational exit codes (0 to 7) for automated monitoring and alerting

---

## 2. CLI Invocation

### Syntax
```bash
python -m src.corpusai_run --portal <PORTAL_ID> --mode <MODE> [OPTIONS]
```

### Arguments and Defaults
| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--portal` | string | *Required* | Portal identifier defined in `PORTAL_CONFIGS` (e.g. `wordpress-news`). |
| `--mode` | string | *Required* | Execution mode: `baseline` or `incremental`. |
| `--results-dir` | path | `./results` | Root directory for run staging, packaging, and catalog storage. |
| `--dry-run` | flag | `False` | Validates portal configuration, displays operational parameters, and exits cleanly (exit code 0) without making network requests. |
| `--log-level` | string | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |

### Examples
```bash
# Validate portal configuration (no requests executed)
python -m src.corpusai_run --portal wordpress-news --mode baseline --dry-run

# Execute initial baseline crawl
python -m src.corpusai_run --portal wordpress-news --mode baseline --results-dir ./results

# Execute daily incremental crawl
python -m src.corpusai_run --portal wordpress-news --mode incremental --results-dir ./results --log-level INFO
```

---

## 3. Baseline vs Incremental Mode Semantics

### Baseline Mode (`--mode baseline`)
- **Purpose**: Establishes initial ground truth corpus state and archival snapshot.
- **Prerequisite**: No prior runs or catalog entries required. If a catalog already exists, the baseline run appends a new run entry.
- **Execution**: Discovers and captures all articles up to configured caps (`max_archive_pages`, `max_articles`).
- **Artifacts**: Generates initial `portal_state.json`, captures WARC records for all fetched articles, packages the run, and advances `latest_successful_run_id` in `catalog.json`.

### Incremental Mode (`--mode incremental`)
- **Purpose**: Synchronizes new articles while preserving bandwidth, server polite limits, and storage.
- **Prerequisite**: Requires an existing catalog containing a successful prior run (`latest_successful_run_id`) with accessible `portal_state.json`. If missing, the run aborts immediately with exit code 4.
- **Execution**: Traverses archive pages comparing observed articles against `previous_state`. Traversal stops as soon as known articles converge according to the incremental crawl policy.
- **WARC Optimization**: Only genuine `new` or `changed` articles are fetched and written to new WARC files. Unchanged articles reuse provenance records from prior runs; if 0 new articles are discovered, no redundant WARC files are created.
- **State Advancement**: If convergence is successful, updates `latest_successful_run_id` in `catalog.json`.

---

## 4. Run Directory Layout and Storage Architecture

Archival runs operate in three tiers: raw run staging, immutable package promotion, and catalog index tracking.

```
<results-dir>/
├── catalog.json                                # Central index tracking all runs for the portal
├── runs/                                       # Raw execution staging directories
│   ├── baseline-20261023T010000Z/
│   │   ├── run.log                             # Dedicated operational log
│   │   ├── portal_state.json                   # Serialized PortalState snapshot
│   │   ├── run_manifest.json                   # IncrementalRun manifest & traversal statistics
│   │   ├── run_summary.json                    # High-level machine-readable execution summary
│   │   ├── checksums.sha256                    # SHA-256 manifests for run directory
│   │   └── content_html.warc.gz                # Article HTML WARC container
│   └── incremental-20261024T010000Z/
│       ├── run.log
│       ├── portal_state.json
│       ├── run_manifest.json
│       ├── run_summary.json
│       └── checksums.sha256
└── packages/                                   # Verified immutable archival packages
    └── <portal-id>/
        └── runs/
            ├── run-20261023T010000Z-wordpr/    # Final promoted baseline package
            │   ├── manifest.json               # Canonical ArchivePackage manifest
            │   ├── checksums.sha256            # Tamper-evident checksums
            │   ├── environment.json            # Hardware/OS/software execution metadata
            │   ├── content/
            │   │   └── content_html.warc.gz
            │   └── extra/                      # Attached portal_state.json snapshot
            └── run-20261024T010000Z-wordpr/    # Final promoted incremental package
```

### Storage Safety Guarantees
1. **Catalog Safety**: The catalog (`catalog.json`) is updated **only** after `ArchivePackager` completes package staging and `PackageVerifier.verify_package` independently verifies checksum integrity.
2. **Immutability Enforcement**: `ArchivePackager` refuses to overwrite existing finalized run packages in `packages/<portal-id>/runs/<run_id>/`.

---

## 5. Exit Codes and Operational Response

The CLI runner uses deterministic process exit codes to communicate run outcomes to orchestration agents, cron monitors, and CI systems:

| Exit Code | Classification | Meaning | Recommended Operational Response |
| :---: | :--- | :--- | :--- |
| **0** | `SUCCESS` | Run succeeded, convergence achieved (`CONVERGED`), package verified, catalog updated (or dry-run succeeded). | Normal completion. No action needed. |
| **1** | `FETCH_FAILURE` | Crawl failed due to unrecoverable network, HTTP, or parser failure (retries exhausted). | Inspect `run.log`. Check portal network reachability, DNS, or server maintenance status. |
| **2** | `CONFIG_ERROR` | Unknown portal ID or invalid CLI arguments. | Verify portal ID against supported portals or update CLI invocation arguments. |
| **3** | `ROBOTS_DISALLOWED` | Portal `robots.txt` disallows crawling for configured head URL and User-Agent. | Do not bypass. Review `robots.txt` updates, adjust User-Agent or crawl path if authorized, or contact site owner. |
| **4** | `NO_BASELINE` | Incremental crawl attempted with no prior baseline in catalog or missing previous state. | Run `--mode baseline` first to establish initial corpus state. |
| **5** | `VERIFICATION_FAILED`| Archival package creation or verification failed (manifest mismatch or corrupt checksums). | Inspect `run.log` and package errors. Catalog was not advanced. Check disk integrity and run staging directory. |
| **6** | `BUDGET_EXHAUSTED` | Crawl terminated due to reaching max requests or max pages limit before convergence. | Review pagination activity in `run.log`. If portal had high volume of publications, increase budget parameters in portal configuration. |
| **7** | `ANOMALY_DETECTED` | Anomaly detected requiring manual inspection (e.g. `DRASTIC_DROP`, `ACCESS_GATE_DETECTED`, `HEAD_CHANGED`). | Inspect `anomalies_detected` in `run_summary.json` and review `run.log`. Catalog was not advanced to prevent corrupting state. |

---

## 6. Intra-Run Retry Policy

CorpusAI employs an intra-run retry loop within `IncrementalArchivePolicy` to recover from transient faults without failing an entire daily crawl.

### Triggers & Classification
- **Retriable Events**:
  - HTTP `5xx` Server Errors (`500`, `502`, `503`, `504`)
  - Network and transport exceptions (`requests.RequestException`, connection drops, read timeouts)
  - HTTP `429 Too Many Requests` (subject to rate-limit backoff rules)
- **Non-Retriable Events** (fail immediately without retry):
  - HTTP `200 OK` (success)
  - HTTP `404 Not Found` and `410 Gone` (resource deleted/absent)
  - HTTP `401 Unauthorized` and `403 Forbidden` (access gates, flagged as anomalies)

### Exponential Backoff & Delay Calculation
- For transient `5xx` errors and connection drops:
  $$\text{delay} = 2^{\text{attempt}}\text{ seconds}$$
  - Attempt 1: 2.0s
  - Attempt 2: 4.0s
  - Attempt 3: 8.0s
- For HTTP `429 Too Many Requests`:
  - Retried at most **once** per URL.
  - Inspects the `Retry-After` header. If present and numeric, sleeps for the indicated seconds (capped at **60s**, defaulting to **30s** if omitted or unparseable).

### Budget Accounting
Every HTTP request—initial attempts and subsequent retries alike—increments the crawler request counter against `budget.max_requests`. If the budget is exhausted during retry backoff, execution halts cleanly with `BUDGET_EXHAUSTED`.

### `retry_log` Structure
All retry events are recorded in `IncrementalRun.retry_log` and preserved in `run_manifest.json` and `run_summary.json`:
```json
{
  "timestamp": "2026-10-23T01:14:46Z",
  "url": "https://wordpress.org/news/sample-post",
  "attempt": 1,
  "status_code": 503,
  "error": null,
  "delay_seconds": 2.0
}
```

---

## 7. Operational Logging and Summary

Operational logging uses Python's standard `logging` framework configured with a dual handler architecture:
1. **Console Handler (`sys.stdout`)**: Clean human-readable operational events.
2. **File Handler (`<run-dir>/run.log`)**: Persistent execution record.

### Log Format
```
YYYY-MM-DDTHH:MM:SSZ [LEVEL] logger.name: Message
```
Example:
```
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Starting CorpusAI BASELINE run for portal 'wordpress-news'
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Run ID: run-20261022T201446Z-wordpr
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Crawl traversal finished. Convergence status: CONVERGED
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Article metrics: total=2, new=2, unchanged=0, changed=0, missing=0, failures=0
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Saved run summary to .../run_summary.json
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Package created and verified at .../runs/run-20261022T201446Z-wordpr
2026-10-23T01:14:46Z [INFO] corpusai.wordpress-news: Exiting with code 0 (success).
```

### `run_summary.json` Structure
Written to `<run-dir>/run_summary.json` at run conclusion:
```json
{
  "run_id": "run-20261022T201446Z-wordpr",
  "portal_id": "wordpress-news",
  "mode": "baseline",
  "status": "CONVERGED",
  "exit_code": 0,
  "started_at": "2026-10-23T01:14:46.123456+00:00",
  "completed_at": "2026-10-23T01:14:46.789012+00:00",
  "article_metrics": {
    "total": 2,
    "new": 2,
    "unchanged": 0,
    "changed": 0,
    "missing": 0,
    "failures": 0
  },
  "requests_made": 3,
  "anomalies_detected": [],
  "retries_count": 0,
  "package_dir": "results/packages/wordpress-news/runs/run-20261022T201446Z-wordpr"
}
```

---

## 8. Local Execution Instructions

To execute runs locally:

1. **Activate Python Virtual Environment**:
   ```bash
   # Linux/macOS
   source venv/bin/activate
   # Windows PowerShell
   .\venv\Scripts\Activate.ps1
   ```

2. **Verify Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run Dry-Run Check**:
   ```bash
   python -m src.corpusai_run --portal wordpress-news --mode baseline --dry-run
   ```

4. **Execute Crawl**:
   ```bash
   # Run initial baseline
   python -m src.corpusai_run --portal wordpress-news --mode baseline --results-dir ./results
   
   # Run incremental crawl
   python -m src.corpusai_run --portal wordpress-news --mode incremental --results-dir ./results
   ```

5. **Inspect Artifacts**:
   ```bash
   # View latest run summary
   cat results/runs/incremental-*/run_summary.json
   
   # View catalog
   cat results/catalog.json
   ```

---

## 9. GitHub Actions Daily Run Workflow

CorpusAI includes a GitHub Actions workflow located at `.github/workflows/daily_run.yml`.

### Manual Triggering
The workflow supports manual on-demand execution via GitHub's `workflow_dispatch` interface:
1. Navigate to the repository's **Actions** tab on GitHub.
2. Select **Daily Archival Crawl Run** from the workflow list.
3. Click **Run workflow**.
4. Choose the target portal (e.g., `wordpress-news`), mode (`incremental` or `baseline`), and log level (`INFO` or `DEBUG`).
5. After execution, the `run.log` and `run_summary.json` artifacts are available for download under **Artifacts**.

### Persistence Gate Rationale (Why Scheduled Triggers Are Commented Out)
In `.github/workflows/daily_run.yml`, the standard cron schedule:
```yaml
# schedule:
#   - cron: "0 2 * * *" # Daily at 02:00 UTC
```
is deliberately commented out.

**Reasoning**:
- GitHub Actions runners execute in ephemeral, stateless virtual machines that are destroyed upon job termination.
- An incremental crawler relies fundamentally on stateful continuity: the `catalog.json` and previous `portal_state.json` must be durable between daily runs.
- Without an external persistent storage backend (such as an AWS S3 bucket, GCP Cloud Storage bucket, self-hosted runner volume, or long-term artifact cache), each scheduled run would start with an empty results directory, causing incremental runs to fail with exit code 4 (`NO_BASELINE`).

### Enabling Scheduled Execution
Once durable remote persistence is configured:
1. Configure cloud credentials (e.g. AWS S3 secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`).
2. Add a pre-crawl step to restore `results/` from cloud storage (`aws s3 sync s3://${{ secrets.S3_BUCKET_NAME }}/results/ ./results/`).
3. Add a post-crawl step to push new runs, packages, and `catalog.json` back to cloud storage.
4. Uncomment the `schedule` trigger block in `.github/workflows/daily_run.yml`.

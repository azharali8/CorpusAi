# Archive Storage and Run Layout Design

## Overview
This document specifies the durable on-disk directory layout and metadata architecture for CorpusAI archival runs. It ensures reproducibility, immutable execution records, clear separation between primary corpus text and supporting visual replay assets, and readiness for academic repository deposits (e.g. Zenodo, Dataverse).

---

## Directory Layout Convention

For multi-crawl portals and incremental crawl synchronization, runs are organized into immutable timestamped run directories under a domain-scoped archive root:

```
archives/
  └── <portal-identifier>/                     # e.g., wordpress-news
      ├── catalog.json                         # Global index of all historical runs and current active head
      └── runs/
          ├── <run-id-1>/                      # e.g., run-20261006T171500Z-a1b2c3d4
          │   ├── manifest.json                # Complete machine-readable run manifest
          │   ├── checksums.sha256             # Tamper-evident checksums of all run artifacts
          │   ├── environment.json             # Execution environment (OS, Python, Docker, Crawler version)
          │   ├── deposit_metadata.json        # Optional Zenodo/Dataverse release metadata (DepositMetadata)
          │   ├── content/                     # Primary corpus text representations
          │   │   ├── content_html.warc.gz     # Article HTML response records
          │   │   └── content_json.warc.gz     # Structured REST API article payloads
          │   ├── replay/                      # Faithful visual replay artifacts
          │   │   ├── archive.wacz             # ReplayWeb.page-compatible WACZ package
          │   │   └── screenshots/             # Viewport and full-page PNG captures
          │   └── qa/                          # Automated replay QA outputs
          │       ├── qa_summary.json          # Automated replay comparison report
          │       └── qa.wacz                  # QA comparison archive (if generated)
          └── <run-id-2>/
```

---

## Artifact Specifications

### Required vs. Optional Artifacts

- **Mandatory Package Files:**
  - `manifest.json`: Machine-readable run manifest detailing `run_id`, `portal_id`, `mode`, `status`, article counts, timestamps, configuration parameters, and the relative path inventory of all packaged artifacts.
  - `environment.json`: Execution environment metadata recording Python version, platform details (OS/release/architecture), and crawler identifiers without leaking sensitive local user paths or secrets.
  - `checksums.sha256`: Tamper-evident SHA-256 manifest containing relative paths and hashes for every file in the package (excluding `checksums.sha256` itself).
- **Stream Artifacts (Packaged When Produced):**
  - `content/content_html.warc.gz`: Extracted textual HTML records in standard WARC ISO 28500 format.
  - `content/content_json.warc.gz`: REST API and structured JSON payloads in WARC format.
  - `replay/archive.wacz`: Full visual replay container (WACZ) preserving CSS, JavaScript, fonts, and images.
  - `replay/screenshots/`: Browser-rendered viewport or full-page PNG screenshots.
  - `qa/`: Replay QA comparison archives and summaries.
  - `deposit_metadata.json`: Optional structured rights, creators, and licensing declarations for institutional deposit.
- **No Phantom Archives:**
  - Unproduced optional artifacts (e.g. `content_json.warc.gz` when no REST API was crawled) are omitted rather than written as empty placeholder files.

---

## Checksum Coverage and Verification Contract

### Checksum Policy
1. Every file within the run directory is hashed using SHA-256 upon packaging.
2. Checksums are recorded in standard format: `<sha256-hex>  <relative-path-forward-slashes>`
3. **Non-Circular Guarantee:** `checksums.sha256` covers all artifacts in the package directory except itself. It never records its own hash.

### Verification (`PackageVerifier`)
The verifier (`src.archive_package.PackageVerifier`) operates on any finalized package directory and verifies:
1. **Mandatory Metadata:** `manifest.json`, `environment.json`, and `checksums.sha256` must exist and be valid JSON/checksum files.
2. **Missing Files:** All files listed in `checksums.sha256` must exist on disk.
3. **Integrity Mismatches:** File SHA-256 hashes must strictly match the recorded hashes.
4. **Unexpected Files:** Any unrecorded file found inside the package directory triggers a verification failure.

---

## Safe Finalization & Lifecycle Behavior

1. **Staged Promotion:**
   - Packages are initially assembled in an isolated temporary staging directory (`.stage_<run_id>_*`).
   - Package contents, manifests, and checksums are generated and verified in staging.
   - Upon successful verification, the staging directory is atomically promoted (`os.replace` / `shutil.move`) to its final path `archives/<portal_id>/runs/<run_id>/`.
   - If an error occurs during packaging or verification, the staging directory is deleted, preventing partially-written or corrupt packages from appearing in `runs/`.
2. **Immutable Run Protection:**
   - The packager explicitly refuses to overwrite an existing finalized run directory, raising a `FileExistsError`.
3. **Catalog Pointer Protection:**
   - When registered with `ArchiveCatalog`, `latest_successful_run_id` only advances if the run completed successfully (`status` in `CONVERGED`, `COMPLETED`, etc.). Failed runs (`status="FAILED"`) are recorded in the catalog history but leave `latest_successful_run_id` pointing to the prior successful baseline.

---

## Programmatic Usage

```python
from src.archive_package import ArchivePackager, PackageVerifier
from src.archive_catalog import ArchiveCatalog

packager = ArchivePackager("archives")
portal_id = "wordpress-news"
run_id = "run-20261023T000000Z-a1b2c3d4"

# Package run from captured artifacts
run_dir, result = packager.package_run(
    portal_id=portal_id,
    run_id=run_id,
    mode="BASELINE",
    status="COMPLETED",
    started_at="2026-10-23T00:00:00Z",
    content_html_warc="path/to/articles.warc.gz",
    replay_wacz="path/to/archive.wacz",
    screenshots={"post1.png": "path/to/shot.png"},
    article_count=10,
)

assert result.is_valid is True

# Standalone verification
verification = PackageVerifier.verify_package(run_dir)
if not verification.is_valid:
    print(f"Verification failed: {verification.checksum_mismatches}")
```

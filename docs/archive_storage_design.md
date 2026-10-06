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

## Core Storage Principles

1. **Immutable Run Directories:**
   - Once a crawl run finishes and computes its `checksums.sha256`, the directory contents are never modified in-place.
   - Any revisit, incremental addition, or repair is recorded as a new distinct `run-id`.

2. **Deterministic Run Identifiers:**
   - Run IDs follow the pattern `run-<UTC-compact-timestamp>-<config-hash>` (e.g., `run-20261006T171500Z-a1b2c3d4`).
   - The config hash is a deterministic short SHA-256 digest of normalized crawler parameters (scope, seed URLs, worker count, behaviors).

3. **Separation of Corpus Content vs. Replay Assets:**
   - **Content Stream (`content/`):** Contains pure textual and structural data (HTML, REST JSON, TEI/XML) required by NLP, linguistics, and text-mining pipelines.
   - **Replay Stream (`replay/`):** Contains full web-fidelity containers (WACZ/WARC) containing CSS, JavaScript, fonts, and responsive images needed by browser engines for historical visual reproduction.
   - An intact WACZ is never corrupted or split physically; instead, logical classification is recorded in `manifest.json`.

4. **Tamper-Evident Integrity Metadata:**
   - Every artifact (WARC, WACZ, JSON manifest, PNG screenshot) has its SHA-256 digest calculated and recorded in `checksums.sha256`.
   - The repository uses the term **"tamper-evident integrity metadata"** rather than "tamper-proof" or "censorship-proof".

5. **Exclusion of Binary Archives from Git:**
   - Large raw WARC files, WACZ containers, browser caches, and raw HTML snapshots are strictly excluded from Git tracking via `.gitignore`.
   - Only small JSON summaries, metadata manifests, and verification scripts are tracked in source control.

6. **Deposit and Rights Declarations:**
   - Each archival package includes structured `DepositMetadata` declaring creator affiliations, collection periods, license statuses (e.g. `Research-Only`, `InC`), and embargo dates before being uploaded to public or institutional data repositories.

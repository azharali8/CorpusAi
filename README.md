<div align="center">

# CorpusAI

**Robust, reproducible web-corpus crawling and archival workflows**

An experimental research prototype for deterministic discovery, revisit policies, multi-level archive traversal, and WARC provenance in digital preservation and corpus construction.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-168%20passing-brightgreen.svg)]()
[![Status](https://img.shields.io/badge/status-research%20prototype-orange.svg)]()

</div>

---

## Overview

**CorpusAI** investigates how to reliably discover, revisit, classify, and preserve web content for corpus-building workflows. Modern web portals present significant preservation challenges:
- **Unstable pagination** and mid-crawl archive content shifts
- **Nested category, topic, and forum taxonomies** with cycles and deep links
- **Access gates**, interstitial consent barriers, and redirect chains
- **Multi-page articles** split across sequential component URLs
- **Structured content-bearing JSON** alongside rendered HTML
- **Heterogeneous WARC files** mixing primary text with secondary page assets (CSS, JS, images, fonts)

CorpusAI evaluates deterministic crawler algorithms and architectural boundaries to make corpus acquisition inspectable, reproducible, and verifiable.

---

## Feature Overview

| Area | Current Capability | Scope |
| :--- | :--- | :--- |
| **Archive Discovery** | DOM-based article URL discovery using semantic selectors and path filtering | Deterministic / Live validated |
| **Pagination Stability** | Bounded revisit policies, ordered/unordered fingerprints, and head-page sentinels | Synthetic benchmarks |
| **Nested Archives** | Bounded BFS/DFS graph traversal with domain scoping, budgets, and cycle detection | Synthetic benchmarks |
| **Navigation Gates** | Finite state-machine handling for age gates, consent pages, and redirect chains | Synthetic benchmarks |
| **Multi-page Articles** | Logical multi-component assembly with complete/incomplete provenance tracking | Synthetic benchmarks |
| **Archival Storage** | WARC response generation with ISO 28500 record IDs, timestamps, and SHA-256 digests | warcio integration |
| **Content Separation** | Strict separation of content-bearing records (`content_html`, `content_json`) from assets | MIME-based classifier |
| **Real-World Validation** | Polite, bounded discovery, HTML capture, and REST JSON verification on WordPress News | Live WordPress validation |
| **AI Selector Proposal** | *Optional secondary module:* Local LLM selector generation with deterministic validation | Experimental (`Ollama`) |

---

## Current Status

- **Offline Unit Test Suite:** **168 passing tests** verified in an isolated offline environment.
- **Controlled Synthetic Experiments:** 100% test scenario completion across simulated unstable pagination, multi-level graph topologies, and navigation gates.
- **Real-World WordPress News Validation:**
  - **Phase 5A Discovery:** 10/10 article URLs matched the manually collected ground-truth sample with matching ordering.
  - **Phase 5B HTML Capture:** 3 sampled articles captured with title, publication date, author, canonical URL, and body text manually verified.
  - **Phase 5C Structured Content:** Confirmed authoritative article text in structured WordPress REST JSON matching HTML text with 1.0 normalized Jaccard word similarity.
  - **Browser-Backed Replay:** Browser-backed replay has not yet been performed.

> [!NOTE]
> All reported results are from controlled synthetic scenarios and a limited real-world sample (`https://wordpress.org/news/all-posts/`). They should not be generalized to arbitrary websites.

---

## Architecture

```mermaid
flowchart TD
    A["Target Web Portal"] --> B["Archive Discovery"]
    B --> C["Archive Graph Traversal"]
    C --> D["Pagination & Stability Policy"]
    D --> E["Content URL Registry"]
    E --> F["Content / Asset Classification"]
    F --> G["WARC / WACZ Provenance"]
    G --> H["Downstream Corpus Processing"]

    I["Optional AI Selector Proposal"] -.-> J["Deterministic Validator"]
    J -.-> E
```

*(AI selector proposal operates strictly as an optional side component outside the primary crawling pipeline).*

---

## Why CorpusAI?

Standard web scrapers often assume static pagination and flat URL hierarchies. In real preservation workflows, portals fail in subtle ways:
1. **Mid-Crawl Mutations:** Articles published during a crawl shift paginated pages, causing crawlers to miss articles at the head of the archive.
2. **Infinite Traversal Loops:** Deeply nested forum categories or calendar views trap crawlers in unbounded traversals.
3. **Multi-Component Fragmentation:** Multi-page articles are stored as disconnected records rather than coherent textual documents.
4. **Mixed Archive Bloat:** Archiving entire page asset trees complicates downstream text-mining pipelines that only require clean HTML or structured JSON.

CorpusAI implements explicit state tracking, visit budgets, and provenance registries to address these failure modes systematically.

---

## Core Design Principles

- **Deterministic Execution:** Core crawl traversal, state transitions, and extraction operate deterministically.
- **Explicit Provenance:** Every URL and record preserves its discovery path, archive origin, HTTP headers, and SHA-256 payload digest.
- **Bounded Revisit Policies:** Head verification and boundary reconciliation operate within strict request budgets.
- **No Silent Failures:** Incomplete multi-page articles, gate blocks, and budget exhaustion are explicitly classified.
- **Content vs. Asset Separation:** Content-bearing text is isolated from presentation assets in archival outputs.
- **Offline Reproducibility:** Core algorithms are tested against local fixtures without requiring network access.
- **AI Safety Boundary:** Where AI modules are explored:
  > *AI proposes. Deterministic systems validate. Human approval remains explicit.*
- **Human Review Boundary:**
  > *Real-world results are manually verified before being treated as validated.*

---

## Implemented Components

### 1. Crawling & Discovery
- **Archive Discovery (`src/archive_page_discovery.py`):** Structural DOM analysis for candidate article links with canonical path filtering.
- **Content URL Registry (`src/content_url_registry.py`):** Dedicated registry tracking article URLs, HTTP metadata, capture status, and WARC record IDs.
- **Multi-Level Graph Traversal (`src/archive_graph_policy.py`):** BFS/DFS graph traversals with cycle detection, domain scoping, and explicit node budgets.

### 2. Stability & Updating
- **Archive Visit Policies (`src/archive_visit_policy.py`):** Ordered/unordered page fingerprinting and mutation-resilient revisit policies.
- **Head-Page Verification (`src/head_verification.py`):** Sentinel polling and boundary reconciliation to detect mid-crawl archive insertions.

### 3. Content Handling & Gates
- **Navigation Gate Policy (`src/navigation_gate_policy.py`):** State-machine modeling for age gates, consent screens, redirect chains, and session cookie reuse.
- **Multi-Page Article Policy (`src/multipage_article_policy.py`):** Logical document collation tracking sequential component URLs.
- **Article Inspector (`src/article_inspector.py`):** Metadata extraction, referenced asset discovery, lazy-load detection, and content JSON classification.

### 4. Archival & Provenance
- **WARC Utilities (`src/warc_utils.py`):** Parsing, creating, and validating ISO 28500 WARC/WARC.GZ records with custom provenance headers.
- **Content Separation:** Dedicated separate outputs for HTML content (`content_html.warc.gz`), structured JSON (`content_json.warc.gz`), and supporting assets (`assets.warc.gz`).

### 5. Experimental AI Module
- **Rule Generator & Repair (`src/rule_generator.py`, `src/rule_repair.py`):** Prompt-driven selector generation via local Ollama models (`qwen2.5-coder:3b`) with schema constraints and statistical validation ($\ge 90\%$).

---

## Real-World Validation

Evaluated against **WordPress News** (`https://wordpress.org/news/all-posts/`):

- **Phase 5A — Article Discovery:**
  - Automated discovery independently extracted the first 10 articles without prior knowledge of the manual ground truth.
  - Achieved **10/10 exact set matches** with identical chronological ordering in the captured snapshot.
- **Phase 5B — HTML Content Capture:**
  - Polite sequential capture of the first 3 discovered articles with a 1.5s delay.
  - Extracted metadata (titles, publication dates, authors, canonical URLs) was manually verified against live pages.
  - Created isolated `content_html.warc.gz` (3 records, SHA-256 verified).
- **Phase 5C — Structured Content Investigation:**
  - Controlled query to the WordPress REST API (`/wp-json/wp/v2/posts?slug=owa-president`).
  - Confirmed structured `content.rendered` availability matching HTML body text with 1.0 normalized Jaccard word similarity.
  - Cataloged 23 referenced assets (images, CSS, JS, fonts) and lazy-load indicators.
  - *Browser-backed replay has not yet been performed* (documented with `full_page_replay_verified = false`).

---

## Experimental Results Summary

| Experiment | Observed Result | Evaluation Scope |
| :--- | :--- | :--- |
| **WARC MIME Classification** | HTML records separated from mixed page assets with 100% precision | Synthetic fixture |
| **Pagination Mutation** | Bounded revisit policy improved empirical archive coverage in tested scenarios | Synthetic simulation |
| **Head Verification** | Sentinel checking detected tested mid-crawl head-page insertions | Synthetic simulation |
| **Multi-Level Traversal** | Bounded BFS/DFS traversed nested taxonomy graphs without infinite loops | Synthetic simulation |
| **Gate / Multi-Page Handling** | Resolved valid gates; flagged broken/missing components as incomplete | Synthetic simulation |
| **WordPress Discovery** | 10/10 candidate article URLs matched manual human ground truth | Real-world validation |
| **Article HTML Capture** | 3/3 sampled articles captured and manually verified against live metadata | Real-world validation |
| **WordPress REST JSON** | Authoritative article body text confirmed in ~7 KB JSON vs. ~154 KB HTML | Real-world validation |

---

## Project Structure

```
CorpusAI/
├── config/                                        # Extraction schema and selector configurations
├── data/                                          # Local HTML sample fixtures
├── docs/                                          # Architecture specifications and component references
├── experiments/                                   # LLM and WARC selector experiments
│   ├── run_real_llm_experiment.py
│   ├── run_repair_demo.py
│   └── run_warc_selector_experiment.py
├── research/                                      # Specialized research tracks and benchmarks
│   ├── archiving/                                 # WARC storage and mixed-content analysis
│   │   ├── current_tools.md
│   │   ├── mixed_content_findings.md
│   │   └── experiments/
│   │       ├── create_mixed_warc.py
│   │       ├── inspect_warc_records.py
│   │       └── run_mixed_content_analysis.py
│   ├── crawling/                                  # Crawl stability and graph traversal benchmarks
│   │   ├── gated_navigation_findings.md
│   │   ├── multilevel_findings.md
│   │   ├── pagination_instability.md
│   │   └── experiments/
│   │       ├── run_gated_navigation_experiment.py
│   │       ├── run_head_verification_experiment.py
│   │       ├── run_multilevel_experiment.py
│   │       └── run_stability_experiment.py
│   └── real_world/                                # Real-world portal capture and validation
│       └── wordpress/
│           ├── article_capture_checklist.md
│           ├── manual_ground_truth.txt
│           ├── replay_checklist.md
│           ├── run_article_capture_validation.py
│           ├── run_asset_capture_validation.py
│           └── run_discovery_validation.py
├── results/                                       # Structured JSON manifests and metrics
├── src/                                           # Core library implementation
│   ├── archive_graph_policy.py
│   ├── archive_page_discovery.py
│   ├── archive_visit_policy.py
│   ├── article_inspector.py
│   ├── content_url_registry.py
│   ├── downloader.py
│   ├── evaluator.py
│   ├── extractor.py
│   ├── head_verification.py
│   ├── html_preprocessor.py
│   ├── multipage_article_policy.py
│   ├── navigation_gate_policy.py
│   ├── rule_generator.py
│   ├── rule_repair.py
│   ├── selector_validator.py
│   ├── warc_utils.py
│   └── webarticlecurator_adapter.py
├── tests/                                         # Comprehensive offline unit test suite (168 tests)
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Installation

### Prerequisites
- Python 3.11+
- Virtual environment tool (`venv` or `uv`)

```bash
git clone https://github.com/azharali8/CorpusAi.git
cd CorpusAi

python -m venv .venv

# On Windows:
.venv\Scripts\activate

# On Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Quick Start

### 1. Run the Offline Test Suite
```bash
pytest -v
```

### 2. Run Real-World Validation Scripts
```bash
# Validate article discovery against WordPress News
python research/real_world/wordpress/run_discovery_validation.py

# Execute polite capture of sampled articles into Content WARC
python research/real_world/wordpress/run_article_capture_validation.py

# Perform asset inventory and REST API JSON investigation
python research/real_world/wordpress/run_asset_capture_validation.py
```

### 3. Run Synthetic Crawl Stability Benchmarks
```bash
# Run unstable pagination simulation
python research/crawling/experiments/run_stability_experiment.py

# Run head-page verification benchmark
python research/crawling/experiments/run_head_verification_experiment.py

# Run multi-level archive traversal benchmark
python research/crawling/experiments/run_multilevel_experiment.py

# Run gated navigation and multi-page assembly simulation
python research/crawling/experiments/run_gated_navigation_experiment.py
```

---

## Testing

All standard unit tests run completely **offline** using local fixtures and deterministic mock responders.

```bash
pytest -v
```

**Current offline test suite:** `168 passing tests` (0 failures, 0 errors).

---

## WARC & Content Architecture

CorpusAI enforces a strict distinction between primary content records and secondary presentation resources:

- **`content_html.warc.gz`:** Contains HTTP response records for known article URLs.
- **`content_json.warc.gz`:** Contains verified structured content JSON (e.g. WordPress REST API posts).
- **`assets.warc.gz`:** Reserved exclusively for fetched supporting assets (CSS, JS, images, fonts).

Every record stores immutable metadata in its WARC record headers (`WARC-Record-ID`, `WARC-Target-URI`, `WARC-Date`, `WARC-Source-Archive`, and SHA-256 payload digest).

---

## Experimental AI Assistance

An experimental secondary module explores AI-assisted selector proposal using local LLMs:

- **Constrained Inference:** Uses local Ollama models (`qwen2.5-coder:3b`) with zero cloud data transmission.
- **Strict Verification:** Proposed CSS/XPath selectors must pass statistical validation ($\ge 90\%$ match rate, non-empty values) against representative HTML samples.
- **Human Approval:** Repaired selector rules are never automatically deployed into production configurations without explicit review.
- **Separation from Crawler:** AI inference has no role in URL discovery, traversal, or HTTP execution.

---

## Limitations

- **Prototype Scope:** CorpusAI is an experimental research framework, not a distributed production web crawler.
- **Limited Portal Coverage:** Real-world validation has currently been conducted on a small sample of WordPress News pages.
- **Replay Verification:** Browser-backed visual replay has not yet been performed.
- **No Access Bypasses:** CorpusAI does not attempt to bypass CAPTCHAs, paywalls, or authentication barriers.
- **Synthetic Generalization:** Benchmark results on simulated portal mutations reflect controlled conditions and may not directly generalize to all CMS architectures.
- **Politeness:** Crawling relies on polite rate-limiting (1–2s delays) and explicit `robots.txt` compliance.

---

## Responsible Crawling

When executing live real-world experiments, CorpusAI:
- Checks and complies with `robots.txt` directives.
- Uses an identifiable, academic research User-Agent.
- Enforces strict request timeouts (15s) and bounded retry counts.
- Restricts requests to sequential, low-frequency execution.
- Terminates immediately upon receiving access restriction or error responses.

---

## Roadmap

- [ ] Containerized browser-backed WACZ capture and automated visual replay auditing
- [ ] Real-world validation across forum and bulletin-board platforms
- [ ] Incremental day-to-day crawl synchronization and change detection
- [ ] Integration with downstream TEI/XML corpus transformation pipelines

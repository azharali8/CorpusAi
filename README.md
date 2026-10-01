# CorpusAI

CorpusAI is an experimental research prototype for robust, reproducible web-corpus crawling and archival workflows.

Its current focus is on:
- Archive and article discovery
- Deterministic visit policies
- Unstable pagination detection and mitigation
- Incremental verification and head-page sentinel monitoring
- Multi-level archive graph traversal and cycle handling
- Gated navigation policies (consent, age verification, redirect chains)
- Multi-page article assembly and provenance tracking
- WARC/WACZ provenance and MIME-based record classification
- Separating content records from supporting assets
- Real-world validation on WordPress-style portals

*(AI-assisted selector generation is maintained as an isolated experimental module, not as the primary crawler engine).*

---

## Motivation

Web portals can change dynamically while being crawled, use multi-level nested archive taxonomies, split articles across multiple pages, load media lazily, and mix primary textual content with diverse supporting web assets (CSS, JS, fonts, images).

CorpusAI investigates deterministic ways to make corpus acquisition, web archiving, and text extraction more reproducible, inspectable, and resilient to real-world crawl anomalies.

---

## Core Design Principles

- **Deterministic Execution:** Crawling and extraction pipelines operate on deterministic rules and deterministic state machines.
- **Explicit Provenance:** Every discovered URL and archived record preserves source archives, SHA-256 digests, HTTP metadata, and WARC Record IDs.
- **Bounded Revisit Policies:** Pagination boundaries and archive heads are verified with bounded overhead to detect mid-crawl content shifts.
- **No Silent Failures:** Structural failures, gate blocks, redirect loops, and budget exhaustion are explicitly classified.
- **Separation of Content and Assets:** Content-bearing records (HTML / JSON) are cleanly distinguished from presentation assets.
- **Offline Reproducibility:** Core algorithms and policies are thoroughly validated using comprehensive offline unit tests.
- **AI Proposal Boundary:** Where AI modules are used, the architecture strictly adheres to:  
  $$\text{AI Proposes} \longrightarrow \text{Validator Verifies} \longrightarrow \text{Human Approves} \longrightarrow \text{Deterministic Code Executes}$$

---

## Architecture

```
                      Target Web Portal
                             │
                             ▼
                   [ Archive Discovery ]
                             │
                             ▼
              [ Archive Graph Traversal ]
                             │
                             ▼
             [ Pagination / Stability Policy ]
                             │
                             ▼
                  [ Content URL Registry ]
                             │
                             ▼
             [ Content / Asset Classification ]
                             │
                             ▼
                 [ WARC / WACZ Provenance ]
                             │
                             ▼
               Downstream Corpus Processing
```

---

## Implemented Research Components

1. **Deterministic HTML Extractor:** High-throughput CSS/XPath extraction with clean normalization.
2. **Statistical Selector Validator:** Precision thresholding ($\ge 90\%$) against representative page sets.
3. **Optional Local LLM Selector Generator:** Experimental prompt-driven rule generation via local Ollama instances (`qwen2.5-coder:3b`).
4. **WARC Record Inspector & Classifier:** Precise MIME-based classification separating HTML records from mixed asset types.
5. **Unstable Pagination Simulator & Policies:** Benchmarking Naive, Precision, and Robust revisit strategies under simulated portal shifts.
6. **Head-Page Verification:** Sentinel checking and boundary reconciliation to ensure complete archive coverage during live insertions.
7. **Multi-Level Archive Graph Traversal:** Deterministic DFS/BFS graph traversal across nested archive taxonomies with strict domain scoping and cycle prevention.
8. **Navigation Gate Policy:** State-machine handling for age gates, interstitial consent pages, redirect chains, and multi-page article component collation.
9. **Real-World WordPress Discovery Validation:** Independent DOM-based discovery matching 10/10 manual ground-truth articles on live WordPress News.
10. **Separate Content WARC Capture:** Direct capture of verified article HTML into dedicated content WARCs (`content_html.warc.gz`).
11. **Structured Content JSON Investigation:** Automated discovery and classification of WordPress REST API endpoints (`wp-json/wp/v2/posts?slug=...`) for high-fidelity structured text capture.

---

## Current Validation Status

- **Synthetic Controlled Experiments:** 100% test coverage across simulated unstable pagination, multi-level graph topologies, and navigation gates.
- **Offline Unit Test Suite:** **168 passing tests** executed in isolated offline environments.
- **Small Real-World WordPress News Validation:**
  - **Phase 5A Discovery:** 100.0% precision and 100.0% recall against manually collected ground truth.
  - **Phase 5B HTML Capture:** 3 sampled articles successfully captured and verified against live title/author/date metadata.
  - **Phase 5C REST JSON Validation:** Confirmed authoritative article text availability in structured JSON matching HTML text with 1.0 normalized Jaccard word similarity.
  - **Replay Verification:** Browser-backed visual replay is documented as *awaiting manual verification* (`full_page_replay_verified = false`).

---

## Project Structure

```
CorpusAI/
├── config/              # YAML selector configurations and schema templates
├── data/                # Sample and test HTML fixtures
├── docs/                # Architecture specifications and component references
├── experiments/         # Synthetic simulation and benchmark runners
├── research/            # Research notes, WARC investigations, and real-world experiments
├── results/             # Structured JSON manifests and experiment metrics
├── src/                 # Core library modules
│   ├── archive_page_discovery.py
│   ├── article_inspector.py
│   ├── content_url_registry.py
│   ├── extractor.py
│   ├── head_verification.py
│   ├── multi_level_crawler.py
│   ├── navigation_gate_policy.py
│   ├── rule_generator.py
│   ├── selector_validator.py
│   ├── warc_utils.py
│   └── webarticlecurator_adapter.py
├── tests/               # Comprehensive offline pytest test suite
├── pyproject.toml       # Project metadata
├── requirements.txt     # Python dependencies
└── README.md
```

---

## Installation

### Requirements
- Python 3.11+
- Virtual environment recommended

### Setup

```bash
git clone https://github.com/elte-dh/CorpusAI.git
cd CorpusAI

python -m venv .venv

# Windows:
.venv\Scripts\activate

# Linux / macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Running Tests

Run the complete offline test suite:

```bash
pytest -v
```

Current test suite: **168 tests passing**.

---

## Research Experiments

To reproduce research experiments:

```bash
# Run unstable pagination simulator
python experiments/run_pagination_experiment.py

# Run head-page verification benchmark
python experiments/run_head_verification_experiment.py

# Run multi-level archive traversal benchmark
python experiments/run_multilevel_experiment.py

# Run real-world WordPress discovery validation
python research/real_world/wordpress/run_discovery_validation.py

# Run real-world article capture validation
python research/real_world/wordpress/run_article_capture_validation.py

# Run asset and REST JSON validation
python research/real_world/wordpress/run_asset_capture_validation.py
```

---

## Limitations

- **Scope:** CorpusAI is an experimental research prototype, not a broad distributed web crawler.
- **Coverage:** Real-world validation has been evaluated on small controlled samples (e.g. WordPress News).
- **Authentication & DRM:** No bypass mechanisms for CAPTCHAs, paywalls, or authentication gates.
- **Visual Replay:** Full visual fidelity replay in archival browsers requires containerized browser engines (e.g. Browsertrix) and human verification.
- **AI Selector Inference:** LLM-based selector generation is experimental and requires deterministic validation before production deployment.
- **Site Politeness:** Live requests must always strictly comply with `robots.txt` and domain rate limits.

---

## Roadmap

- Browser-backed WACZ capture and automated visual replay testing.
- Evaluation across additional real-world CMS architectures and discussion forums.
- Incremental and continuous crawl synchronization models.
- Downstream TEI / XML transformation workflows.

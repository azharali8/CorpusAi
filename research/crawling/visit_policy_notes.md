# Current Archiving Crawler Visit Policy Analysis (Extended)

**Scope:** WebArticleCurator, Browsertrix Crawler, pywb, and CorpusAI `ArchiveVisitPolicy`.  
**Purpose:** Investigate how modern archiving crawlers handle dynamic pagination shifts, mid-crawl insertions, archive boundary coverage, and head-page verification.

---

## 1. WebArticleCurator (ELTE-DH)

### A. Architecture & Crawling Mechanism
- *VERIFIED FROM SOURCE CODE & OFFICIAL CLI:*
- `WebArticleCurator` is designed primarily as a **targeted URL downloader and site-level sample collector**.
- Its crawl commands (e.g. `webarticlecurator crawl`) execute queue-based breadth-first or depth-first link traversal based on extracted HTML anchor tags (`<a href="...">`).
- **Pagination Handling:**
  - Standard link extraction relies on matching regex patterns or CSS selectors for pagination links.
  - Traversal is **strictly single-pass**: once a URL (e.g. `archive?page=2`) is fetched and written to WARC, it is marked as visited in an internal set and will **not be revisited** during the same crawl run.
- **Head Page Revisit Behavior:**
  - Does **not** revisit the archive head (Page 1) after downstream traversal.
  - If new content is published during a crawl, newly inserted head articles and boundary-shifted items are permanently missed.

---

## 2. Browsertrix Crawler (Webrecorder)

### A. Crawl Scope & Queue Dynamics
- *VERIFIED FROM OFFICIAL DOCUMENTATION & SPECIFICATIONS:*
- Browsertrix utilizes a sophisticated Chromium-based crawl engine with configurable scope rules (`include`, `exclude`, `maxDepth`, `limit`).
- **Discovery Queue & Frontier Deduplication:**
  - Discovered URLs are enqueued in a priority frontier.
  - Deduplication is performed by canonical URL (SURT / normalized URI).
  - Enforces a **single-visit policy per canonical URL**. Once `https://portal.test/archive?page=1` is loaded and its rendered DOM is archived, it is not automatically revisited unless explicitly configured with periodic re-crawl workflows.
- **Critical Distinction: URL Frontier Deduplication vs. Archive Stability Verification:**
  - URL frontier deduplication ensures a crawler does not download the *same URL* multiple times.
  - Archive stability verification evaluates whether the *set of entity links returned by paginated container URLs* has changed due to portal publication dynamics.
  - Common crawlers (including Browsertrix) solve URL deduplication cleanly, but do not perform multi-pass archive stability verification.

---

## 3. pywb (Python Wayback)

### A. Recording vs. Crawling Scope
- *VERIFIED FROM CODE & DOCUMENTATION:*
- `pywb` does not include an autonomous crawler engine; it records traffic routed through its proxy.
- Revisit detection in `pywb` operates at the **payload deduplication level** (writing `revisit` records when identical SHA-1 hashes are observed), not at the link discovery/traversal level.

---

## 4. Multi-Phase Crawl Model Comparison

| Dimension | WebArticleCurator | Browsertrix Crawler | CorpusAI `ArchiveVisitPolicy` (Task 2B) |
|---|---|---|---|
| **Traversal Model** | Single-pass link queue | Single-pass browser frontier | Multi-phase stability pipeline |
| **Phases** | Initial crawl only | Initial crawl only | `INITIAL_TRAVERSAL` → `HEAD_VERIFICATION` → `RECONCILIATION` → `FINAL_VERIFICATION` |
| **Ordered vs. Set Fingerprinting** | None | None | **Dual SHA-256 (Ordered + Set)** |
| **Head Sentinel Verification** | No | No | **Yes (Revisits Page 1 after traversal)** |
| **Boundary Shift Propagation** | No | No | **Yes ($N \rightarrow N+1$ bounded reconciliation)** |
| **Dynamic Page Count Expansion** | Unmanaged | Frontier expansion | **Yes (Detects newly created final page)** |
| **Empirical Convergence Metric** | Queue empty | Queue empty / Depth limit | **Set Stability + Head Verification + Queue Empty** |

---

## 5. Gated Navigation & Multi-Page Article Handling (Task 4)

### A. WebArticleCurator Analysis
- *VERIFIED FROM SOURCE CODE & OFFICIAL CLI:*
- **Access Gates / Interstitials:** WebArticleCurator lacks deterministic handling for age or consent interstitials. An age-confirmation gate returned at an article URL will either be downloaded as the raw HTML (storing the gate text instead of article content) or fail if a redirect is unhandled.
- **Multi-Page Articles:** Articles split across multiple pages (`?page=2`, `/continue`) are either enqueued as disconnected separate URLs or ignored if pagination patterns are unconfigured, without maintaining a unified logical article representation.

### B. Browsertrix Crawler Analysis
- *VERIFIED FROM OFFICIAL DOCUMENTATION & SPECIFICATIONS:*
- **Browser-Level Interaction vs. Policy Boundary:** Browsertrix runs a full Chromium instance with automated Behaviors (e.g. autoscroll, cookie acceptance heuristics). While a browser engine can physically click interstitial dialogs, generic crawlers lack an explicit **policy approval boundary** to determine whether a given gate action is permissible for corpus ingestion.
- **Multi-Page Articles in WARC:** Browsertrix captures multi-page component requests into its raw WARC stream, but does not assemble them into structured `LogicalArticle` manifests for downstream NLP pipelines.

---

## 6. Architectural Recommendations for Future Crawler Implementation
1. **Adopt Multi-Phase Traversal & Head Sentinels:** Separate initial traversal from head verification to catch dynamic publication shifts.
2. **Maintain Explicit Gate Policy Boundaries:** Decouple gate detection (`NavigationGatePolicy`) from graph traversal (`ArchiveGraphTraversalPolicy`) to allow fine-grained approval rules per portal origin.
3. **Track Origin-Scoped Session States:** Reuse verified gate tokens across all URLs within the same origin to avoid repeated interstitial handshakes.
4. **Assemble Component Pages into Logical Articles:** Ensure multi-page stories are grouped into single canonical records (`MultiPageArticlePolicy`) with ordered component page lists and WARC record references.
5. **Report Empirical Stability, Not Mathematical Completeness:** Clearly report `CONVERGED (empirical stability)` versus `BUDGET_EXHAUSTED` in crawler provenance manifests.

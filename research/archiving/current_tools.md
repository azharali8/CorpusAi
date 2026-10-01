# Current Web-Archiving Tooling Analysis

**Scope:** pywb, Browsertrix Crawler, WARC standard (ISO 28500), and warcio.  
**Purpose:** Investigate how modern archiving tools capture, store, index, and segregate HTML documents versus page assets.

---

## 1. pywb (Python Wayback)

### A. Version and Environment Status
- **Candidate Version:** `pywb==2.10.0` (as resolved via PyPI dry-run against Python 3.11).
- **Environment Status:** *Investigated via Dependency Analysis & Dry-Run Resolution*. `pywb` requires substantial legacy network stack dependencies (`gevent`, `brotlipy`, `wsgiprox`, `fakeredis`, `redis==2.10.6`, `pyOpenSSL`). To maintain CorpusAI's strict CPU-only, lightweight environment and prevent dependency breakage with `webarticlecurator` and `lxml`, pywb's recording architecture was analyzed from official specifications, source code, and WARC indexing standards rather than permanent global venv mutation.

### B. Core Architecture & Operation Modes
- **Functionality:** `pywb` is primarily an **indexing, playback, and replay engine** (Wayback machine implementation), not a headless browser crawler.
- **Recording Mode (`wayback --record`):** Acts as an HTTP/HTTPS interception proxy (using `wsgiprox` and `certauth`). When a browser or client requests resources through the pywb proxy, pywb captures raw HTTP request and response streams and appends them to WARC files (`.warc.gz`).
- **WARC Writing Behavior:** Every distinct HTTP transaction initiated by the client—including the root HTML document, stylesheet links (`.css`), scripts (`.js`), embedded images (`.png`, `.jpg`), AJAX/Fetch responses (`.json`), and web fonts (`.woff2`)—is recorded as an individual `response` WARC record.
- **Indexing & Representation:**
  - `pywb` generates CDXJ (CDX JSON) index files.
  - Each line in a CDXJ index contains: `SURT-URL timestamp JSON-metadata`.
  - The JSON metadata includes: `mime` (HTTP Content-Type), `status` (HTTP status code), `digest` (payload SHA-1 hash), `length` (compressed record length), and `offset` (byte offset in the WARC).
- **Deduplication / Revisit Records:**
  - When re-encountering identical assets with unchanged payloads, `pywb` writes `revisit` records referencing the prior capture digest and URI rather than duplicating the payload bytes.
- **HTML vs. Asset Distinguishability:**
  - `pywb` preserves exact HTTP response headers inside each response record.
  - The CDXJ index exposes `mime: "text/html"`, allowing downstream index lookups without decompressing the full WARC payload.

---

## 2. Browsertrix Crawler (Webrecorder)

### A. Core Architecture & Capture Mechanics
- **Role:** Modern, automated, high-fidelity browser-based crawler designed by Webrecorder.
- **Technology:** Runs automated Chromium browser instances controlled via Chrome DevTools Protocol (CDP) and Puppeteer.
- **Output Formats:**
  - Native **WACZ** (Web Archive Collection Zipped, ISO/TS 28500 / frictionless data container).
  - Internal standard **WARC 1.0/1.1** files (`archive.warc.gz`).

### B. Storage of Network Resources
- *VERIFIED FROM OFFICIAL SPECIFICATIONS & WACZ STANDARD (ISO/TS 28500-2):*
- When Browsertrix crawls a page, the headless Chromium instance loads and executes the full client-side web application.
- All network requests triggered by rendering (HTML, CSS, JS, XHR/Fetch, images, video chunks, web fonts) are captured via proxy/CDP and written as standard WARC response/request pairs into `archive.warc.gz`.
- Therefore, **the underlying WARC payload still stores HTML documents and auxiliary assets together in the same sequential archive stream**.

### C. Page Metadata & Resource Indexing in WACZ
- Unlike raw legacy WARCs, Browsertrix / WACZ creates a structured multi-layer archive:
  ```
  archive.wacz (ZIP container)
  ├── datapackage.json       (Metadata & manifest)
  ├── archive/
  │   └── data.warc.gz       (Full heterogeneous WARC records)
  ├── indexes/
  │   └── index.cdx.gz       (CDXJ index with MIME, status, offsets)
  └── pages/
      ├── pages.jsonl        (Page-level manifest)
      └── text/              (Optional extracted text resources)
  ```
- **`pages/pages.jsonl` (Page Manifest):**
  - Contains one JSON line per seed or navigated top-level page:
    ```json
    {
      "id": "page-1",
      "url": "https://example.org/article",
      "title": "Indigenous Knowledge",
      "ts": "2026-09-29T10:00:00Z",
      "size": 15420
    }
    ```
  - Explicitly separates **top-level document pages** from incidental background resource requests (CSS, scripts, tracking pixels).
- **Text Extraction Support:**
  - Browsertrix provides `--generateText` / text extraction options during crawling.
  - When enabled, extracted rendered text is saved in `pages/text/` or embedded in dedicated WARC `resource` records (`urn:uuid:...` with `WARC-Type: resource`), allowing immediate full-text search and NLP without re-parsing HTML or executing JavaScript.

---

## 3. WARC Standard (ISO 28500) & warcio

### A. Formal WARC Record Types
1. **`warcinfo`:** Describes the harvesting environment, software, crawling parameters, and format conformance.
2. **`response`:** Contains the complete network response (e.g. HTTP status line, HTTP headers, and raw payload body).
3. **`request`:** Contains the outbound HTTP request sent by the harvester.
4. **`resource`:** Contains a standalone resource directly harvested without a full HTTP network transaction (e.g. screenshots, DOM dumps, metadata).
5. **`revisit`:** Records verification that content was unchanged from an earlier capture, omitting the redundant payload.
6. **`metadata`:** Auxiliary metadata about another record (e.g. crawler logs, OCR data, classification tags).
7. **`conversion`:** Contains an altered version of another record's payload (e.g. converted PDF or plain text).

### B. Critical Distinction: WARC Content-Type vs. HTTP Content-Type

> [!IMPORTANT]
> **A frequent point of confusion in web archiving:**
> - For a `response` record, the **WARC header `Content-Type`** is almost universally `application/http; msgtype=response`. It describes the envelope format (an HTTP message).
> - The **actual media type of the payload** (HTML, PNG, CSS, JSON) resides in the inner **HTTP response header `Content-Type`** (e.g., `text/html; charset=utf-8`).
> - For a `resource` record, the WARC `Content-Type` directly specifies the payload MIME type (e.g. `text/plain` or `image/png`).

Attempting to filter HTML by inspecting only the outer WARC record headers will fail; a parser must parse the inner HTTP response header block.

---

## 4. Summary Matrix

| Archiving Tool | Capture Mechanism | Output Format | Assets Stored Together? | Distinguishing Mechanism | Downstream Extraction Difficulty |
|---|---|---|---|---|---|
| **Raw WARC / warcio** | Stream reader | `.warc` / `.warc.gz` | **Yes (Heterogeneous)** | Inner HTTP `Content-Type` parsing | Low (with proper parser) |
| **pywb** | Interception Proxy | `.warc.gz` + `.cdxj` | **Yes (Heterogeneous)** | CDXJ index `mime` field or HTTP headers | Low |
| **WebArticleCurator** | CLI Downloader | `.warc` / `.warc.gz` | **Yes (Per URL / Run)** | `extract_html_samples()` MIME filter | Low |
| **Browsertrix Crawler** | Headless Chromium | `.wacz` (`.warc.gz` + manifests) | **Yes in WARC, No in Pages Manifest** | `pages.jsonl` + CDXJ + `pages/text/` | Very Low |

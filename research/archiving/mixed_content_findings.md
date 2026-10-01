# Mixed-Content Storage & Archiving Findings

**Investigation Target:** Mixed-content storage in WARC workflows, SOTA tooling comparison, reliability of HTML record separation, and Wayback provenance linkages.  
**Research Question:** *Does the historical issue of mixed HTML and page assets in WARC files still pose a practical challenge for text-mining pipelines, and how is it addressed by modern tooling?*

---

## 1. Experimental Methodology & Results

### A. Controlled Controlled Fixture (`data/warc/mixed_content_test.warc`)
To evaluate whether HTML documents can be deterministically separated from secondary assets without relying on file extensions, a controlled heterogeneous WARC was constructed containing 11 distinct records:
1. `warcinfo` record (`application/warc-fields`)
2. `response` HTML (standard `.html` URL, `text/html; charset=utf-8`)
3. `response` HTML (extensionless URL `/article/123`, `text/html; charset=utf-8`)
4. `response` HTML (query parameter URL `/news?id=45`, `text/html; charset=utf-8`)
5. `response` CSS (`/assets/style.css`, `text/css`)
6. `response` JavaScript (`/assets/app.js`, `application/javascript`)
7. `response` Image (`/images/logo.png`, `image/png`)
8. `response` Image (misleading `.html` extension `/image.html`, `image/png`)
9. `response` JSON (`/api/metadata.json`, `application/json`)
10. `response` Text (`/robots.txt`, `text/plain`)
11. `resource` Non-HTTP metadata (`urn:example:...`, `text/plain`)

### B. Classification & Filtering Metrics
Inspection and evaluation were executed via `inspect_warc_records.py` and `run_mixed_content_analysis.py`, outputting `results/warc_mixed_content_analysis.json`:

```json
{
  "warc_file": "mixed_content_test.warc",
  "total_records": 11,
  "response_records": 9,
  "html_records": 3,
  "css_records": 1,
  "javascript_records": 1,
  "image_records": 2,
  "json_records": 1,
  "text_records": 1,
  "font_records": 0,
  "other_records": 2,
  "filtered_usable_html_count": 3,
  "expected_html_count": 3,
  "html_filter_accuracy": 1.0,
  "misclassified_records": []
}
```

### C. Experimental Findings
1. **MIME-based filtering achieved 100% precision and recall** without inspecting URL extensions.
2. **Extensionless URLs (`/article/123`, `/news?id=45`)** were successfully identified as HTML via HTTP `Content-Type: text/html`.
3. **Misleading URLs (`/image.html` containing PNG binary)** were correctly rejected from the HTML set and categorized as `IMAGE` based on its HTTP `Content-Type: image/png`.
4. **Outer vs. Inner Header Resolution:** The outer WARC header `Content-Type: application/http; msgtype=response` was correctly handled as an envelope header, and classification reliably decoded the inner HTTP payload header.

---

## 2. Definitive Conclusion on WARC Mixed-Content Storage

Among the four predetermined research outcomes, the experimental and architectural evidence supports:

> ### **B. FORMAT STILL MIXES RESOURCES, BUT TOOLING SOLVES MOST OF IT**
>
> *(With modern extensions moving toward Category C in WACZ workflows)*

### Detailed Explanation:
1. **Conceptual Persistence in the WARC Standard (ISO 28500):**
   The WARC standard was intentionally designed as a comprehensive, byte-accurate container for entire network transactions. Therefore, any crawler preserving full page fidelity (e.g. `pywb`, `heritrix`, `browsertrix`, `webarticlecurator`) will record stylesheets, scripts, fonts, and images in the same sequential WARC file.
2. **Tooling Resolution at the Parsing Layer:**
   Modern parsers (`warcio`, `fastwarc`, CDXJ indexers) parse the inner HTTP response header stream in single-pass linear time. As demonstrated by CorpusAI's `warc_utils.py` and `inspect_warc_records.py`, filtering out non-HTML responses requires only 4 lines of deterministic Python code:
   ```python
   if (record.rec_type == "response" and
       record.http_headers and
       record.http_headers.get_header("Content-Type", "").lower().startswith("text/html")):
       yield record
   ```
3. **Emergence of WACZ (Category C evolution):**
   Modern toolchains like Browsertrix Crawler wrap WARCs inside WACZ containers, providing top-level `pages/pages.jsonl` manifests and pre-extracted text (`pages/text/`). This allows text-mining systems to skip raw WARC parsing entirely when accessing curated pages.

---

## 3. Potential PR / Engineering Contribution Opportunity

### Assessment: Is there a meaningful PR opportunity?
- **For core `warcio` or `pywb`:** No PR is necessary. `warcio` already provides the necessary primitives (`ArchiveIterator`, `record.http_headers`, `StatusAndHeaders`), and `pywb` already writes standard CDXJ indices with `mime` fields.
- **For CorpusAI / WebArticleCurator / Downstream NLP pipelines:**
  There is a high-value engineering improvement: **A Standalone, Typed WARC Manifest & HTML Sieve Module**.
  
  **Concrete Contribution:**
  A lightweight utility module (similar to `src/warc_utils.py` / `inspect_warc_records.py`) that generates an external JSON/YAML manifest for any WARC archive before text extraction begins.
  
  Benefits:
  - Discovers and caches HTML record offsets.
  - Links root HTML document records to their dependent sub-resource records (CSS, JS, images) via capture timestamp and referrer/page URL.
  - Provides instant zero-overhead streaming access for downstream text-processing tools without repeatedly scanning the entire raw WARC.

---

## 4. Wayback Provenance & TEI Linkage Identifiers

When converting extracted articles to TEI XML or persistent corpus formats, each article must retain verifiable linkage to its web-archival origin.

### Available Stable Identifiers in WARC:

| Identifier | WARC Header / Field | Example Value | TEI / Provenance Role |
|---|---|---|---|
| **WARC-Record-ID** | `WARC-Record-ID` | `<urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6>` | Globally unique, immutable record key |
| **Target URI** | `WARC-Target-URI` | `https://example.org/article/123` | Original harvested web address |
| **Capture Timestamp** | `WARC-Date` | `2026-09-29T10:00:00Z` | Temporal anchor for Wayback replay URLs |
| **Payload Digest** | `WARC-Payload-Digest` | `sha1:B2UY56GOGACR7A...` | Cryptographic proof of payload integrity |
| **WARC Filename** | `WARC-Filename` / File path | `crawl_20260929.warc.gz` | Source container reference |
| **Wayback Replay URL** | Computed Pattern | `http://wayback.local/archive/20260929100000/https://example.org/article/123` | Direct link to replayable snapshot in pywb |

In future TEI output, these fields can be populated directly in the TEI Header `<biblStruct>` / `<sourceDesc>`:
```xml
<sourceDesc>
  <biblStruct>
    <monogr>
      <title level="m">Synthetic Portal</title>
      <idno type="WARC-Record-ID">urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6</idno>
      <idno type="WARC-Target-URI">https://example.org/article/123</idno>
      <idno type="WARC-Date">2026-09-29T10:00:00Z</idno>
      <idno type="WARC-SHA256">0de78fa112b39ba24c584957c2c9c21b7eb261e7dd0c80d4f60d8dd416fea305</idno>
    </monogr>
  </biblStruct>
</sourceDesc>
```

---

## 5. Summary of Test Validation
- **10/10 Classification tests passing** in `tests/test_warc_classification.py`.
- **Total test suite:** 69 passing tests (58 baseline + 11 new tests).
- All tests run offline with zero internet dependencies.

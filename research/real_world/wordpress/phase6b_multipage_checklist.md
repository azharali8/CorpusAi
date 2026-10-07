# Phase 6B.1 Multi-Page Baseline Manual Review Checklist

**Target Portal:** WordPress News  
**Seed Archive URL:** `https://wordpress.org/news/all-posts/`  
**Run ID:** `run-20261007T193716Z-wpnews-multi`  
**Review Status:** `VERIFIED`  
**manual_multipage_review_verified:** `true`  

> [!NOTE]
> Manual multi-page sanity review performed by the project owner against live WordPress News archive pages after automated discovery.

---

## 1. Multi-Page Scope Verification
- [x] Traversal covered exactly 5 archive pages (`https://wordpress.org/news/all-posts/` to `.../page/5/`).
- [x] Exactly 50 canonical article URLs discovered across pages 1–5 without duplicate entries.
- [x] Pagination links (`next_page_url`) correctly chained across all 5 pages.
- [x] Ordered and unordered fingerprints generated for all 5 archive pages.
- [x] Exact live HTTP request count recorded (15 total: 5 archive pages + 10 article bodies).
- [x] Full portal coverage explicitly marked `full_portal_coverage = false`.

---

## 2. Article Content Capture vs Discovery Separation
- [x] Top 10 newest articles (Page 1) have `content_capture_status = "CAPTURED"` with valid title, date, author, and content hashes.
- [x] Remaining 40 discovered articles (Pages 2–5) have `content_capture_status = "NOT_CAPTURED_IN_PILOT"` with unpolluted placeholders.
- [x] Zero unprompted live requests made for the 40 deferred articles.

---

## 3. Sample Article Inspection Checklist

### Sample A (Page 1 - Head Article): WordPress 7.1.3 Maintenance and Security Release
- **URL:** `https://wordpress.org/news/2026/10/wordpress-7-1-3-maintenance-and-security-release`
- [x] Canonical URL matches live portal address.
- [x] Article title extracted accurately: `WordPress 7.1.3 Maintenance and Security Release`.
- [x] Author extracted accurately: `Jake Spurlock`.
- [x] Publication date matches metadata: `2026-10-06T17:00:00+00:00`.
- [x] Content SHA-256 hash computed and recorded.

### Sample B (Page 1 - Feature Article): WordPress Takes Its Turn Leading the Open Website Alliance
- **URL:** `https://wordpress.org/news/2026/09/owa-president`
- [x] Canonical URL matches live portal address.
- [x] Article title extracted accurately: `WordPress Takes Its Turn Leading the Open Website Alliance`.
- [x] Publication date matches metadata: `2026-09-21T14:28:41+00:00`.
- [x] Content SHA-256 hash computed and recorded.

### Sample C (Page 3 - Interior Page): WCEU 2026 Recap
- **URL:** `https://wordpress.org/news/2026/06/wceu-2026-recap`
- [x] URL accurately discovered on archive Page 3 (`.../page/3/`).
- [x] Marked as `NOT_CAPTURED_IN_PILOT` (deferred in bounded pilot scope).
- [x] No extraneous HTTP payload fetched for deferred interior item.

### Sample D (Page 5 - Boundary Page): WordCamp Asia 2026
- **URL:** `https://wordpress.org/news/2026/01/wordcamp-asia-2026`
- [x] URL accurately discovered on boundary Page 5 (`.../page/5/`).
- [x] Marked as `NOT_CAPTURED_IN_PILOT` (deferred in bounded pilot scope).
- [x] Boundary fingerprint recorded for Page 5.

---

## 4. Lineage and Integrity Verification
- [x] `manifest.json` schema matches `IncrementalRun` structure with `scope_completion = "MULTIPAGE_PILOT_COMPLETE"`.
- [x] `portal_state.json` contains 5 archive pages and 50 articles.
- [x] `catalog.json` registers `run-20261007T193716Z-wpnews-multi` as latest successful run.
- [x] `checksums.sha256` verifies all artifacts intact.

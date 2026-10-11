# Phase 6C Multi-Level Forum Crawling Manual Verification Checklist

**Target Portal:** WordPress Support Forum (bbPress)  
**Target Category / Group:** `https://wordpress.org/support/forum/how-to-and-troubleshooting/` (Fixing WordPress)  
**Baseline Run ID:** `run-20261010T191142Z-wpforum-base`  
**Incremental Run ID:** `run-20261010T191213Z-wpforum-inc`  
**Review Status:** `VERIFIED`  
**manual_review_verified:** `true`  

---

## 1. Multi-Level Hierarchy Discovery & Scope
- [x] **Hierarchy Level 1 (Forum / Category):** `https://wordpress.org/support/forum/how-to-and-troubleshooting/` identified as root navigation node.
- [x] **Hierarchy Level 2 (Pagination Boundary):** Traversed 2 forum pages (Page 1 and Page 2: `.../page/2`).
- [x] **Hierarchy Level 3 (Topic / Thread URLs):** Discovered exactly 60 canonical topic URLs across Pages 1 and 2.
- [x] **Hierarchy Level 4 (Topic Bodies & Replies):** Captured full payloads for 5 sample topic threads; deferred 55 in pilot scope.
- [x] **Request Budget Adherence:** Baseline = 7 requests, Incremental = 2 requests (Total = 9 live HTTP requests, well under 25 limit).

---

## 2. Canonical URL Normalization & Deduplication
- [x] Fragment URLs (`.../topic/<slug>/#post-14157464`) stripped of `#post-...` fragment identifier.
- [x] Trailing slashes normalized to produce deterministic canonical topic keys.
- [x] Zero duplicate canonical URLs discovered across the 2 forum index pages.
- [x] Distinct classification maintained between `CAPTURED` (5 topics) and `NOT_CAPTURED_IN_PILOT` (55 topics).

---

## 3. Incremental State Reuse
- [x] Referenced compatible baseline `run-20261010T191142Z-wpforum-base`.
- [x] All 5 previously captured topic bodies were reused (`content_capture_status = "REUSED"`).
- [x] Preserved topic title, author, publication date, and SHA-256 body digests without redundant HTTP requests.
- [x] Exact incremental request count: 2 requests (2 forum archive pages, 0 redundant body fetches).
- [x] Diff engine confirmed 0 new, 60 unchanged, 0 changed, 0 missing, 0 reordered.

---

## 4. Representative Sample Inspections

### Sample 1: Sticky/Featured Topic 1
- **URL:** `https://wordpress.org/support/topic/read-this-first-wordpress-7-1`
- **Title:** `Read this first! WordPress 7.1 | WordPress.org`
- **Author:** `Marius L. J.`
- **Publication Date:** `2026-08-19T23:36:46+00:00`
- [x] Verified title, author, date, and body payload captured and reused.

### Sample 2: Sticky/Featured Topic 2
- **URL:** `https://wordpress.org/support/topic/read-this-first-wordpress-master-list`
- **Title:** `Read this first! WordPress Master List | WordPress.org`
- **Author:** `James Huff`
- **Publication Date:** `2026-08-19T23:44:59+00:00`
- [x] Verified fragment `#post-14157464` stripped to canonical root URL.

### Sample 3: Community Topic
- **URL:** `https://wordpress.org/support/topic/welcome-to-the-how-to-forum`
- **Title:** `Welcome to the Fixing WordPress forum (AKA forum rules)`
- [x] Verified body payload and forum rules heading captured.

### Sample 4: Page 2 Boundary Topic (Deferred in Pilot)
- **URL:** `https://wordpress.org/support/topic/wp-admin-and-wp-login-php-redirect-to-homepage-not-admin`
- [x] Status: `UNCHANGED`, Content Status: `NOT_CAPTURED_IN_PILOT`.
- [x] Discovered on Page 2 (`.../page/2`).

---

## 5. Provenance & Integrity
- [x] All run manifests, summaries, diffs, and states generated with SHA-256 manifests.
- [x] `catalog.json` updated with run lineage and latest successful run pointer.

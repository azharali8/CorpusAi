# Phase 6B.2 Real Multi-Page Incremental Crawl Report

**Run ID:** `run-20261010T185118Z-wpnews-multi-inc`  
**Mode:** `INCREMENTAL` (Multi-Page Pilot)  
**Previous Baseline Run ID:** `run-20261007T193716Z-wpnews-multi`  
**Target:** `https://wordpress.org/news/all-posts/`  
**Started:** `2026-10-10T18:51:18Z`  
**Completed:** `2026-10-10T18:51:35Z`  

---

## 1. Incremental Execution Summary
- **Archive Pages Visited:** 5 (Pages 1 to 5)
- **Total Articles Discovered:** 50
- **Articles Reused from Baseline:** 10 (`content_capture_status = "REUSED"`)
- **Articles Deferred in Pilot Scope:** 40 (`content_capture_status = "NOT_CAPTURED_IN_PILOT"`)
- **Newly Discovered Articles:** 0
- **Newly Captured Bodies:** 0
- **Live HTTP Requests Made:** 5 (5 archive pages, 0 redundant article body fetches)
- **Scope Completion:** `MULTIPAGE_PILOT_COMPLETE`
- **Full Portal Coverage:** `false` (bounded 5-page pilot)
- **Boundary Status:** `BOUNDARY_RECONCILED`
- **Head Changed:** `False`
- **Factual Result:** `REAL_MULTIPAGE_INCREMENTAL_NO_CHANGE`

---

## 2. Archive Pages Comparison
| Page # | Archive Page URL | Discovered URLs | Status vs Baseline | Fingerprint Match |
|---|---|---|---|---|
| 1 | `https://wordpress.org/news/all-posts/` | 10 | `UNCHANGED` | `True` |
| 2 | `https://wordpress.org/news/all-posts/page/2/` | 10 | `UNCHANGED` | `True` |
| 3 | `https://wordpress.org/news/all-posts/page/3/` | 10 | `UNCHANGED` | `True` |
| 4 | `https://wordpress.org/news/all-posts/page/4/` | 10 | `UNCHANGED` | `True` |
| 5 | `https://wordpress.org/news/all-posts/page/5/` | 10 | `UNCHANGED` | `True` |

---

## 3. Discovered Articles Sample Across Pages 1, 3, 5
| # | Page | Title | Date | Status | Content Status | Canonical URL |
|---|---|---|---|---|---|---|
| 1 | `https://wordpress.org/news/all-posts/` | WordPress 7.1.3 Maintenance and Security Release | 2026-10-06T17:00:00+00:00 | `UNCHANGED` | `REUSED` | `https://wordpress.org/news/2026/10/wordpress-7-1-3-maintenance-and-security-release` |
| 2 | `https://wordpress.org/news/all-posts/` | WordPress 7.1.2 Release | 2026-09-22T14:01:20+00:00 | `UNCHANGED` | `REUSED` | `https://wordpress.org/news/2026/09/wordpress-7-1-2-release` |
| 3 | `https://wordpress.org/news/all-posts/` | WordPress Takes Its Turn Leading the Open Website Alliance | 2026-09-21T14:28:41+00:00 | `UNCHANGED` | `REUSED` | `https://wordpress.org/news/2026/09/owa-president` |
| 4 | `https://wordpress.org/news/all-posts/` | WordPress 7.1.1 Maintenance and Security Release | 2026-09-17T19:53:09+00:00 | `UNCHANGED` | `REUSED` | `https://wordpress.org/news/2026/09/wordpress-7-1-1-maintenance-and-security-release` |
| 5 | `https://wordpress.org/news/all-posts/` | Students Built 108 Websites in Two Weeks in Bangladesh | 2026-09-07T23:19:34+00:00 | `UNCHANGED` | `REUSED` | `https://wordpress.org/news/2026/09/108-student-websites` |
| 21 | `https://wordpress.org/news/all-posts/page/3/` | (Not inspected in pilot) | N/A | `UNCHANGED` | `NOT_CAPTURED_IN_PILOT` | `https://wordpress.org/news/2026/06/kim-parsell-wcus-scholarship` |
| 22 | `https://wordpress.org/news/all-posts/page/3/` | (Not inspected in pilot) | N/A | `UNCHANGED` | `NOT_CAPTURED_IN_PILOT` | `https://wordpress.org/news/2026/06/global-partners-first-half-2026` |
| 41 | `https://wordpress.org/news/all-posts/page/5/` | (Not inspected in pilot) | N/A | `UNCHANGED` | `NOT_CAPTURED_IN_PILOT` | `https://wordpress.org/news/2026/03/announcing-my-wordpress` |
| 50 | `https://wordpress.org/news/all-posts/page/5/` | (Not inspected in pilot) | N/A | `UNCHANGED` | `NOT_CAPTURED_IN_PILOT` | `https://wordpress.org/news/2026/01/wordcamp-asia-2026` |

---

## 4. Storage and Provenance Artifacts
- **Portal State File:** `portal_state.json`
- **Manifest File:** `manifest.json`
- **Diff File:** `diff.json`
- **Run Summary File:** `run_summary.json`
- **Request Log File:** `request_log.json`
- **Integrity Manifest:** `checksums.sha256`

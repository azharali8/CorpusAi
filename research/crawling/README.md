# Crawling & Pagination Instability Research

**Area:** Crawler Architecture / Archive Traversal Policies  
**Focus:** Unstable Pagination, Same-Date Reordering, Mid-Crawl Mutation, Stale Caching  
**Lead Researcher:** CorpusAI Project  
**Date:** September 2026

---

## Executive Summary

When harvesting news portals and historical archives, paginated archive listings often display non-deterministic behavior:
1. **Same-Date Random Reordering:** Articles published on the same calendar day appear in unstable order across repeated visits to the same page.
2. **Cross-Page Shifts:** Articles move across page boundaries ($N \leftrightarrow N+1$).
3. **Mid-Crawl Article Insertion:** New breaking articles are published while the crawler is mid-traversal, shifting all existing page contents downward.
4. **Stale/Frozen Caching:** CDN/HTTP proxies serve outdated page states before eventually updating.

This research track defines the formal representations, fingerprinting models, global tracking ledgers, and deterministic visit policies (`NAIVE`, `PRECISION`, `ROBUST`) to measure and mitigate link discovery loss.

## Directory Layout

- `README.md`: Overview of the crawling stability research track.
- `pagination_instability.md`: Experimental benchmark analysis across Scenarios A–E, comparing recall, request costs, and boundary shifts.
- `visit_policy_notes.md`: Technical investigation of current crawling systems (`WebArticleCurator`, `Browsertrix Crawler`, `pywb`) and their handling of dynamic pagination shifts.
- `experiments/`:
  - `portal_simulator.py`: Deterministic local portal simulator modeling Scenarios A through E.
  - `run_stability_experiment.py`: Comprehensive benchmark suite evaluating `NAIVE`, `PRECISION`, and `ROBUST` visit policies against ground truth.

## Quick Links

- Policy Engine: `src/archive_visit_policy.py`
- Test Suite: `tests/test_archive_visit_policy.py` (15 offline unit tests)
- Experiment Artifact: `results/pagination_stability_experiment.json`

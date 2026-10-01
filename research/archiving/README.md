# Archiving & Web-Scale Extraction Research

**Area:** Archiving Sub-system / Crawler Architecture  
**Focus:** WARC Storage, Mixed Content, Tooling Analysis (pywb, Browsertrix, warcio)  
**Lead Researcher:** CorpusAI Project  
**Date:** September 2026

---

## Executive Summary

This research area investigates whether modern web-archiving workflows still store diverse page assets (HTML, CSS, JS, images, fonts, JSON) alongside text-mining targets in WARC files, and how downstream processing pipelines (such as CorpusAI, WebArticleCurator, and HTML2TEI) can reliably separate text/HTML from secondary assets.

## Directory Layout

- `README.md`: Overview of the archiving research track.
- `current_tools.md`: Comprehensive technical analysis of `pywb`, `Browsertrix Crawler`, `warcio`, and the ISO 28500 WARC standard.
- `mixed_content_findings.md`: Experimental findings, evaluation report on mixed content filtering, conclusions on the research question, potential PR/contribution opportunities, and Wayback provenance linkages.
- `experiments/`:
  - `create_mixed_warc.py`: Controlled synthetic generator for heterogeneous WARC fixtures (HTML, CSS, JS, PNG, misleading `.html` PNGs, JSON, text, warcinfo, and resources).
  - `inspect_warc_records.py`: Structured record inspector and MIME-based classifier.
  - `run_mixed_content_analysis.py`: Quantitative evaluator outputting `results/warc_mixed_content_analysis.json`.

## Quick Links

- [Current Archiving Tooling Analysis](current_tools.md)
- [Mixed-Content Findings & SOTA Evaluation](mixed_content_findings.md)
- Test Suite: `tests/test_warc_classification.py`
- Analysis Artifact: `results/warc_mixed_content_analysis.json`

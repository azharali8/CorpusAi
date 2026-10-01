# HTML2TEI Integration Notes

**Author:** CorpusAI Project  
**Date:** 2026-09  
**Status:** Investigation only — no code implemented  
**Phase:** 4 (WARC Integration)

---

## Overview

This document records an investigation into how CorpusAI could assist or extend the **ELTE-DH HTML2TEI** workflow in future work. No HTML2TEI code is implemented in CorpusAI. This file is a design note for researchers.

---

## What is HTML2TEI?

[HTML2TEI](https://github.com/ELTE-DH/HTML2TEI) is an ELTE Digital Humanities tool that converts portal-specific HTML content into well-structured **TEI XML** (Text Encoding Initiative format). It operates after WebArticleCurator has archived pages into WARC files.

The pipeline in the ELTE-DH workflow is approximately:

```
Web portal
    ↓
WebArticleCurator (WARC archiving)
    ↓
HTML2TEI
  ├── content-tree extraction (portal-specific rules)
  ├── inventory-maker (identifies content blocks)
  └── bigram-model (language boundary detection)
    ↓
TEI XML corpus
```

HTML2TEI relies on **portal-specific rule files** (Python dictionaries / config) that define exactly which HTML elements contain article content for each publication. These rule files are written and maintained manually.

---

## Current Manual Effort in HTML2TEI

The main bottleneck in the HTML2TEI workflow is creating and maintaining these portal-specific rule files. When a portal redesigns its layout:

1. A human must inspect the new HTML structure.
2. New selectors / rules must be written or updated.
3. The updated rules must be validated against sample pages.
4. The workflow must be re-run.

This is the same class of problem CorpusAI is designed to address.

---

## How CorpusAI Could Assist HTML2TEI

### Potential integration points (investigation only):

| CorpusAI Component | Potential HTML2TEI Role |
|----|-----|
| `html_preprocessor.preprocess_html()` | Clean archived HTML before rule inference |
| `OllamaRuleGenerator.generate_rules()` | Propose candidate selectors for HTML2TEI portal rule files |
| `SelectorValidator.validate_rule_set()` | Validate candidate rules against a sample of WARC pages |
| `RuleRepairManager` | Detect and propose repair for broken portal rules after layout changes |
| `warc_utils.extract_html_samples()` | Supply representative HTML from existing WARC archives |

The **AI proposes → Validator verifies → Human approves → Deterministic code executes** principle would apply equally to HTML2TEI rule generation.

---

## Key Differences from CorpusAI's Current Scope

HTML2TEI uses **Python dictionary rule files** (not YAML selectors), which have a richer structure:

- `BLOCK_RULES` — identifies article body blocks
- `BIGRAM_RULES` — handles language/block transitions
- `CONTENT_TREE` — maps HTML hierarchy to TEI tree nodes

CorpusAI's current CSS/XPath selector schema (`{"selector": "...", "type": "css"}`) would need to be extended to generate this richer format.

Additionally, HTML2TEI's `inventory-maker` performs structural analysis beyond simple CSS/XPath matching, involving layout pattern detection that would require a more sophisticated LLM prompt.

---

## Practical Integration Approach (Future Work)

A future integration could follow this pattern:

```
1. Input: a set of WARC records from a new or updated portal
2. CorpusAI: extract_html_samples() → preprocess_html() → OllamaRuleGenerator.generate_rules()
3. Human reviews + approves proposed candidates
4. Human translates approved CSS selectors → HTML2TEI BLOCK_RULES format
5. HTML2TEI: runs deterministic extraction using the translated rules
```

Step 4 (translation) could eventually be automated by an additional CorpusAI module that maps CSS selectors to HTML2TEI's Python dictionary format, but this is explicitly **out of scope for Phase 4**.

---

## Scope Decision

> **No HTML2TEI code is implemented in CorpusAI.**

CorpusAI's Phase 4 scope is limited to:
- Demonstrating compatibility with the WARC format produced by WebArticleCurator
- Reading, splitting, and preprocessing WARC-archived HTML
- Running the selector generation pipeline on WARC-sourced HTML

HTML2TEI integration is explicitly deferred to future research phases. The architectural decision to keep CorpusAI's extraction layer deterministic and separate from AI components ensures it can be extended toward HTML2TEI format targets without redesign.

---

## References

- [WebArticleCurator (ELTE-DH)](https://github.com/ELTE-DH/WebArticleCurator)
- [HTML2TEI (ELTE-DH)](https://github.com/ELTE-DH/HTML2TEI)
- [TEI Guidelines](https://tei-c.org/guidelines/)
- CorpusAI `src/warc_utils.py` — WARC reading utilities
- CorpusAI `src/html_preprocessor.py` — HTML cleaning for LLM inspection
- CorpusAI `src/rule_generator.py` — OllamaRuleGenerator

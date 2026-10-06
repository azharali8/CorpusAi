# Third-Party Notices & Attributions

CorpusAI interacts with or depends on several third-party libraries, container images, and tools. Each component remains governed by its respective license.

---

## External Tools & Containerized Runtimes

### 1. Webrecorder Browsertrix Crawler
- **Project:** [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler) (Webrecorder project)
- **Role:** Browser-backed web archival capture, WACZ generation, and automated replay QA workflow.
- **License:** [GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)](https://github.com/webrecorder/browsertrix-crawler/blob/main/LICENSE)
- **Relationship:** CorpusAI invokes Browsertrix Crawler as an external, containerized command-line tool via Docker. CorpusAI does not include modified Browsertrix source code or redistribute Browsertrix binaries as CorpusAI code. Browsertrix remains subject to its own license.

### 2. WebArticleCurator (Reference Crawler Architecture)
- **Role:** Reference crawling architecture informing multi-level traversal and unstable pagination benchmarks.
- **Relationship:** Clean-room reference integration via external CLI/WARC adapter interfaces.

---

## Python Libraries (Runtime & Testing)

| Package | License | Primary Role |
| :--- | :--- | :--- |
| **beautifulsoup4** | MIT | HTML parsing and structural DOM navigation |
| **requests** | Apache-2.0 | Polite HTTP request fetching and REST API interaction |
| **warcio** | Apache-2.0 | ISO 28500 WARC/WARC.GZ reading, writing, and iteration |
| **lxml** | BSD-3-Clause | Fast XML/HTML tree processing |
| **pytest** | MIT | Unit and regression testing framework |
| **PyYAML** | MIT | YAML configuration and schema parsing |

---

## Archived Web Materials & Research Artifacts Disclaimer

- Captured web resources, including HTML documents, JSON API responses, WARC/WACZ files, images, CSS, JavaScript bundles, fonts, and metadata stored under `results/` or referenced in experiments, remain the intellectual property and copyright of their respective original publishers and copyright holders.
- CorpusAI's Apache 2.0 license applies **strictly** to the original CorpusAI source code and documentation created for this research project. It does **not** grant redistribution, sublicensing, or ownership rights for third-party web content collected during experimental runs.

"""
Unit tests for ArchivePageDiscovery module and evaluation harness.

Tests run offline using local HTML fixture slices and mock structures:
1. article links detected from semantic containers;
2. category links rejected;
3. navigation links rejected;
4. external links rejected;
5. duplicates removed;
6. archive ordering preserved;
7. ground-truth file is not referenced by discovery module;
8. evaluator compares sets correctly;
9. order mismatch detected separately from set mismatch.
"""

import inspect
import os
import pytest
import sys

from src.archive_page_discovery import ArchivePageDiscovery
from research.real_world.wordpress.run_discovery_validation import evaluate_discovery


SAMPLE_ARCHIVE_HTML = """
<!DOCTYPE html>
<html>
<head><title>News Archive</title></head>
<body>
  <nav>
    <a href="/about/">About</a>
    <a href="/plugins/">Plugins</a>
    <a href="https://external.org/partner">External Partner</a>
  </nav>
  <main>
    <div class="category-links">
      <a href="/news/category/releases/">Releases Category</a>
      <a href="/news/tag/security/">Security Tag</a>
      <a href="/news/author/matt/">Author Matt</a>
    </div>

    <article class="wp-block-post">
      <h2 class="wp-block-post-title"><a href="/news/2026/09/post-alpha/">Post Alpha</a></h2>
      <a href="/news/2026/09/post-alpha/">Read More</a>
    </article>

    <article class="wp-block-post">
      <h2 class="wp-block-post-title"><a href="/news/2026/09/post-beta/">Post Beta</a></h2>
    </article>

    <article class="wp-block-post">
      <h2 class="wp-block-post-title"><a href="/news/2026/08/post-gamma/">Post Gamma</a></h2>
    </article>

    <div class="pagination">
      <a href="/news/page/2/">Page 2</a>
    </div>
  </main>
</body>
</html>
"""


# 1. Article links detected
def test_article_links_detected():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    assert len(res["discovered_article_urls"]) == 3
    assert "https://wordpress.org/news/2026/09/post-alpha" in res["discovered_article_urls"]
    assert "https://wordpress.org/news/2026/09/post-beta" in res["discovered_article_urls"]
    assert "https://wordpress.org/news/2026/08/post-gamma" in res["discovered_article_urls"]


# 2. Category links rejected
def test_category_links_rejected():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    urls = res["discovered_article_urls"]
    assert not any("/category/" in u for u in urls)
    assert not any("/tag/" in u for u in urls)


# 3. Navigation links rejected
def test_navigation_links_rejected():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    urls = res["discovered_article_urls"]
    assert not any("/about" in u for u in urls)
    assert not any("/plugins" in u for u in urls)
    assert not any("/page/" in u for u in urls)


# 4. External links rejected
def test_external_links_rejected():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    urls = res["discovered_article_urls"]
    assert not any("external.org" in u for u in urls)


# 5. Duplicates removed
def test_duplicates_removed():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    # Post alpha has two links in HTML (title + read more)
    assert res["discovered_article_urls"].count("https://wordpress.org/news/2026/09/post-alpha") == 1
    assert res["duplicates_removed"] >= 1


# 6. Archive ordering preserved
def test_archive_ordering_preserved():
    discovery = ArchivePageDiscovery(base_url="https://wordpress.org/news/all-posts/")
    res = discovery.discover_article_urls(SAMPLE_ARCHIVE_HTML)
    expected = [
        "https://wordpress.org/news/2026/09/post-alpha",
        "https://wordpress.org/news/2026/09/post-beta",
        "https://wordpress.org/news/2026/08/post-gamma",
    ]
    assert res["discovered_article_urls"] == expected


# 7. Ground-truth file is not referenced by discovery module
def test_ground_truth_not_referenced_in_discovery():
    source = inspect.getsource(ArchivePageDiscovery)
    assert "manual_ground_truth" not in source
    assert "ground_truth" not in source.lower()
    assert "wordpress-7-1-2-release" not in source


# 8. Evaluator compares sets correctly
def test_evaluator_compares_sets_correctly(tmp_path):
    gt_file = tmp_path / "test_gt.txt"
    gt_file.write_text("https://example.org/news/a\nhttps://example.org/news/b\n")

    disc = ["https://example.org/news/a", "https://example.org/news/b"]
    comp = evaluate_discovery(disc, str(gt_file))

    assert comp["precision"] == 1.0
    assert comp["recall"] == 1.0
    assert comp["exact_set_matches"] == 2
    assert comp["same_order"] is True


# 9. Order mismatch detected separately from set mismatch
def test_evaluator_detects_order_mismatch(tmp_path):
    gt_file = tmp_path / "test_gt.txt"
    gt_file.write_text("https://example.org/news/a\nhttps://example.org/news/b\n")

    # Reversed order
    disc = ["https://example.org/news/b", "https://example.org/news/a"]
    comp = evaluate_discovery(disc, str(gt_file))

    assert comp["precision"] == 1.0
    assert comp["recall"] == 1.0
    assert comp["exact_set_matches"] == 2
    assert comp["same_order"] is False  # Order mismatch detected!

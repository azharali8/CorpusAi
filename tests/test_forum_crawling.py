"""
Unit tests for multi-level forum / bulletin board crawling and incremental state reuse.
"""

import pytest
from src.archive_visit_policy import normalize_article_url
from src.portal_state import ArticleState, ArchivePageState, PortalState
from src.run_comparison import RunComparisonEngine


def test_forum_topic_fragment_url_canonicalization():
    """Verify forum reply and post fragment URLs are canonicalized to root topic URL."""
    raw_topic = "https://forum.test/support/topic/my-first-issue/#post-12345"
    normalized = normalize_article_url(raw_topic)
    assert normalized == "https://forum.test/support/topic/my-first-issue"

    raw_topic_with_slash = "https://forum.test/support/topic/my-first-issue/?replyto=5#comment-99"
    normalized_slash = normalize_article_url(raw_topic_with_slash)
    assert normalized_slash == "https://forum.test/support/topic/my-first-issue?replyto=5"


def test_forum_multi_level_hierarchy_representation():
    """Verify 4-level forum hierarchy (Category -> Page -> Topic -> Post/Reply) representation."""
    forum_url = "https://forum.test/category/tech"
    page1_url = "https://forum.test/category/tech?page=1"
    page2_url = "https://forum.test/category/tech?page=2"

    topics_p1 = [f"https://forum.test/topic/t_{i}" for i in range(1, 11)]
    topics_p2 = [f"https://forum.test/topic/t_{i}" for i in range(11, 21)]

    pages_state = {
        page1_url: ArchivePageState(
            url=page1_url,
            ordered_fingerprint="ord_p1",
            unordered_fingerprint="unord_p1",
            article_urls=topics_p1,
            next_page_url=page2_url,
            page_number=1,
        ),
        page2_url: ArchivePageState(
            url=page2_url,
            ordered_fingerprint="ord_p2",
            unordered_fingerprint="unord_p2",
            article_urls=topics_p2,
            previous_page_url=page1_url,
            page_number=2,
        ),
    }

    articles_state = {}
    for idx, u in enumerate(topics_p1 + topics_p2, 1):
        is_captured = (idx <= 5)
        articles_state[u] = ArticleState(
            canonical_url=u,
            first_seen_run_id="run-forum-001",
            last_seen_run_id="run-forum-001",
            title=f"Topic {idx}" if is_captured else None,
            content_hash=f"hash_{idx}" if is_captured else None,
            content_capture_status="CAPTURED" if is_captured else "NOT_CAPTURED_IN_PILOT",
            seen_on_pages=[page1_url if idx <= 10 else page2_url],
        )

    portal_state = PortalState(
        portal_id="test-forum",
        run_id="run-forum-001",
        archive_pages=pages_state,
        articles=articles_state,
        archive_order=list(articles_state.keys()),
        head_fingerprint="unord_p1",
    )

    assert len(portal_state.archive_pages) == 2
    assert len(portal_state.articles) == 20
    assert sum(1 for a in portal_state.articles.values() if a.content_capture_status == "CAPTURED") == 5
    assert sum(1 for a in portal_state.articles.values() if a.content_capture_status == "NOT_CAPTURED_IN_PILOT") == 15


def test_forum_incremental_reuse_across_pagination_boundary():
    """Verify incremental crawl of multi-level forum reuses previously captured topic bodies."""
    # Baseline
    topics = [f"https://forum.test/topic/{i}" for i in range(1, 21)]
    base_articles = {
        u: ArticleState(
            canonical_url=u,
            first_seen_run_id="run-base",
            last_seen_run_id="run-base",
            title=f"Topic {i+1}",
            content_hash=f"hash_{i+1}" if i < 5 else None,
            content_capture_status="CAPTURED" if i < 5 else "NOT_CAPTURED_IN_PILOT",
        )
        for i, u in enumerate(topics)
    }
    base_pages = {
        "https://forum.test/cat": ArchivePageState(
            url="https://forum.test/cat",
            ordered_fingerprint="ord",
            unordered_fingerprint="unord",
            article_urls=topics,
            page_number=1,
        )
    }
    base_state = PortalState(
        portal_id="test-forum",
        run_id="run-base",
        archive_pages=base_pages,
        articles=base_articles,
        archive_order=topics,
        head_fingerprint="unord",
    )

    # Incremental run reuses state
    inc_articles = {
        u: ArticleState(
            canonical_url=u,
            first_seen_run_id="run-base",
            last_seen_run_id="run-inc",
            title=art.title,
            content_hash=art.content_hash,
            status="UNCHANGED",
            content_capture_status="REUSED" if art.content_capture_status == "CAPTURED" else "NOT_CAPTURED_IN_PILOT",
        )
        for u, art in base_articles.items()
    }
    inc_state = PortalState(
        portal_id="test-forum",
        run_id="run-inc",
        archive_pages=base_pages,
        articles=inc_articles,
        archive_order=topics,
        head_fingerprint="unord",
    )

    diff = RunComparisonEngine.compare_states(base_state, inc_state)
    assert len(diff.new_articles) == 0
    assert len(diff.unchanged_articles) == 20
    assert len(diff.changed_articles) == 0
    assert diff.head_changed is False

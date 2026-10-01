"""
Unit tests for HTML Preprocessor module.
"""

import pytest
from src.html_preprocessor import preprocess_html


def test_preprocessing_strips_scripts_and_styles():
    raw_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Article Title</title>
        <style>body { color: red; }</style>
        <script>console.log("secret tracker");</script>
    </head>
    <body>
        <!-- This is a layout comment -->
        <main class="content">
            <h1 class="headline">Important News</h1>
            <p>Article body text paragraph.</p>
        </main>
        <script src="bundle.js"></script>
    </body>
    </html>
    """
    cleaned = preprocess_html(raw_html)

    assert "secret tracker" not in cleaned
    assert "color: red" not in cleaned
    assert "This is a layout comment" not in cleaned
    assert "bundle.js" not in cleaned

    # Essential structure and text preserved
    assert '<h1 class="headline">Important News</h1>' in cleaned
    assert "<p>Article body text paragraph.</p>" in cleaned
    assert 'class="content"' in cleaned


def test_preprocessing_preserves_semantic_attributes():
    raw_html = """
    <article id="art-99" class="main-post" onclick="alert(1)" data-tracking="xyz">
        <time class="published" datetime="2024-05-01">May 1, 2024</time>
        <span class="author-name">Jane Doe</span>
    </article>
    """
    cleaned = preprocess_html(raw_html)

    assert 'id="art-99"' in cleaned
    assert 'class="main-post"' in cleaned
    assert 'datetime="2024-05-01"' in cleaned
    assert 'class="published"' in cleaned
    assert 'class="author-name"' in cleaned
    # Non-allowed attributes stripped
    assert "onclick" not in cleaned
    assert "data-tracking" not in cleaned


def test_preprocessing_empty_input():
    assert preprocess_html("") == ""
    assert preprocess_html("   ") == ""

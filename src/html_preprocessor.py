"""
HTML Preprocessor module for CorpusAI.

Prepares representative HTML documents for selector inference by stripping
non-structural elements (scripts, styles, comments, SVG) while strictly preserving
semantic tags, attributes (id, class), and content text.
"""

import re
from bs4 import BeautifulSoup, Comment


def preprocess_html(html_content: str) -> str:
    """
    Clean and minify an HTML document for LLM inspection without destroying structural fidelity.

    Removes:
    - <script>, <style>, <noscript>, <svg>, <canvas>, <iframe>
    - HTML comments
    - Extraneous multi-line whitespace

    Preserves:
    - All semantic containers (<header>, <article>, <section>, <main>, <nav>, etc.)
    - Core structural tags (<div>, <span>, <h1>-<h6>, <p>, <a>, <time>, etc.)
    - Essential attributes (class, id, datetime, href)
    - Meaningful text content

    Args:
        html_content: Raw HTML document string.

    Returns:
        Cleaned, compact HTML string.
    """
    if not html_content or not html_content.strip():
        return ""

    soup = BeautifulSoup(html_content, "lxml")

    # 1. Remove comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 2. Remove non-structural tags
    removable_tags = ["script", "style", "noscript", "svg", "canvas", "iframe", "link", "meta"]
    for tag_name in removable_tags:
        for element in soup.find_all(tag_name):
            element.decompose()

    # 3. Clean up empty/unnecessary attributes but keep id, class, datetime, href
    allowed_attrs = {"id", "class", "datetime", "href", "name", "role", "itemprop", "itemtype"}
    for tag in soup.find_all(True):
        attrs_to_remove = [k for k in tag.attrs if k not in allowed_attrs]
        for attr in attrs_to_remove:
            del tag[attr]

    # Render prettified/cleaned HTML
    cleaned = str(soup)

    # Collapse excessive blank lines
    cleaned = re.sub(r"\n\s*\n", "\n", cleaned)
    return cleaned.strip()

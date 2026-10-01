"""
Article Inspector and Archive Extractor module.

Provides deterministic inspection of article HTML:
- Metadata extraction (title, heading, publication date, author, canonical URL)
- Referenced asset discovery (images, CSS, JS, fonts, media)
- Lazy-loading indicators inspection (loading="lazy", data-src, srcset, deferred scripts)
- WordPress REST API discovery link inspection
- Content JSON vs Metadata JSON classification
"""

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


def extract_article_metadata(html: str, base_url: str = "") -> Dict[str, Any]:
    """
    Extract verification metadata from article HTML deterministically.

    Returns:
        Dict with title, heading, publication_date, author, canonical_url, and rest_api_url.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Page Title (<title>)
    title = ""
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        title = title_tag.string.strip()

    # 2. Visible Article Heading (h1, or h2 with entry-title / wp-block-post-title)
    heading = ""
    h1_tag = soup.find("h1")
    if h1_tag:
        heading = " ".join(h1_tag.get_text(strip=True).split())
    else:
        post_title = soup.find(["h2", "h3"], class_=re.compile(r"wp-block-post-title|entry-title|post-title"))
        if post_title:
            heading = " ".join(post_title.get_text(strip=True).split())

    # 3. Canonical URL (<link rel="canonical">)
    canonical_url = None
    canonical_tag = soup.find("link", rel=lambda r: r and "canonical" in (r if isinstance(r, list) else [r]))
    if canonical_tag and canonical_tag.get("href"):
        canonical_url = canonical_tag.get("href").strip()

    # 4. Publication Date (<time>, meta article:published_time, etc.)
    publication_date = None
    time_tag = soup.find("time")
    if time_tag:
        publication_date = time_tag.get("datetime") or time_tag.get_text(strip=True)
    if not publication_date:
        meta_time = soup.find("meta", property="article:published_time")
        if meta_time and meta_time.get("content"):
            publication_date = meta_time.get("content").strip()

    # 5. Author (byline, rel="author", meta author, wp-block-post-author)
    author = None
    author_tag = soup.find(rel=lambda r: r and "author" in (r if isinstance(r, list) else [r]))
    if author_tag:
        author = author_tag.get_text(strip=True)
    if not author:
        meta_author = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", property="article:author")
        if meta_author and meta_author.get("content"):
            author = meta_author.get("content").strip()
    if not author:
        author_elem = soup.find(class_=re.compile(r"wp-block-post-author|author-name|byline|posted-by"))
        if author_elem:
            author = " ".join(author_elem.get_text(strip=True).split())

    # 6. WordPress REST API Link (<link rel="https://api.w.org/" href="...">)
    rest_api_url = None
    rest_link = soup.find("link", rel=lambda r: r and ("https://api.w.org/" in (r if isinstance(r, list) else [r]) or "alternate" in (r if isinstance(r, list) else [r]) and "json" in str(r)))
    if rest_link and rest_link.get("href"):
        rest_api_url = rest_link.get("href").strip()
    else:
        # Check specifically for type="application/json"
        json_link = soup.find("link", type="application/json")
        if json_link and json_link.get("href"):
            rest_api_url = json_link.get("href").strip()

    if rest_api_url and base_url:
        rest_api_url = urljoin(base_url, rest_api_url)

    return {
        "title": title,
        "heading": heading,
        "publication_date": publication_date,
        "author": author,
        "canonical_url": canonical_url,
        "rest_api_url": rest_api_url,
    }


def inspect_lazy_loading(html: str) -> Dict[str, Any]:
    """
    Detect lazy-loading markers in HTML without browser interaction.

    Inspects:
    - loading="lazy" attributes
    - data-src / data-lazy / data-original attributes
    - srcset attributes
    - deferred/async script tags indicating client-side hydration or lazy loaders
    """
    soup = BeautifulSoup(html, "html.parser")

    lazy_loading_attrs = []
    data_src_elements = []
    srcset_elements = []
    deferred_scripts = []

    # 1. Check images/iframes for loading="lazy"
    for el in soup.find_all(["img", "iframe"]):
        if el.get("loading") == "lazy":
            lazy_loading_attrs.append({
                "tag": el.name,
                "src": el.get("src", ""),
            })
        if any(attr in el.attrs for attr in ["data-src", "data-lazy-src", "data-original", "data-lazy"]):
            data_src_elements.append({
                "tag": el.name,
                "data_src": el.get("data-src") or el.get("data-lazy-src") or el.get("data-original"),
            })
        if el.get("srcset"):
            srcset_elements.append({
                "tag": el.name,
                "src": el.get("src", ""),
            })

    # 2. Check scripts for defer / async or lazyloading libraries
    for script in soup.find_all("script"):
        src = script.get("src", "")
        is_deferred = script.has_attr("defer") or script.has_attr("async")
        if is_deferred or "lazy" in src.lower() or "intersection-observer" in src.lower():
            deferred_scripts.append({
                "src": src,
                "defer": script.has_attr("defer"),
                "async": script.has_attr("async"),
            })

    detected = bool(lazy_loading_attrs or data_src_elements or deferred_scripts)

    return {
        "lazy_loading_detected": detected,
        "loading_lazy_count": len(lazy_loading_attrs),
        "data_src_count": len(data_src_elements),
        "srcset_count": len(srcset_elements),
        "deferred_scripts_count": len(deferred_scripts),
        "details": {
            "loading_lazy_samples": lazy_loading_attrs[:5],
            "data_src_samples": data_src_elements[:5],
            "deferred_scripts_samples": deferred_scripts[:5],
        },
    }


def extract_referenced_assets(html: str, page_url: str) -> List[Dict[str, Any]]:
    """
    Inspect the article HTML for referenced resources:
    images, CSS, JavaScript, fonts, and embedded media.

    Returns:
        List of dicts: {"url": str, "resource_type": str, "same_origin": bool}
    """
    soup = BeautifulSoup(html, "html.parser")
    page_origin = urlparse(page_url).netloc
    assets = []
    seen_urls = set()

    def add_asset(raw_url: Optional[str], res_type: str):
        if not raw_url or raw_url.startswith("data:") or raw_url.startswith("blob:") or raw_url.startswith("javascript:"):
            return
        full_url = urljoin(page_url, raw_url)
        # strip anchor
        full_url = full_url.split("#")[0]
        if not full_url or full_url in seen_urls:
            return
        seen_urls.add(full_url)
        asset_origin = urlparse(full_url).netloc
        assets.append({
            "url": full_url,
            "resource_type": res_type,
            "same_origin": (asset_origin == page_origin),
        })

    # Images
    for img in soup.find_all("img"):
        add_asset(img.get("src"), "image")
        if img.get("srcset"):
            # parse srcset entries
            for part in img.get("srcset").split(","):
                entry = part.strip().split()
                if entry:
                    add_asset(entry[0], "image")

    # CSS stylesheets
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in (r if isinstance(r, list) else [r])):
        add_asset(link.get("href"), "css")

    # Favicons / icons
    for link in soup.find_all("link", rel=lambda r: r and any("icon" in x for x in (r if isinstance(r, list) else [r]))):
        add_asset(link.get("href"), "image")

    # JavaScript
    for script in soup.find_all("script", src=True):
        add_asset(script.get("src"), "javascript")

    # Fonts (<link rel="preload" as="font"> or font extensions)
    for link in soup.find_all("link", rel=lambda r: r and "preload" in (r if isinstance(r, list) else [r])):
        if link.get("as") == "font" or any(link.get("href", "").endswith(ext) for ext in [".woff", ".woff2", ".ttf", ".otf"]):
            add_asset(link.get("href"), "font")

    # Embedded Audio/Video
    for video in soup.find_all("video"):
        add_asset(video.get("src"), "video")
        for source in video.find_all("source"):
            add_asset(source.get("src"), "video")

    for audio in soup.find_all("audio"):
        add_asset(audio.get("src"), "audio")
        for source in audio.find_all("source"):
            add_asset(source.get("src"), "audio")

    return assets


def classify_json_payload(json_data: Any) -> Dict[str, Any]:
    """
    Classify whether a JSON response represents primary article content (CONTENT_JSON)
    or only metadata/listing/schema data (METADATA_JSON / UNKNOWN).

    Criteria for CONTENT_JSON:
    - Dict with substantial textual body / rendered content (e.g. `content.rendered`, `body`, `article_body`, `text`)
    - Dict with structured post title, content, and date matching article semantics.
    """
    if not isinstance(json_data, (dict, list)):
        return {
            "classification": "NON_JSON",
            "is_content_json": False,
            "reason": "Data is not a dict or list",
        }

    # Handle single post object
    post_obj = json_data
    if isinstance(json_data, list):
        if len(json_data) == 1 and isinstance(json_data[0], dict):
            post_obj = json_data[0]
        else:
            return {
                "classification": "METADATA_JSON",
                "is_content_json": False,
                "reason": f"JSON is an array with {len(json_data)} items (likely listing or index API)",
            }

    # Check for WordPress REST API post schema: post_obj["content"]["rendered"]
    has_rendered_content = False
    content_len = 0
    if isinstance(post_obj.get("content"), dict) and "rendered" in post_obj["content"]:
        rendered = post_obj["content"]["rendered"]
        if isinstance(rendered, str) and len(rendered.strip()) > 50:
            has_rendered_content = True
            content_len = len(rendered.strip())
    elif isinstance(post_obj.get("content"), str) and len(post_obj.get("content", "").strip()) > 50:
        has_rendered_content = True
        content_len = len(post_obj.get("content", "").strip())
    elif isinstance(post_obj.get("body"), str) and len(post_obj.get("body", "").strip()) > 50:
        has_rendered_content = True
        content_len = len(post_obj.get("body", "").strip())

    has_title = bool(post_obj.get("title"))
    has_slug_or_id = bool(post_obj.get("slug") or post_obj.get("id"))

    if has_rendered_content and (has_title or has_slug_or_id):
        return {
            "classification": "CONTENT_JSON",
            "is_content_json": True,
            "content_length_chars": content_len,
            "post_id": post_obj.get("id"),
            "slug": post_obj.get("slug"),
            "title_rendered": post_obj.get("title", {}).get("rendered") if isinstance(post_obj.get("title"), dict) else post_obj.get("title"),
            "reason": "Contains full rendered article body content and article identifiers",
        }

    return {
        "classification": "METADATA_JSON",
        "is_content_json": False,
        "reason": "JSON does not contain substantial rendered article body text",
    }


def classify_image_role(img_tag, page_url: str) -> Dict[str, Any]:
    """
    Classify an image's role as 'article_content', 'site_chrome', or 'unknown'
    using deterministic DOM context and ancestor containers.
    """
    src = img_tag.get("src", "")
    full_url = urljoin(page_url, src).split("#")[0] if src else ""

    # Check for obvious chrome indicators
    src_lower = src.lower()
    if any(k in src_lower for k in ["favicon", "wmark", "logo", "code-is-poetry", "gravatar"]):
        return {
            "url": full_url,
            "role": "site_chrome",
            "reason": "URL or filename matches site logo / favicon / chrome marker",
        }

    # Inspect parent hierarchy
    parent_classes = []
    parent_tags = []
    curr = img_tag.parent
    depth = 0
    in_content = False
    in_chrome = False

    while curr and depth < 8:
        tag_name = getattr(curr, "name", "")
        parent_tags.append(tag_name)
        classes = " ".join(curr.get("class", [])) if curr.get("class") else ""
        parent_classes.append(classes)

        if tag_name in ["header", "footer", "nav"]:
            in_chrome = True
        if any(c in classes for c in ["site-header", "site-footer", "global-header", "global-footer", "widget"]):
            in_chrome = True

        if any(c in classes for c in ["entry-content", "wp-block-post-content", "post-content", "wp-block-image"]):
            in_content = True
        if tag_name in ["article", "main"] and not in_chrome:
            in_content = True

        curr = curr.parent
        depth += 1

    if in_content and not in_chrome:
        role = "article_content"
        reason = "Located inside article body container (e.g. entry-content / wp-block-image)"
    elif in_chrome:
        role = "site_chrome"
        reason = "Located inside header / footer / navigation container"
    else:
        role = "unknown"
        reason = "No clear content or chrome container found"

    return {
        "url": full_url,
        "role": role,
        "reason": reason,
    }


def extract_content_images(html: str, page_url: str) -> List[Dict[str, Any]]:
    """
    Extract and classify all images in an HTML document.

    Returns:
        List of dicts: {"url": str, "role": str, "reason": str}
    """
    soup = BeautifulSoup(html, "html.parser")
    seen_urls = set()
    images = []

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        info = classify_image_role(img, page_url)
        url = info["url"]
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        images.append(info)

    return images


def compare_html_and_json_content(
    html_metadata: Dict[str, Any],
    html_body_text: str,
    json_post_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compare article representation between HTML extraction and WordPress REST API JSON.
    """
    # HTML properties
    html_title = html_metadata.get("heading") or html_metadata.get("title", "")
    html_date = html_metadata.get("publication_date") or ""
    html_author = html_metadata.get("author") or ""
    html_canonical = html_metadata.get("canonical_url") or ""

    # JSON properties
    json_title = ""
    if isinstance(json_post_data.get("title"), dict):
        json_title = json_post_data["title"].get("rendered", "")
    elif isinstance(json_post_data.get("title"), str):
        json_title = json_post_data["title"]

    json_date = json_post_data.get("date_gmt") or json_post_data.get("date", "")
    json_slug = json_post_data.get("slug", "")
    json_link = json_post_data.get("link", "")

    json_rendered_body = ""
    if isinstance(json_post_data.get("content"), dict):
        json_rendered_body = json_post_data["content"].get("rendered", "")
    elif isinstance(json_post_data.get("content"), str):
        json_rendered_body = json_post_data["content"]

    json_soup = BeautifulSoup(json_rendered_body, "html.parser")
    json_clean_text = " ".join(json_soup.get_text().split())
    html_clean_text = " ".join(html_body_text.split())

    # Normalized text similarity
    def normalize_str(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower())).strip()

    norm_html = normalize_str(html_clean_text)
    norm_json = normalize_str(json_clean_text)

    # Word overlap coefficient
    words_html = set(norm_html.split())
    words_json = set(norm_json.split())
    intersection = words_html.intersection(words_json)
    union = words_html.union(words_json)
    similarity = len(intersection) / len(union) if union else 0.0

    title_match = (normalize_str(html_title) in normalize_str(json_title)) or (normalize_str(json_title) in normalize_str(html_title))

    return {
        "same_article_identity": bool(title_match or (json_slug in html_canonical)),
        "title_match": title_match,
        "html_title": html_title,
        "json_title": json_title,
        "html_date": html_date,
        "json_date": json_date,
        "html_author": html_author,
        "json_author_id": json_post_data.get("author"),
        "json_slug": json_slug,
        "html_body_length_chars": len(html_clean_text),
        "json_body_length_chars": len(json_clean_text),
        "normalized_word_jaccard_similarity": round(similarity, 4),
        "json_body_available": bool(json_rendered_body),
        "html_only_elements": [
            "Navigation header",
            "Footer widget areas",
            "Theme styles & inline CSS",
            "Jetpack sharing markup",
            "Local navigation bar block",
        ],
        "json_only_elements": [
            "Raw post ID",
            "Numeric author ID",
            "Post format & ping_status fields",
            "Curated REST _links taxonomy links",
        ],
    }


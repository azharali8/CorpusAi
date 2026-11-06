"""
Browser discovery, network isolation, and screenshot comparison utilities.

Provides:
- find_portable_browser: Discover local Chromium or Edge executables across platforms.
- get_browser_isolation_flags: Command-line flags enforcing offline browser network isolation.
- compare_screenshots: Quantitative visual diffing metrics using Pillow (optional dependency).
"""

import os
import shutil
from typing import Any, Dict, List, Optional


def find_portable_browser(custom_path: Optional[str] = None) -> str:
    """
    Locate a portable Chromium-family or compatible browser executable.

    Priority order:
    1. custom_path argument (if provided and exists).
    2. CORPUSAI_BROWSER_PATH environment variable (if set and exists).
    3. Standard PATH executables via shutil.which ('chromium', 'google-chrome', 'chrome', 'msedge').
    4. Common OS-specific installation paths (Windows Program Files / LocalAppData, Linux /usr/bin, macOS).

    Raises RuntimeError if no suitable browser binary can be found.
    """
    # 1. Explicit argument
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path)

    # 2. Environment variable
    env_browser = os.environ.get("CORPUSAI_BROWSER_PATH", "").strip()
    if env_browser and os.path.isfile(env_browser):
        return os.path.abspath(env_browser)

    # 3. Executable names in system PATH
    candidate_names = [
        "chromium",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "msedge",
    ]
    for name in candidate_names:
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return os.path.abspath(which_path)

    # 4. Standard platform installation locations
    candidate_locations = [
        # Windows Chrome & Edge standard paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        # Linux standard paths
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        # macOS standard paths
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]

    for loc in candidate_locations:
        if loc and os.path.isfile(loc):
            return os.path.abspath(loc)

    raise RuntimeError(
        "No Chromium-compatible browser binary found. "
        "Please set the CORPUSAI_BROWSER_PATH environment variable to a valid chrome or msedge executable."
    )


def get_browser_isolation_flags(
    allowed_host: str = "127.0.0.1",
    allowed_port: Optional[int] = None,
) -> List[str]:
    """
    Return Chromium command-line flags that strictly isolate browser network access:
    - routes all external network requests to an unroutable blackhole address (127.0.0.1:9 discard port)
    - bypasses the proxy exclusively for local loopback addresses (127.0.0.1 / localhost)
    - maps external DNS lookups to a non-routable address to prevent external leakage.
    """
    bypass = "127.0.0.1;localhost"
    if allowed_host and allowed_host not in ("127.0.0.1", "localhost"):
        bypass = f"{allowed_host};127.0.0.1;localhost"

    return [
        "--proxy-server=http://127.0.0.1:9",
        f"--proxy-bypass-list={bypass}",
        "--host-rules=MAP * 127.0.0.1:9, EXCLUDE 127.0.0.1, EXCLUDE localhost",
    ]


def compare_screenshots(
    image1_path: str,
    image2_path: str,
    diff_output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compare two screenshots using Pillow (PIL) and return quantitative similarity metrics.

    Metrics returned:
    - match_ratio: float between 0.0 (completely different) and 1.0 (identical)
    - total_pixels: int total number of pixels evaluated
    - diff_pixel_count: int count of pixels that differ
    - dimensions_match: bool whether images have the same (width, height)
    - image1_dimensions: Tuple[int, int]
    - image2_dimensions: Tuple[int, int]
    - diff_bounding_box: Optional[Tuple[int, int, int, int]] (left, upper, right, lower)
    """
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise ImportError("Pillow is required for screenshot comparison. Run: pip install Pillow") from e

    if not os.path.isfile(image1_path):
        raise FileNotFoundError(f"Screenshot image1 not found: {image1_path}")
    if not os.path.isfile(image2_path):
        raise FileNotFoundError(f"Screenshot image2 not found: {image2_path}")

    with Image.open(image1_path) as im1, Image.open(image2_path) as im2:
        img1 = im1.convert("RGB")
        img2 = im2.convert("RGB")

    dim1 = img1.size
    dim2 = img2.size
    dimensions_match = (dim1 == dim2)

    # If dimensions do not match, normalize or evaluate intersection / canvas
    if not dimensions_match:
        # Resize or pad into common canvas for comparison, or evaluate common overlap
        common_w = max(dim1[0], dim2[0])
        common_h = max(dim1[1], dim2[1])
        base1 = Image.new("RGB", (common_w, common_h), (255, 255, 255))
        base2 = Image.new("RGB", (common_w, common_h), (255, 255, 255))
        base1.paste(img1, (0, 0))
        base2.paste(img2, (0, 0))
        img1 = base1
        img2 = base2

    diff = ImageChops.difference(img1, img2)
    bbox = diff.getbbox()

    total_pixels = img1.size[0] * img1.size[1]

    if bbox is None:
        # Identical images
        diff_pixel_count = 0
        match_ratio = 1.0
    else:
        # Count non-zero pixels in grayscale diff
        diff_gray = diff.convert("L")
        # diff_gray pixels > 0 are differences; use get_flattened_data if available (Pillow >= 10.1) else getdata
        if hasattr(diff_gray, "get_flattened_data"):
            diff_data = diff_gray.get_flattened_data()
        else:
            diff_data = diff_gray.getdata()
        diff_pixel_count = sum(1 for p in diff_data if p > 0)
        match_ratio = max(0.0, 1.0 - (diff_pixel_count / float(total_pixels)))

        if diff_output_path:
            os.makedirs(os.path.dirname(os.path.abspath(diff_output_path)), exist_ok=True)
            diff.save(diff_output_path)

    return {
        "match_ratio": round(match_ratio, 6),
        "total_pixels": total_pixels,
        "diff_pixel_count": diff_pixel_count,
        "dimensions_match": dimensions_match,
        "image1_dimensions": dim1,
        "image2_dimensions": dim2,
        "diff_bounding_box": bbox,
    }

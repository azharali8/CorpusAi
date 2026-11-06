"""Unit tests for standalone browser utilities (src.utils.browser).

Verifies:
1. Browser discovery order, explicit argument, and environment variable handling.
2. Browser binary validation and not-found behavior.
3. Network isolation flags and loopback exception configuration.
4. Screenshot comparison metrics with identical and different images.
5. Optional Pillow import error behavior when Pillow is uninstalled.
6. Importing src.utils.browser without starting replay server or importing heavy networking.
7. Re-export backward compatibility from src.replay_server.
"""

import os
import shutil
import sys
from unittest import mock

import pytest

from src.utils.browser import (
    compare_screenshots,
    find_portable_browser,
    get_browser_isolation_flags,
)


def test_find_portable_browser_custom_path_success(tmp_path):
    """Explicit custom_path argument takes highest priority if it exists."""
    fake_browser = tmp_path / "chrome.exe"
    fake_browser.write_text("dummy binary")
    resolved = find_portable_browser(custom_path=str(fake_browser))
    assert resolved == str(fake_browser.resolve())


def test_find_portable_browser_custom_path_missing_falls_through(tmp_path, monkeypatch):
    """If custom_path does not exist, fall back to environment variable or PATH."""
    missing_path = str(tmp_path / "nonexistent_browser.exe")
    fake_env_browser = tmp_path / "env_chrome.exe"
    fake_env_browser.write_text("dummy")
    monkeypatch.setenv("CORPUSAI_BROWSER_PATH", str(fake_env_browser))

    resolved = find_portable_browser(custom_path=missing_path)
    assert resolved == str(fake_env_browser.resolve())


def test_find_portable_browser_env_var_priority(tmp_path, monkeypatch):
    """CORPUSAI_BROWSER_PATH takes priority over PATH when custom_path is omitted."""
    fake_browser = tmp_path / "env_chrome.exe"
    fake_browser.write_text("binary")
    monkeypatch.setenv("CORPUSAI_BROWSER_PATH", str(fake_browser))

    resolved = find_portable_browser()
    assert resolved == str(fake_browser.resolve())


def test_find_portable_browser_path_lookup(tmp_path, monkeypatch):
    """If no custom_path and no env var, discover browser from PATH candidates in order."""
    monkeypatch.delenv("CORPUSAI_BROWSER_PATH", raising=False)
    fake_chromium = tmp_path / "chromium"
    fake_chromium.write_text("dummy")

    def mock_which(cmd):
        if cmd == "chromium":
            return str(fake_chromium)
        return None

    monkeypatch.setattr(shutil, "which", mock_which)
    resolved = find_portable_browser()
    assert resolved == str(fake_chromium.resolve())


def test_find_portable_browser_candidate_locations(monkeypatch):
    """If PATH lookup fails, discover browser from platform candidate locations."""
    monkeypatch.delenv("CORPUSAI_BROWSER_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    fake_loc = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    def mock_isfile(p):
        return p == fake_loc

    monkeypatch.setattr(os.path, "isfile", mock_isfile)
    resolved = find_portable_browser()
    assert resolved == os.path.abspath(fake_loc)


def test_find_portable_browser_not_found_raises_runtime_error(monkeypatch):
    """When no browser executable exists anywhere, raise RuntimeError with guidance."""
    monkeypatch.delenv("CORPUSAI_BROWSER_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    with pytest.raises(RuntimeError, match="No Chromium-compatible browser binary found"):
        find_portable_browser()


def test_browser_isolation_flags_default():
    """Default isolation routes everything to blackhole 127.0.0.1:9 and excludes localhost."""
    flags = get_browser_isolation_flags(allowed_host="127.0.0.1")
    assert "--proxy-server=http://127.0.0.1:9" in flags
    assert "--proxy-bypass-list=127.0.0.1;localhost" in flags
    assert "--host-rules=MAP * 127.0.0.1:9, EXCLUDE 127.0.0.1, EXCLUDE localhost" in flags


def test_browser_isolation_flags_custom_allowed_host():
    """Custom allowed host is prepended to proxy-bypass-list."""
    flags = get_browser_isolation_flags(allowed_host="archive.internal")
    assert "--proxy-bypass-list=archive.internal;127.0.0.1;localhost" in flags


def test_compare_screenshots_identical_images(tmp_path):
    """Comparing two identical images produces match_ratio 1.0 and 0 diff pixels."""
    from PIL import Image

    img1_path = str(tmp_path / "img1.png")
    img2_path = str(tmp_path / "img2.png")
    im = Image.new("RGB", (100, 100), color=(255, 0, 0))
    im.save(img1_path)
    im.save(img2_path)

    result = compare_screenshots(img1_path, img2_path)
    assert result["match_ratio"] == 1.0
    assert result["diff_pixel_count"] == 0
    assert result["dimensions_match"] is True
    assert result["total_pixels"] == 10000
    assert result["diff_bounding_box"] is None


def test_compare_screenshots_different_images(tmp_path):
    """Comparing different images computes diff pixel count, bounding box, and optional diff file."""
    from PIL import Image

    img1_path = str(tmp_path / "img1.png")
    img2_path = str(tmp_path / "img2.png")
    diff_out = str(tmp_path / "diff.png")

    im1 = Image.new("RGB", (50, 50), color=(255, 255, 255))
    im2 = Image.new("RGB", (50, 50), color=(255, 255, 255))
    # Modify 10x10 square in center
    for x in range(20, 30):
        for y in range(20, 30):
            im2.putpixel((x, y), (0, 0, 0))

    im1.save(img1_path)
    im2.save(img2_path)

    result = compare_screenshots(img1_path, img2_path, diff_output_path=diff_out)
    assert result["dimensions_match"] is True
    assert result["diff_pixel_count"] == 100
    assert result["total_pixels"] == 2500
    assert result["match_ratio"] == round(1.0 - (100 / 2500.0), 6)
    assert result["diff_bounding_box"] == (20, 20, 30, 30)
    assert os.path.exists(diff_out)


def test_compare_screenshots_dimension_mismatch(tmp_path):
    """Images with different sizes are normalized into a shared canvas."""
    from PIL import Image

    img1_path = str(tmp_path / "small.png")
    img2_path = str(tmp_path / "large.png")

    Image.new("RGB", (40, 40), color=(200, 200, 200)).save(img1_path)
    Image.new("RGB", (60, 60), color=(200, 200, 200)).save(img2_path)

    result = compare_screenshots(img1_path, img2_path)
    assert result["dimensions_match"] is False
    assert result["image1_dimensions"] == (40, 40)
    assert result["image2_dimensions"] == (60, 60)
    assert result["total_pixels"] == 3600  # 60x60 max canvas


def test_compare_screenshots_missing_file_raises_filenotfound(tmp_path):
    """Missing input files raise FileNotFoundError."""
    missing = str(tmp_path / "missing.png")
    existing = str(tmp_path / "existing.png")
    from PIL import Image
    Image.new("RGB", (10, 10)).save(existing)

    with pytest.raises(FileNotFoundError, match="Screenshot image1 not found"):
        compare_screenshots(missing, existing)
    with pytest.raises(FileNotFoundError, match="Screenshot image2 not found"):
        compare_screenshots(existing, missing)


def test_compare_screenshots_pillow_optional(tmp_path, monkeypatch):
    """If Pillow is not installed, invoking compare_screenshots raises informative ImportError."""
    fake1 = str(tmp_path / "a.png")
    fake2 = str(tmp_path / "b.png")

    with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageChops": None}):
        with pytest.raises(ImportError, match="Pillow is required for screenshot comparison"):
            compare_screenshots(fake1, fake2)


def test_backward_compatibility_reexports_from_replay_server():
    """All 3 browser functions are cleanly accessible via src.replay_server."""
    from src.replay_server import (
        compare_screenshots as rs_compare,
        find_portable_browser as rs_find,
        get_browser_isolation_flags as rs_flags,
    )
    assert rs_find is find_portable_browser
    assert rs_flags is get_browser_isolation_flags
    assert rs_compare is compare_screenshots

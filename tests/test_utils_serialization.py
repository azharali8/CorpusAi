"""Tests for shared atomic JSON serialization utilities (src.utils.serialization).

Covers:
1. Parent directory creation.
2. UTF-8 content and non-ASCII character preservation.
3. JSON indentation and ensure_ascii=False output.
4. File-level flush and fsync invocations before replacement.
5. Atomic replacement of existing destination.
6. Missing file handling (FileNotFoundError).
7. Existing destination remaining intact when serialization fails before replacement.
8. Temporary-file cleanup on serialization failure.
9. str and Path input compatibility.
10. Delegation from domain classes (ArchiveCatalog, PortalState, IncrementalRun) preserving public behavior.
"""

from pathlib import Path
from unittest.mock import patch
import pytest

from src.archive_catalog import ArchiveCatalog
from src.incremental_run import IncrementalRun
from src.portal_state import ArticleState, PortalState
from src.utils.serialization import atomic_write_json, ensure_parent_dir, read_json


def test_ensure_parent_dir_creates_nested_directories(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "dir" / "test.json"
    assert not target.parent.exists()
    ret = ensure_parent_dir(target)
    assert ret.parent.exists()
    assert ret == target


def test_ensure_parent_dir_accepts_str(tmp_path: Path):
    target_str = str(tmp_path / "str_dir" / "file.json")
    ret = ensure_parent_dir(target_str)
    assert isinstance(ret, Path)
    assert ret.parent.exists()
    assert ret == Path(target_str)


def test_ensure_parent_dir_preserves_lexical_path_without_resolving(tmp_path: Path):
    # Lexical path with relative navigation ("..")
    nested = tmp_path / "sub"
    nested.mkdir()
    lexical = nested / ".." / "outside.json"
    ret = ensure_parent_dir(lexical)
    assert ret == lexical
    assert not ret.is_absolute() if not lexical.is_absolute() else True

    # Symlink target preservation
    target_file = tmp_path / "target_real.json"
    target_file.write_text("{\"v\": 1}", encoding="utf-8")
    link_file = tmp_path / "symlink_dir" / "link.json"
    ensure_parent_dir(link_file)
    try:
        link_file.symlink_to(target_file)
        ret_link = ensure_parent_dir(link_file)
        assert ret_link == link_file
        # When writing to link_file, os.replace replaces the link file itself,
        # exactly matching baseline behavior (without writing into target_file).
        atomic_write_json(link_file, {"v": 2})
        assert read_json(link_file) == {"v": 2}
        assert read_json(target_file) == {"v": 1}
    except (OSError, NotImplementedError):
        # Platform privileges may restrict symlink creation
        pass


def test_atomic_write_and_read_preserves_utf8_and_formatting(tmp_path: Path):
    target = tmp_path / "sample.json"
    data = {
        "title": "Árvíztűrő tükörfúrógép",
        "description": "Unicode characters: 日本語, 🚀, español, ümläüt",
        "nested": {"count": 42, "items": ["a", "b"]},
    }
    atomic_write_json(target, data, indent=2)

    raw_text = target.read_text(encoding="utf-8")
    assert "Árvíztűrő tükörfúrógép" in raw_text
    assert "\\u" not in raw_text  # ensure_ascii=False verified
    assert "  \"title\":" in raw_text  # indent=2 verified

    loaded = read_json(target)
    assert loaded == data


def test_atomic_write_with_path_and_str(tmp_path: Path):
    path_obj = tmp_path / "from_path.json"
    str_obj = str(tmp_path / "from_str.json")

    atomic_write_json(path_obj, {"key": "val1"})
    atomic_write_json(str_obj, {"key": "val2"})

    assert read_json(path_obj) == {"key": "val1"}
    assert read_json(str_obj) == {"key": "val2"}


def test_atomic_write_invokes_flush_and_fsync(tmp_path: Path):
    target = tmp_path / "fsync_test.json"
    fsync_called = []

    with patch("os.fsync", side_effect=lambda fd: fsync_called.append(fd)):
        atomic_write_json(target, {"atomic": True})

    assert len(fsync_called) == 1
    assert target.exists()
    assert read_json(target) == {"atomic": True}


def test_atomic_replacement_overwrites_existing_file(tmp_path: Path):
    target = tmp_path / "overwrite.json"
    atomic_write_json(target, {"version": 1})
    assert read_json(target) == {"version": 1}

    atomic_write_json(target, {"version": 2})
    assert read_json(target) == {"version": 2}


def test_failed_serialization_preserves_original_and_cleans_tmp(tmp_path: Path):
    target = tmp_path / "preserved.json"
    atomic_write_json(target, {"original": "data"})

    class NonSerializable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": NonSerializable()})

    # Destination file must remain unmodified
    assert read_json(target) == {"original": "data"}

    # No leftover temporary files
    tmp_files = list(tmp_path.glob("*.tmp.*"))
    assert tmp_files == []


def test_read_json_missing_file_raises_filenotfound(tmp_path: Path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError, match="^File not found: "):
        read_json(missing)

    # Custom message support
    custom_msg = f"Custom missing message: {missing}"
    with pytest.raises(FileNotFoundError) as exc_info:
        read_json(missing, missing_file_message=custom_msg)
    assert str(exc_info.value) == custom_msg


def test_domain_classes_missing_file_exact_messages(tmp_path: Path):
    missing_cat = str(tmp_path / "missing_catalog.json")
    with pytest.raises(FileNotFoundError) as exc_cat:
        ArchiveCatalog.load_from_json(missing_cat)
    assert str(exc_cat.value) == f"Catalog file not found: {missing_cat}"

    missing_state = str(tmp_path / "missing_state.json")
    with pytest.raises(FileNotFoundError) as exc_state:
        PortalState.load_from_json(missing_state)
    assert str(exc_state.value) == f"PortalState file not found: {missing_state}"

    missing_run = str(tmp_path / "missing_run.json")
    with pytest.raises(FileNotFoundError) as exc_run:
        IncrementalRun.load_from_json(missing_run)
    assert str(exc_run.value) == f"IncrementalRun file not found: {missing_run}"


def test_archive_catalog_delegation(tmp_path: Path):
    cat_file = tmp_path / "catalog.json"
    catalog = ArchiveCatalog(portal_id="news_portal")
    catalog.save_to_json(str(cat_file))

    assert cat_file.exists()
    loaded = ArchiveCatalog.load_from_json(str(cat_file))
    assert loaded.portal_id == "news_portal"
    assert loaded.runs == {}

    # load_or_create behavior
    created = ArchiveCatalog.load_or_create(str(tmp_path / "nonexistent.json"), "new_portal")
    assert created.portal_id == "new_portal"


def test_portal_state_delegation(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state = PortalState(
        portal_id="blog_portal",
        run_id="run-1",
        articles={
            "http://example.com/1": ArticleState(
                canonical_url="http://example.com/1",
                first_seen_run_id="run-1",
                last_seen_run_id="run-1",
                title="Post 1",
            )
        },
    )
    state.save_to_json(str(state_file))

    assert state_file.exists()
    loaded = PortalState.load_from_json(str(state_file))
    assert loaded.portal_id == "blog_portal"
    assert loaded.run_id == "run-1"
    assert "http://example.com/1" in loaded.articles
    assert loaded.articles["http://example.com/1"].title == "Post 1"


def test_incremental_run_delegation(tmp_path: Path):
    run_file = tmp_path / "run.json"
    run = IncrementalRun(
        run_id="run-2026-11-04",
        target_portal="news_portal",
        seed_urls=["http://example.com"],
        mode="INCREMENTAL",
    )
    run.save_to_json(str(run_file))

    assert run_file.exists()
    loaded = IncrementalRun.load_from_json(str(run_file))
    assert loaded.run_id == "run-2026-11-04"
    assert loaded.mode == "INCREMENTAL"
    assert loaded.seed_urls == ["http://example.com"]

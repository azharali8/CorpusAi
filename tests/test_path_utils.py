"""
Unit tests for resolve_contained_path utility.

Tests containment guarantees against directory traversal, absolute paths,
Windows drive and UNC prefixes, null bytes, and external symlinks.
"""

from pathlib import Path
import pytest

from src.utils.paths import resolve_contained_path


def test_valid_normal_filename(tmp_path):
    target, key = resolve_contained_path(tmp_path, "manifest.json")
    assert key == "manifest.json"
    assert target == (tmp_path / "manifest.json").resolve()


def test_valid_nested_relative_path(tmp_path):
    target, key = resolve_contained_path(tmp_path, "metrics/summary.json")
    assert key == "metrics/summary.json"
    assert target == (tmp_path / "metrics" / "summary.json").resolve()


def test_backslash_normalized_to_forward_slash(tmp_path):
    target, key = resolve_contained_path(tmp_path, "replay\\screenshots\\shot.png")
    assert key == "replay/screenshots/shot.png"
    assert target == (tmp_path / "replay" / "screenshots" / "shot.png").resolve()


def test_dot_components_handled(tmp_path):
    target, key = resolve_contained_path(tmp_path, "./metrics/./summary.json")
    assert key == "metrics/summary.json"
    assert target == (tmp_path / "metrics" / "summary.json").resolve()


def test_parent_traversal_at_beginning_rejected(tmp_path):
    with pytest.raises(ValueError, match="Path traversal"):
        resolve_contained_path(tmp_path, "../outside.txt")


def test_parent_traversal_after_directory_rejected(tmp_path):
    with pytest.raises(ValueError, match="Path traversal"):
        resolve_contained_path(tmp_path, "folder/../../outside.txt")


def test_posix_absolute_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="Absolute"):
        resolve_contained_path(tmp_path, "/etc/passwd")


def test_windows_drive_absolute_rejected(tmp_path):
    with pytest.raises(ValueError, match="Absolute, drive-qualified"):
        resolve_contained_path(tmp_path, "C:\\Windows\\win.ini")
    with pytest.raises(ValueError, match="Absolute, drive-qualified"):
        resolve_contained_path(tmp_path, "C:/Windows/win.ini")


def test_windows_drive_relative_rejected(tmp_path):
    with pytest.raises(ValueError, match="Absolute, drive-qualified"):
        resolve_contained_path(tmp_path, "C:foo.txt")


def test_windows_root_relative_rejected(tmp_path):
    with pytest.raises(ValueError, match="Absolute, drive-qualified"):
        resolve_contained_path(tmp_path, "\\Windows\\System32")


def test_unc_path_rejected(tmp_path):
    with pytest.raises(ValueError, match="Absolute, drive-qualified"):
        resolve_contained_path(tmp_path, "\\\\server\\share\\file.txt")
    with pytest.raises(ValueError, match="Absolute"):
        resolve_contained_path(tmp_path, "//server/share/file.txt")


def test_empty_and_blank_paths_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty or blank"):
        resolve_contained_path(tmp_path, "")
    with pytest.raises(ValueError, match="empty or blank"):
        resolve_contained_path(tmp_path, "   ")
    with pytest.raises(ValueError, match="empty directory or root"):
        resolve_contained_path(tmp_path, ".")
    with pytest.raises(ValueError, match="empty directory or root"):
        resolve_contained_path(tmp_path, "./.")


def test_null_byte_rejected(tmp_path):
    with pytest.raises(ValueError, match="null byte"):
        resolve_contained_path(tmp_path, "foo\x00bar.txt")


def test_symlink_pointing_outside_base_rejected(tmp_path):
    """If a symlink inside base points to a file outside base, candidate resolution rejects it."""
    outside_dir = tmp_path / "outside_zone"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("secret_content", encoding="utf-8")

    inside_dir = tmp_path / "package_zone"
    inside_dir.mkdir()
    symlink_file = inside_dir / "link_to_secret.txt"

    try:
        symlink_file.symlink_to(secret_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in current environment/permissions")

    with pytest.raises(ValueError, match="escapes base directory"):
        resolve_contained_path(inside_dir, "link_to_secret.txt")


def test_symlink_pointing_to_base_dir_itself_rejected(tmp_path):
    """If a symlink inside base points back to base itself, resolution rejects it."""
    base_dir = tmp_path / "base_zone"
    base_dir.mkdir()
    symlink_to_base = base_dir / "loop_to_base"

    try:
        symlink_to_base.symlink_to(base_dir)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in current environment/permissions")

    with pytest.raises(ValueError, match="base directory itself"):
        resolve_contained_path(base_dir, "loop_to_base")


def test_filesystem_resolution_oserror_converted_to_valueerror(tmp_path, monkeypatch):
    """Base directory resolution failure (OSError) is converted to ValueError with cause."""
    def fail_resolve(self):
        raise OSError("Simulated disk error or unresolvable path")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    with pytest.raises(ValueError, match="Failed to resolve base directory") as exc_info:
        resolve_contained_path(tmp_path, "some_file.txt")

    assert isinstance(exc_info.value.__cause__, OSError)


def test_filesystem_resolution_runtimeerror_symlink_loop_converted(tmp_path, monkeypatch):
    """Base directory resolution failure (RuntimeError) is converted to ValueError with cause."""
    def fail_loop(self):
        raise RuntimeError("Symlink loop encountered")

    monkeypatch.setattr(Path, "resolve", fail_loop)

    with pytest.raises(ValueError, match="Failed to resolve base directory") as exc_info:
        resolve_contained_path(tmp_path, "some_file.txt")

    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_candidate_resolution_oserror_converted_to_valueerror(tmp_path, monkeypatch):
    """Candidate resolution failure (OSError) is converted to ValueError with cause."""
    orig_resolve = Path.resolve
    base_resolved = tmp_path.resolve()

    def fail_candidate_only(self):
        # Allow base resolution to succeed
        if str(self) == str(tmp_path) or str(self) == str(base_resolved):
            return orig_resolve(self)
        raise OSError("Simulated candidate filesystem I/O error")

    monkeypatch.setattr(Path, "resolve", fail_candidate_only)

    with pytest.raises(ValueError, match="Failed to resolve candidate path") as exc_info:
        resolve_contained_path(tmp_path, "unresolvable_candidate.txt")

    assert "unresolvable_candidate.txt" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)


def test_candidate_resolution_runtimeerror_symlink_loop_converted(tmp_path, monkeypatch):
    """Candidate resolution failure (RuntimeError) is converted to ValueError with cause."""
    orig_resolve = Path.resolve
    base_resolved = tmp_path.resolve()

    def fail_candidate_only(self):
        # Allow base resolution to succeed
        if str(self) == str(tmp_path) or str(self) == str(base_resolved):
            return orig_resolve(self)
        raise RuntimeError("Simulated recursive candidate symlink loop")

    monkeypatch.setattr(Path, "resolve", fail_candidate_only)

    with pytest.raises(ValueError, match="Failed to resolve candidate path") as exc_info:
        resolve_contained_path(tmp_path, "loop_candidate.txt")

    assert "loop_candidate.txt" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)

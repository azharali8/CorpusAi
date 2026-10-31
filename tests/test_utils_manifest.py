"""
Tests for src/utils/manifest.py — shared SHA-256 manifest generation.

Coverage:
- Output format: two-space separator, hex digest, forward slashes, trailing newline
- Sorting by relative-path label
- base_dir behaviour (path = join(base_dir, key) when given; value when not)
- Forward-slash normalisation for Windows backslash keys
- Digest matches sha256_file() output
- Missing-file propagates FileNotFoundError
- Empty dict produces a single trailing newline
- Backward-compat re-export from src.replay_validation
"""

import hashlib
import os
import tempfile

import pytest

from src.utils.manifest import generate_sha256_manifest
from src.utils.crypto import sha256_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tmp(content: bytes) -> str:
    """Write *content* to a NamedTemporaryFile and return its path."""
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tf.write(content)
    tf.close()
    return tf.name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGenerateSha256Manifest:

    def test_empty_dict(self):
        """Empty files_dict produces just a trailing newline."""
        result = generate_sha256_manifest({})
        assert result == "\n"

    def test_single_file_format(self):
        """Single entry: '<sha256>  <relpath>' with two spaces and trailing newline."""
        path = _write_tmp(b"hello world")
        try:
            result = generate_sha256_manifest({"file.txt": path})
        finally:
            os.unlink(path)

        expected_digest = hashlib.sha256(b"hello world").hexdigest()
        assert result == f"{expected_digest}  file.txt\n"

    def test_multiple_files_sorted(self):
        """Multiple entries are sorted by relative-path key, not insertion order."""
        paths = {k: _write_tmp(k.encode()) for k in ["zebra.txt", "alpha.txt", "mango.txt"]}
        try:
            result = generate_sha256_manifest(paths)
        finally:
            for p in paths.values():
                os.unlink(p)

        lines = result.rstrip("\n").split("\n")
        keys_in_output = [line.split("  ", 1)[1] for line in lines]
        assert keys_in_output == sorted(paths.keys())

    def test_digest_matches_sha256_file(self):
        """Digest in manifest matches sha256_file() for the same file."""
        content = b"CorpusAI manifest test " * 1000
        path = _write_tmp(content)
        try:
            result = generate_sha256_manifest({"data.bin": path})
            expected_digest = sha256_file(path)
        finally:
            os.unlink(path)

        manifest_digest = result.split("  ", 1)[0]
        assert manifest_digest == expected_digest

    def test_forward_slash_normalisation(self):
        """Backslashes in keys are normalised to forward slashes."""
        content = b"windows path test"
        path = _write_tmp(content)
        try:
            result = generate_sha256_manifest({"sub\\dir\\file.txt": path})
        finally:
            os.unlink(path)

        assert "sub/dir/file.txt" in result
        assert "\\" not in result

    def test_without_base_dir_uses_value(self):
        """When base_dir='', the dict value (the file path) is used directly."""
        content = b"value-path test"
        path = _write_tmp(content)
        try:
            result = generate_sha256_manifest({"rel_name.txt": path})
        finally:
            os.unlink(path)

        expected_digest = hashlib.sha256(b"value-path test").hexdigest()
        assert result == f"{expected_digest}  rel_name.txt\n"

    def test_with_base_dir_joins_key(self, tmp_path):
        """When base_dir is provided, path = join(base_dir, key); value is ignored."""
        content = b"base_dir test"
        filename = "artifact.txt"
        (tmp_path / filename).write_bytes(content)

        # Value is deliberately a wrong path — should not be used
        files_dict = {filename: "/nonexistent/should/not/be/used"}
        result = generate_sha256_manifest(files_dict, base_dir=str(tmp_path))

        expected_digest = hashlib.sha256(b"base_dir test").hexdigest()
        assert result == f"{expected_digest}  {filename}\n"

    def test_missing_file_raises_file_not_found(self):
        """Missing file propagates FileNotFoundError (no custom wrapper)."""
        with pytest.raises(FileNotFoundError):
            generate_sha256_manifest({"missing.txt": "/nonexistent/path/missing.txt"})

    def test_trailing_newline(self):
        """Output always ends with exactly one newline."""
        path = _write_tmp(b"trailing newline check")
        try:
            result = generate_sha256_manifest({"f.txt": path})
        finally:
            os.unlink(path)

        assert result.endswith("\n")
        assert not result.endswith("\n\n")

    def test_line_format_two_spaces(self):
        """Separator between digest and path is exactly two spaces."""
        path = _write_tmp(b"two space separator")
        try:
            result = generate_sha256_manifest({"check.txt": path})
        finally:
            os.unlink(path)

        line = result.strip()
        parts = line.split("  ", 1)
        assert len(parts) == 2
        digest, relpath = parts
        assert len(digest) == 64  # SHA-256 hex = 64 chars
        assert relpath == "check.txt"

    def test_multi_file_consistent_format(self, tmp_path):
        """All lines in a multi-file manifest follow the exact format."""
        files = {}
        for i in range(5):
            name = f"file_{i:02d}.bin"
            (tmp_path / name).write_bytes(f"content {i}".encode())
            files[name] = str(tmp_path / name)

        result = generate_sha256_manifest(files)
        lines = result.rstrip("\n").split("\n")
        assert len(lines) == 5
        for line in lines:
            parts = line.split("  ", 1)
            assert len(parts) == 2
            digest, _ = parts
            assert len(digest) == 64
            assert all(c in "0123456789abcdef" for c in digest)


class TestReplayValidationBackwardCompat:
    """generate_sha256_manifest must remain importable from src.replay_validation."""

    def test_importable_from_replay_validation(self):
        from src.replay_validation import generate_sha256_manifest as rv_fn  # noqa: F401
        assert callable(rv_fn)

    def test_same_output_as_utils_manifest(self, tmp_path):
        """Both re-exports produce identical output for the same input."""
        from src.replay_validation import generate_sha256_manifest as rv_fn
        content = b"compat test"
        (tmp_path / "compat.txt").write_bytes(content)
        files = {"compat.txt": str(tmp_path / "compat.txt")}

        result_utils = generate_sha256_manifest(files)
        result_rv = rv_fn(files)
        assert result_utils == result_rv

    def test_compute_file_sha256_importable_from_replay_validation(self):
        from src.replay_validation import compute_file_sha256  # noqa: F401
        assert callable(compute_file_sha256)

"""
Tests for shared cryptographic utilities (src/utils/crypto.py).
"""

import hashlib
import io
from unittest.mock import patch

import pytest

from src.utils.crypto import (
    _create_hasher,
    _hash_stream,
    compute_file_md5_and_sha256,
    md5_bytes,
    md5_file,
    sha256_bytes,
    sha256_file,
)


def test_sha256_bytes():
    data = b"hello world CorpusAI"
    expected = hashlib.sha256(data).hexdigest()
    assert sha256_bytes(data) == expected


def test_md5_bytes():
    data = b"hello world CorpusAI"
    expected = hashlib.md5(data).hexdigest()
    assert md5_bytes(data) == expected


def test_sha256_file_empty(tmp_path):
    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")
    expected = hashlib.sha256(b"").hexdigest()
    assert sha256_file(empty_file) == expected


def test_md5_file_empty(tmp_path):
    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")
    expected = hashlib.md5(b"").hexdigest()
    assert md5_file(empty_file) == expected


def test_sha256_and_md5_file_multi_chunk(tmp_path):
    # Create file larger than chunk_size (65536)
    large_file = tmp_path / "large.bin"
    payload = b"A" * 70000 + b"B" * 70000
    large_file.write_bytes(payload)

    exp_sha = hashlib.sha256(payload).hexdigest()
    exp_md5 = hashlib.md5(payload).hexdigest()

    assert sha256_file(large_file) == exp_sha
    assert md5_file(large_file) == exp_md5

    md5_res, sha_res, size_res = compute_file_md5_and_sha256(large_file)
    assert md5_res == exp_md5
    assert sha_res == exp_sha
    assert size_res == len(payload)


def test_custom_positive_chunk_size(tmp_path):
    f = tmp_path / "custom_chunk.txt"
    payload = b"CorpusAI custom chunk test string."
    f.write_bytes(payload)

    exp_sha = hashlib.sha256(payload).hexdigest()
    exp_md5 = hashlib.md5(payload).hexdigest()

    assert sha256_file(f, chunk_size=4) == exp_sha
    assert md5_file(f, chunk_size=7) == exp_md5
    m, s, sz = compute_file_md5_and_sha256(f, chunk_size=5)
    assert m == exp_md5
    assert s == exp_sha
    assert sz == len(payload)


def test_invalid_chunk_size_values(tmp_path):
    f = tmp_path / "valid.txt"
    f.write_bytes(b"data")

    for bad_val in (0, -1, -65536):
        with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
            sha256_file(f, chunk_size=bad_val)
        with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
            md5_file(f, chunk_size=bad_val)
        with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
            compute_file_md5_and_sha256(f, chunk_size=bad_val)

    for bad_type in ("65536", 12.5, None, True, False, []):
        with pytest.raises(TypeError, match="chunk_size must be an integer"):
            sha256_file(f, chunk_size=bad_type)
        with pytest.raises(TypeError, match="chunk_size must be an integer"):
            md5_file(f, chunk_size=bad_type)
        with pytest.raises(TypeError, match="chunk_size must be an integer"):
            compute_file_md5_and_sha256(f, chunk_size=bad_type)


def test_pathlib_path_and_string_path_equivalence(tmp_path):
    f_path = tmp_path / "pathlib_test.bin"
    f_path.write_bytes(b"pathlib compatibility test payload")

    # Path object
    sha_from_path = sha256_file(f_path)
    # String path
    sha_from_str = sha256_file(str(f_path))

    assert sha_from_path == sha_from_str


def test_file_not_found_raises():
    with pytest.raises(FileNotFoundError):
        sha256_file("non_existent_file_path_xyz.bin")

    with pytest.raises(FileNotFoundError):
        md5_file("non_existent_file_path_xyz.bin")

    with pytest.raises(FileNotFoundError):
        compute_file_md5_and_sha256("non_existent_file_path_xyz.bin")


def test_directory_supplied_raises(tmp_path):
    d = tmp_path / "sub_directory"
    d.mkdir()

    # Opening a directory in binary mode raises OSError (IsADirectoryError on POSIX, PermissionError on Windows)
    with pytest.raises(OSError):
        sha256_file(d)

    with pytest.raises(OSError):
        md5_file(d)

    with pytest.raises(OSError):
        compute_file_md5_and_sha256(d)


# --- Narrowly Scoped Batch 2B Characterization and Regression Tests ---


def test_known_test_vectors():
    """Verify digests against authoritative known test vectors (empty and RFC vectors)."""
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert md5_bytes(b"") == "d41d8cd98f00b204e9800998ecf8427e"

    abc = b"abc"
    assert sha256_bytes(abc) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert md5_bytes(abc) == "900150983cd24fb0d6963f7d28e17f72"


def test_file_and_bytes_digest_equivalence(tmp_path):
    """Confirm file hashing and byte hashing yield exact identical hexadecimal digests."""
    payload = b"Reproducible content verifying byte and file equivalence.\n" * 100
    test_file = tmp_path / "equivalence.bin"
    test_file.write_bytes(payload)

    assert sha256_file(test_file) == sha256_bytes(payload)
    assert md5_file(test_file) == md5_bytes(payload)

    m, s, total = compute_file_md5_and_sha256(test_file)
    assert m == md5_bytes(payload)
    assert s == sha256_bytes(payload)
    assert total == len(payload)


def test_empty_file_combined_hashing(tmp_path):
    """Confirm compute_file_md5_and_sha256 returns zero byte count and correct digests for empty files."""
    empty_file = tmp_path / "empty_combined.txt"
    empty_file.write_bytes(b"")

    m, s, sz = compute_file_md5_and_sha256(empty_file)
    assert m == "d41d8cd98f00b204e9800998ecf8427e"
    assert s == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sz == 0


def test_validation_precedes_file_opening():
    """Confirm chunk_size validation occurs before opening the file (so ValueError/TypeError precede FileNotFoundError)."""
    non_existent = "definitely_non_existent_file_path_12345.bin"

    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        sha256_file(non_existent, chunk_size=-1)

    with pytest.raises(TypeError, match="chunk_size must be an integer"):
        md5_file(non_existent, chunk_size="invalid")

    with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
        compute_file_md5_and_sha256(non_existent, chunk_size=0)


def test_single_file_opening_for_combined_hashing(tmp_path):
    """Confirm compute_file_md5_and_sha256 opens the file exactly once."""
    test_file = tmp_path / "single_open.bin"
    test_file.write_bytes(b"verify single open")

    open_calls = 0
    orig_open = open

    def tracking_open(*args, **kwargs):
        nonlocal open_calls
        if args and str(args[0]) == str(test_file):
            open_calls += 1
        return orig_open(*args, **kwargs)

    with patch("builtins.open", side_effect=tracking_open):
        m, s, sz = compute_file_md5_and_sha256(test_file)

    assert open_calls == 1
    assert sz == len(b"verify single open")
    assert m == md5_bytes(b"verify single open")
    assert s == sha256_bytes(b"verify single open")


def test_non_seekable_stream_behavior():
    """Confirm _hash_stream processes non-seekable streams in a single pass without seek()."""

    class NonSeekableStream:
        def __init__(self, data: bytes):
            self._buffer = io.BytesIO(data)

        def read(self, size=-1):
            return self._buffer.read(size)

        def seek(self, offset, whence=io.SEEK_SET):
            raise io.UnsupportedOperation("seek is not supported on this stream")

    payload = b"non-seekable stream payload across multiple chunks " * 50
    stream = NonSeekableStream(payload)

    digests, total = _hash_stream(stream, ["sha256", "md5"], chunk_size=32)
    assert digests[0] == sha256_bytes(payload)
    assert digests[1] == md5_bytes(payload)
    assert total == len(payload)


def test_stream_algorithm_ordering_and_empty():
    """Confirm requested algorithm ordering is preserved and empty sequences raise ValueError."""
    data = b"deterministic ordering test"
    stream1 = io.BytesIO(data)
    d1, _ = _hash_stream(stream1, ["sha256", "md5"], chunk_size=64)
    assert d1 == [sha256_bytes(data), md5_bytes(data)]

    stream2 = io.BytesIO(data)
    d2, _ = _hash_stream(stream2, ["md5", "sha256"], chunk_size=64)
    assert d2 == [md5_bytes(data), sha256_bytes(data)]

    stream3 = io.BytesIO(data)
    with pytest.raises(ValueError, match="At least one algorithm must be specified"):
        _hash_stream(stream3, [], chunk_size=64)


def test_create_hasher_unsupported_algorithm():
    """Confirm _create_hasher raises ValueError on unsupported algorithms."""
    with pytest.raises(ValueError, match="Unsupported hashing algorithm"):
        _create_hasher("sha3_unsupported_xyz")


def test_multithreaded_hashing_parity(tmp_path):
    """Verify multithreaded=True MtHasher pipeline produces exact digest and byte count parity."""
    payload = b"MtHasher multithreaded pipeline parity verification " * 1024
    test_file = tmp_path / "mt_test.bin"
    test_file.write_bytes(payload)

    seq_md5, seq_sha, seq_bytes = compute_file_md5_and_sha256(test_file, multithreaded=False)
    mt_md5, mt_sha, mt_bytes = compute_file_md5_and_sha256(test_file, multithreaded=True)

    assert mt_md5 == seq_md5 == md5_bytes(payload)
    assert mt_sha == seq_sha == sha256_bytes(payload)
    assert mt_bytes == seq_bytes == len(payload)

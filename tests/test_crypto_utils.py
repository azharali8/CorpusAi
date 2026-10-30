"""
Tests for shared cryptographic utilities (src/utils/crypto.py).
"""

import hashlib
import pytest

from src.utils.crypto import (
    sha256_bytes,
    md5_bytes,
    sha256_file,
    md5_file,
    compute_file_md5_and_sha256,
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

"""
Shared cryptographic hashing utilities for CorpusAI.

Provides canonical, streaming SHA-256 and MD5 operations with unified handling
for files and raw bytes to prevent redundant implementations and avoid memory
exhaustion on large archival packages or WARC records.
"""

import hashlib
import os
from typing import Tuple, Union


def sha256_bytes(data: bytes) -> str:
    """Compute the hexadecimal SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def md5_bytes(data: bytes) -> str:
    """Compute the hexadecimal MD5 digest of bytes."""
    return hashlib.md5(data).hexdigest()


def _validate_chunk_size(chunk_size: int) -> None:
    """Validate that chunk_size is a positive integer."""
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
        raise TypeError(f"chunk_size must be an integer, got {type(chunk_size).__name__}")
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size}")


def sha256_file(file_path: Union[str, os.PathLike], chunk_size: int = 65536) -> str:
    """
    Compute the hexadecimal SHA-256 digest of a file using streaming chunks.

    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    _validate_chunk_size(chunk_size)

    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def md5_file(file_path: Union[str, os.PathLike], chunk_size: int = 65536) -> str:
    """
    Compute the hexadecimal MD5 digest of a file using streaming chunks.

    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    _validate_chunk_size(chunk_size)

    h = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def compute_file_md5_and_sha256(
    file_path: Union[str, os.PathLike],
    chunk_size: int = 65536,
) -> Tuple[str, str, int]:
    """
    Compute MD5, SHA-256, and byte size of a file in a single pass without
    loading the entire file into RAM.

    Returns (md5_hex, sha256_hex, total_bytes).
    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    _validate_chunk_size(chunk_size)

    md5_hash = hashlib.md5()
    sha_hash = hashlib.sha256()
    size = 0
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            md5_hash.update(chunk)
            sha_hash.update(chunk)
            size += len(chunk)
    return md5_hash.hexdigest(), sha_hash.hexdigest(), size

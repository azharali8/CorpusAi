"""
Shared cryptographic hashing utilities for CorpusAI.

Provides canonical, streaming SHA-256 and MD5 operations with unified handling
for files and raw bytes to prevent redundant implementations and avoid memory
exhaustion on large archival packages or WARC records.
"""

import hashlib
import queue
import threading
from pathlib import Path
from typing import Sequence, Tuple, Union

DEFAULT_CHUNK_SIZE: int = 65536


def _create_hasher(algorithm: str):
    """Instantiate a hashlib hash object for a supported algorithm name."""
    algo_clean = algorithm.strip().lower()
    if algo_clean == "sha256":
        return hashlib.sha256()
    if algo_clean == "md5":
        return hashlib.md5()
    raise ValueError(f"Unsupported hashing algorithm: {algorithm!r}")


def _validate_chunk_size(chunk_size: int) -> None:
    """Validate that chunk_size is a positive integer."""
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
        raise TypeError(f"chunk_size must be an integer, got {type(chunk_size).__name__}")
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size}")


def _hash_stream(
    stream,
    algorithms: Union[str, Sequence[str]],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Tuple[list[str], int]:
    """
    Process an open binary stream in chunks, updating hash states in a single pass.

    Returns: (list_of_hexdigests, total_bytes_read)
    """
    _validate_chunk_size(chunk_size)
    if isinstance(algorithms, str):
        algo_list = [algorithms]
    else:
        algo_list = list(algorithms)

    if not algo_list:
        raise ValueError("At least one algorithm must be specified")

    hashers = [_create_hasher(algo) for algo in algo_list]
    total_bytes = 0

    while chunk := stream.read(chunk_size):
        for h in hashers:
            h.update(chunk)
        total_bytes += len(chunk)

    return [h.hexdigest() for h in hashers], total_bytes


def _hash_file(
    file_path: Union[str, Path],
    algorithms: Union[str, Sequence[str]],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Tuple[list[str], int]:
    """
    Validate chunk_size, open file, and compute digests and byte count in a single pass.
    """
    _validate_chunk_size(chunk_size)
    with open(file_path, "rb") as fh:
        return _hash_stream(fh, algorithms, chunk_size)


def hash_bytes(data: bytes, algorithm: str) -> str:
    """Compute the hexadecimal digest of bytes for a supported algorithm."""
    h = _create_hasher(algorithm)
    h.update(data)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Compute the hexadecimal SHA-256 digest of bytes."""
    return hash_bytes(data, "sha256")


def md5_bytes(data: bytes) -> str:
    """Compute the hexadecimal MD5 digest of bytes."""
    return hash_bytes(data, "md5")


def sha256_file(file_path: Union[str, Path], chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """
    Compute the hexadecimal SHA-256 digest of a file using streaming chunks.

    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    digests, _ = _hash_file(file_path, ["sha256"], chunk_size=chunk_size)
    return digests[0]


def md5_file(file_path: Union[str, Path], chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """
    Compute the hexadecimal MD5 digest of a file using streaming chunks.

    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    digests, _ = _hash_file(file_path, ["md5"], chunk_size=chunk_size)
    return digests[0]


def compute_file_md5_and_sha256(
    file_path: Union[str, Path],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    multithreaded: bool = False,
) -> Tuple[str, str, int]:
    """
    Compute MD5, SHA-256, and byte size of a file in a single pass without
    loading the entire file into RAM.

    When multithreaded=True (MtHasher pattern), digest updates for MD5 and
    SHA-256 occur concurrently on dedicated worker threads receiving stream
    chunks via bounded queues.

    Returns (md5_hex, sha256_hex, total_bytes).
    Raises FileNotFoundError if file_path does not exist.
    Raises IsADirectoryError (or PermissionError on Windows) if file_path is a directory.
    Raises ValueError if chunk_size is not a positive integer.
    Raises TypeError if chunk_size is not an integer.
    """
    _validate_chunk_size(chunk_size)
    if not multithreaded:
        digests, total_bytes = _hash_file(file_path, ["md5", "sha256"], chunk_size=chunk_size)
        return digests[0], digests[1], total_bytes

    # Multithreaded MtHasher pipeline
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"Target is a directory, not a file: {p}")

    q_md5: queue.Queue = queue.Queue(maxsize=16)
    q_sha: queue.Queue = queue.Queue(maxsize=16)
    md5_hasher = hashlib.md5()
    sha_hasher = hashlib.sha256()
    err_event = threading.Event()
    worker_exc: list[Exception] = []

    def _worker(q: queue.Queue, h) -> None:
        while not err_event.is_set():
            try:
                chunk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                q.task_done()
                break
            try:
                h.update(chunk)
            except Exception as e:
                worker_exc.append(e)
                err_event.set()
            finally:
                q.task_done()

    t_md5 = threading.Thread(target=_worker, args=(q_md5, md5_hasher), daemon=True)
    t_sha = threading.Thread(target=_worker, args=(q_sha, sha_hasher), daemon=True)
    t_md5.start()
    t_sha.start()

    total_bytes = 0
    try:
        with open(p, "rb") as fh:
            while not err_event.is_set():
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                q_md5.put(chunk)
                q_sha.put(chunk)
    finally:
        q_md5.put(None)
        q_sha.put(None)
        t_md5.join()
        t_sha.join()

    if worker_exc:
        raise worker_exc[0]

    return md5_hasher.hexdigest(), sha_hasher.hexdigest(), total_bytes

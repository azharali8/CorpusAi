"""Shared atomic JSON persistence and filesystem serialization utilities.

Provides consistent, crash-resilient atomic file writing using a temporary
sibling file, explicit flushing, fsync, and atomic filesystem replacement.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional, Union


def ensure_parent_dir(file_path: Union[str, Path]) -> Path:
    """Ensure the parent directory of file_path exists and return lexical file_path as Path.

    Preserves the exact lexical path without resolving symlinks or normalizing
    lexical structure, matching the behavior of os.path.dirname(os.path.abspath(file_path)).
    """
    path = Path(file_path)
    parent_dir = path.parent
    if str(parent_dir) not in ("", "."):
        parent_dir.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_json(file_path: Union[str, Path], data: Any, indent: int = 2) -> None:
    """Atomically write JSON data to file_path.

    Uses a temporary sibling file with process ID suffix, flushes to disk,
    invokes os.fsync on the file descriptor, and atomically replaces the
    destination file via os.replace. If serialization fails before replacement,
    the temporary file is cleaned up and the original file remains intact.
    """
    path = ensure_parent_dir(file_path)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def read_json(
    file_path: Union[str, Path],
    missing_file_message: Optional[str] = None,
) -> Any:
    """Read and deserialize JSON data from file_path.

    Args:
        file_path: Destination path as str or Path.
        missing_file_message: Custom message for FileNotFoundError. If omitted,
            defaults to "File not found: {file_path}".

    Raises:
        FileNotFoundError: If the target file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        msg = missing_file_message or f"File not found: {file_path}"
        raise FileNotFoundError(msg)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

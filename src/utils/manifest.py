"""
Shared SHA-256 checksum manifest generation for CorpusAI.

Provides a single canonical implementation of the checksums.sha256 format used
by archive_package, replay_validation, and corpusai_run, eliminating duplicated
manifest-generation code while preserving exact format compatibility.
"""

from pathlib import Path
from typing import Dict, Union

from src.utils.crypto import sha256_file


def generate_sha256_manifest(
    files_dict: Dict[Union[str, Path], Union[str, Path]],
    base_dir: Union[str, Path] = "",
) -> str:
    """
    Generate standard SHA-256 checksum manifest lines.

    Format per line: ``<sha256_hex>  <relative-path>``
    Lines are sorted by relative-path label and separated by ``\\n`` with a
    trailing newline, matching the ``checksums.sha256`` convention used across
    CorpusAI archival packages.

    Args:
        files_dict: Mapping of relative-path-label -> file path.
            - When *base_dir* is non-empty, the full path is computed as
              ``Path(base_dir) / rel_key``, and the dict values are
              ignored.  This matches the ``corpusai_run`` calling pattern where
              keys are short names and values are absolute paths relative to a
              common run directory.
            - When *base_dir* is empty (default), the full path is taken from
              the dict *value* (the absolute or relative path stored there).
              This matches the ``replay_validation`` calling pattern.
        base_dir: Optional base directory prefix applied to each key.

    Returns:
        Manifest string: one ``<sha256>  <relpath>`` line per file, sorted by
        relative-path label, with a single trailing newline.

    Raises:
        FileNotFoundError: If a referenced file does not exist.
        PermissionError: If a referenced file cannot be opened.
    """
    lines = []
    base_path = Path(base_dir) if base_dir else None

    # Sort keys by their string representation
    sorted_keys = sorted(files_dict.keys(), key=lambda k: str(k))

    for rel_key in sorted_keys:
        rel_str = str(rel_key)
        full_path = (base_path / rel_str) if base_path is not None else files_dict[rel_key]
        digest = sha256_file(full_path)
        # Normalise relative path to forward slashes for portable checksum files using Path.as_posix()
        clean_rel = Path(rel_str).as_posix()
        lines.append(f"{digest}  {clean_rel}")
    return "\n".join(lines) + "\n"

"""
Shared SHA-256 checksum manifest generation for CorpusAI.

Provides a single canonical implementation of the checksums.sha256 format used
by archive_package, replay_validation, and corpusai_run, eliminating duplicated
manifest-generation code while preserving exact format compatibility.
"""

import os
from typing import Dict

from src.utils.crypto import sha256_file


def generate_sha256_manifest(files_dict: Dict[str, str], base_dir: str = "") -> str:
    """
    Generate standard SHA-256 checksum manifest lines.

    Format per line: ``<sha256_hex>  <relative-path>``
    Lines are sorted by relative-path label and separated by ``\\n`` with a
    trailing newline, matching the ``checksums.sha256`` convention used across
    CorpusAI archival packages.

    Args:
        files_dict: Mapping of relative-path-label -> file path.
            - When *base_dir* is non-empty, the full path is computed as
              ``os.path.join(base_dir, rel_key)``, and the dict values are
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
    for rel_path in sorted(files_dict.keys()):
        full_path = os.path.join(base_dir, rel_path) if base_dir else files_dict[rel_path]
        digest = sha256_file(full_path)
        # Normalise to forward slashes for portable checksum files
        clean_rel = rel_path.replace("\\", "/")
        lines.append(f"{digest}  {clean_rel}")
    return "\n".join(lines) + "\n"

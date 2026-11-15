"""
Path safety and containment utilities for CorpusAI.

Provides deterministic path containment resolution ensuring relative paths
do not escape base directories via parent traversal, absolute roots, drive qualifications,
or symlink redirection.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Tuple, Union


def resolve_contained_path(
    base_dir: Union[str, Path],
    relative_path: str,
) -> Tuple[Path, str]:
    """
    Safely resolve a candidate relative path inside a base directory.

    Guarantees:
    - Candidate path cannot escape base_dir via '..' components.
    - Candidate path cannot be a POSIX absolute path or Windows absolute/drive-qualified/UNC path.
    - Candidate path cannot contain null bytes or be empty.
    - Preserves legitimate nested relative subdirectories.
    - Normalizes logical relative key using forward slashes ('/').
    - Follows and resolves symlinks on the filesystem and verifies the final target
      remains strictly contained within the resolved base directory.

    Limitations:
    - Path resolution followed by a subsequent filesystem access does not by itself
      eliminate concurrent filesystem changes or TOCTOU (time-of-check to time-of-use)
      race conditions created by concurrent adversarial symlink swapping.

    Returns:
        (resolved_filesystem_path: Path, normalized_logical_relative_key: str)

    Raises:
        ValueError: If relative_path attempts to escape base_dir or contains invalid roots/traversal.
    """
    if not isinstance(relative_path, str):
        raise ValueError(f"Path must be a string, got {type(relative_path).__name__}")

    if not relative_path or not relative_path.strip():
        raise ValueError("Relative path cannot be empty or blank")

    if "\x00" in relative_path:
        raise ValueError("Path cannot contain null bytes")

    # Reject Windows drive-qualified (C:foo, C:\foo), UNC (\\server\share), or root-relative (\foo) paths
    p_win = PureWindowsPath(relative_path)
    if p_win.is_absolute() or p_win.drive or p_win.root:
        raise ValueError(f"Absolute, drive-qualified, or root-relative paths are forbidden: '{relative_path}'")

    # Reject POSIX absolute paths (/foo)
    p_posix = PurePosixPath(relative_path)
    if p_posix.is_absolute() or p_posix.root:
        raise ValueError(f"Absolute paths are forbidden: '{relative_path}'")

    # Reject any component containing '..'
    # Split on both '/' and '\' to inspect raw path segments
    normalized_separators = relative_path.replace("\\", "/")
    segments = [seg for seg in normalized_separators.split("/") if seg and seg != "."]

    if not segments:
        raise ValueError("Relative path resolves to empty directory or root")

    for seg in segments:
        if seg == "..":
            raise ValueError(f"Path traversal ('..') is strictly forbidden: '{relative_path}'")

    logical_key = "/".join(segments)

    # Resolve filesystem base
    try:
        base = Path(base_dir).resolve()
    except (OSError, RuntimeError) as err:
        raise ValueError(f"Failed to resolve base directory '{base_dir}': {err}") from err

    # Construct candidate and resolve to handle any symlinks or symlinked parent directories
    try:
        candidate = (base / logical_key).resolve()
    except (OSError, RuntimeError) as err:
        raise ValueError(f"Failed to resolve candidate path '{relative_path}' in base '{base_dir}': {err}") from err

    # Strictly reject the base directory itself (e.g. symlink pointing back to base_dir)
    if candidate == base:
        raise ValueError(f"Path resolves to the base directory itself, not beneath it: '{relative_path}'")

    # Verify candidate is strictly contained beneath resolved base
    try:
        candidate.relative_to(base)
    except ValueError as err:
        raise ValueError(f"Path traversal detected: '{relative_path}' escapes base directory '{base}'") from err

    return candidate, logical_key

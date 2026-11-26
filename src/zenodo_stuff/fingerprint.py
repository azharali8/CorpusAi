"""
Package fingerprint computation for archival deposits.
"""

import hashlib
import os
from pathlib import Path

from src.utils.crypto import sha256_file


def compute_package_fingerprint(package_dir: str) -> str:
    """
    Compute a stable SHA-256 fingerprint of the finalized package.
    Uses checksums.sha256 if present, or sorted contents.
    """
    checksums_path = Path(package_dir) / "checksums.sha256"
    if checksums_path.exists():
        return sha256_file(checksums_path)
    # Fallback: hash directory tree listing
    hasher = hashlib.sha256()
    for root, _, files in os.walk(package_dir):
        for f in sorted(files):
            hasher.update(f.encode("utf-8"))
    return hasher.hexdigest()

"""
State Storage and Concurrency Management module for CorpusAI Phase 9B.

Provides:
- PortalLock: Cross-platform OS advisory file lock ensuring process-lifetime
  exclusivity per portal (msvcrt on Windows, fcntl on POSIX).
- StateStorageBackend: Abstract boundary for resolving portal state roots,
  loading/persisting operational state, and coordinating access.
- LocalStateStorageBackend: Local filesystem implementation conforming to existing
  atomic-write and relative checksum conventions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import datetime
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("corpusai.storage")


class LockAcquisitionError(Exception):
    """Raised when portal concurrency lock cannot be acquired."""
    pass


class PortalLock:
    """
    Cross-platform OS-level advisory file lock with process-lifetime ownership.

    Uses msvcrt.locking on Windows and fcntl.flock on POSIX.
    - Prevents overlapping runs for the same portal.
    - Automatically released by the OS kernel if the process terminates or crashes.
    - Diagnostic metadata (PID, started_at) is stored for debugging without
      relying on file deletion or timestamp expiration to release ownership.
    """

    def __init__(self, lock_file_path: str, portal_id: str = ""):
        self.lock_file_path = os.path.abspath(lock_file_path)
        self.portal_id = portal_id
        self._file_obj = None
        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    def acquire(self, blocking: bool = False, timeout_seconds: float = 0.0) -> bool:
        """
        Attempt to acquire exclusive advisory lock.
        Returns True if acquired, False otherwise.
        """
        if self._acquired:
            return True

        os.makedirs(os.path.dirname(self.lock_file_path), exist_ok=True)
        start_time = time.time()

        while True:
            try:
                # Open with read/write (create if not exists)
                # On Windows, open('a+b') or open('r+b') works with msvcrt.locking
                if not os.path.exists(self.lock_file_path):
                    with open(self.lock_file_path, "a+b") as f:
                        f.write(b" ")
                        f.flush()

                f = open(self.lock_file_path, "r+b")

                # Apply OS-level lock on first byte
                if sys.platform == "win32":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Lock granted
                self._file_obj = f
                self._acquired = True

                # Write diagnostic metadata (safe after lock is held)
                try:
                    meta = {
                        "pid": os.getpid(),
                        "portal_id": self.portal_id,
                        "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    meta_bytes = json.dumps(meta).encode("utf-8")
                    f.seek(1)
                    f.write(meta_bytes)
                    f.truncate(1 + len(meta_bytes))
                    f.flush()
                except Exception:
                    pass

                logger.debug(f"Acquired exclusive portal lock: {self.lock_file_path}")
                return True

            except (BlockingIOError, PermissionError, OSError):
                # Lock held by another process
                if 'f' in locals() and f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass

                if not blocking or (time.time() - start_time) >= timeout_seconds:
                    return False
                time.sleep(0.1)

    def release(self) -> None:
        """Release OS advisory lock and close handle."""
        if not self._acquired or self._file_obj is None:
            return

        try:
            if sys.platform == "win32":
                import msvcrt
                self._file_obj.seek(0)
                msvcrt.locking(self._file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning(f"Error unlocking {self.lock_file_path}: {e}")
        finally:
            try:
                self._file_obj.close()
            except Exception:
                pass
            self._file_obj = None
            self._acquired = False
            logger.debug(f"Released portal lock: {self.lock_file_path}")

    def __enter__(self) -> "PortalLock":
        if not self.acquire():
            raise LockAcquisitionError(
                f"Could not acquire portal lock for '{self.portal_id}' at '{self.lock_file_path}'. "
                f"Another process is currently holding the exclusive lock."
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


class StateStorageBackend(ABC):
    """
    Abstract interface for managing operational state persistence and coordination.
    """

    @abstractmethod
    def get_lock(self, portal_id: str) -> PortalLock:
        """Return a portal lock instance for the given portal."""
        pass

    @abstractmethod
    def resolve_portal_root(self, portal_id: str) -> str:
        """Return base directory path for portal operational state."""
        pass

    @abstractmethod
    def get_catalog_path(self, portal_id: str) -> str:
        """Return path to catalog.json for portal."""
        pass

    @abstractmethod
    def get_deposit_registry_path(self, portal_id: str) -> str:
        """Return path to deposit_registry.json for portal."""
        pass


class LocalStateStorageBackend(StateStorageBackend):
    """
    Local filesystem storage backend.

    Maintains directory layout:
    <results_base_dir>/
      ├── portal.lock
      ├── catalog.json
      ├── deposit_registry.json
      ├── runs/
      └── packages/<portal_id>/runs/<run_id>/
    """

    def __init__(self, results_base_dir: str):
        self.results_base_dir = os.path.abspath(results_base_dir)

    def resolve_portal_root(self, portal_id: str) -> str:
        return self.results_base_dir

    def get_lock(self, portal_id: str) -> PortalLock:
        lock_file = os.path.join(self.results_base_dir, f"{portal_id}.lock")
        return PortalLock(lock_file, portal_id=portal_id)

    def get_catalog_path(self, portal_id: str) -> str:
        return os.path.join(self.results_base_dir, "catalog.json")

    def get_deposit_registry_path(self, portal_id: str) -> str:
        return os.path.join(self.results_base_dir, "deposit_registry.json")

    def get_packages_root(self) -> str:
        return os.path.join(self.results_base_dir, "packages")

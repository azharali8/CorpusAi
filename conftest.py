"""
conftest.py — project-root pytest configuration.

Auto-generates missing WARC fixtures before any test session starts.
This ensures the test suite works correctly on clean CI checkouts where
data/warc/*.warc files are absent (they are git-ignored).

The generator is deterministic: given the same tracked HTML source files
it always produces the same WARC bytes and SHA-256 digests.
"""

import os
import sys

import pytest

# Ensure project root is on sys.path (needed for research helper import inside
# the generator when pytest is invoked from a non-root directory).
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def pytest_configure(config):
    """Generate WARC fixtures if they are missing before the session starts."""
    _ensure_warc_fixtures()


def pytest_unconfigure(config):
    """
    Clean up third-party logging handlers before Python interpreter shutdown.
    webarticlecurator.strategies initializes module-level DummyLogger instances
    from mplogger. In pytest, when stderr/stdout stream handlers are finalized,
    mplogger's __del__ tries to flush an already-closed stream, printing
    'Exception ignored in Logger.__del__: ValueError: I/O operation on closed file.'
    Explicitly detaching and closing these handlers here prevents shutdown exceptions.
    """
    try:
        import mplogger.logger
        import gc
        for obj in gc.get_objects():
            if isinstance(obj, mplogger.logger.Logger):
                for h in list(obj._logger.handlers):
                    obj._logger.removeHandler(h)
                    try:
                        h.close()
                    except Exception:
                        pass
    except ImportError:
        pass


def _ensure_warc_fixtures():
    warc_dir = os.path.join(_ROOT, "data", "warc")
    synthetic = os.path.join(warc_dir, "synthetic_portal.warc")
    mixed = os.path.join(warc_dir, "mixed_content_test.warc")

    needs_gen = not os.path.exists(synthetic) or not os.path.exists(mixed)
    if not needs_gen:
        return

    # Import the generator (same package, so this is always available)
    from tests.fixtures.generate_warc_fixtures import (
        generate_synthetic_portal_warc,
        generate_mixed_content_warc,
    )

    if not os.path.exists(synthetic):
        print(f"\n[conftest] Generating missing fixture: {synthetic}")
        generate_synthetic_portal_warc(synthetic)

    if not os.path.exists(mixed):
        print(f"\n[conftest] Generating missing fixture: {mixed}")
        generate_mixed_content_warc(mixed)

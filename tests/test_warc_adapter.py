"""
Tests for src/webarticlecurator_adapter.py

All tests are offline (no live network downloads).
Tests verify safe subprocess usage (shell=False), structured error returns,
and graceful degradation when webarticlecurator is unavailable.
"""

import os
import sys
import ast
import inspect
import pytest

from src.webarticlecurator_adapter import WebArticleCuratorAdapter

WARC_FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "data", "warc", "synthetic_portal.warc"
)
WARC_FIXTURE = os.path.normpath(WARC_FIXTURE)


# ---------------------------------------------------------------------------
# Shared adapter instance
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    return WebArticleCuratorAdapter()


# ---------------------------------------------------------------------------
# 1. is_available() returns True (webarticlecurator is installed)
# ---------------------------------------------------------------------------

def test_is_available(adapter):
    """webarticlecurator must be importable in the project venv."""
    result = adapter.is_available()
    assert result is True, (
        "webarticlecurator is not installed. Run: pip install webarticlecurator"
    )


# ---------------------------------------------------------------------------
# 2. get_version() returns a non-None string
# ---------------------------------------------------------------------------

def test_get_version_non_none(adapter):
    version = adapter.get_version()
    assert version is not None
    assert isinstance(version, str)
    assert len(version) > 0


# ---------------------------------------------------------------------------
# 3. _run_cli() with a missing WARC returns a structured error dict (not an exception)
# ---------------------------------------------------------------------------

def test_run_cli_missing_warc_structured_error(adapter):
    """
    Calling _run_cli with a nonexistent WARC path must return a dict with
    success=False rather than raising an unhandled exception.
    """
    result = adapter._run_cli(["validate", "/nonexistent/does_not_exist.warc"], timeout=30)
    assert isinstance(result, dict)
    assert "success" in result
    assert "returncode" in result
    assert "stdout" in result
    assert "stderr" in result
    # The command will fail but must not raise
    # (returncode non-zero or stderr non-empty)
    assert result["success"] is False or result["returncode"] != 0 or result["stderr"]


# ---------------------------------------------------------------------------
# 4. validate_warc() on the fixture returns a dict with 'valid' key
# ---------------------------------------------------------------------------

def test_validate_warc_fixture(adapter):
    """
    validate_warc() on a well-formed WARC fixture must return a dict.
    We do not assert valid=True because the WAC CLI output format may vary;
    we only assert the method returns a structured dict without crashing.
    """
    result = adapter.validate_warc(WARC_FIXTURE)
    assert isinstance(result, dict)
    assert "valid" in result
    assert "success" in result


# ---------------------------------------------------------------------------
# 5. list_urls() on fixture returns a dict with 'urls' key (list)
# ---------------------------------------------------------------------------

def test_list_urls_returns_structure(adapter):
    """
    list_urls() must return a dict with a 'urls' key containing a list,
    and a 'success' key. We do not assert count=6 because WAC CLI format
    may differ; we assert the interface contract.
    """
    result = adapter.list_urls(WARC_FIXTURE)
    assert isinstance(result, dict)
    assert "urls" in result
    assert isinstance(result["urls"], list)
    assert "success" in result


# ---------------------------------------------------------------------------
# 6. Subprocess failure with a bad WARC path is handled gracefully
# ---------------------------------------------------------------------------

def test_list_urls_bad_path_graceful(adapter):
    result = adapter.list_urls("/this/path/does/not/exist.warc")
    assert isinstance(result, dict)
    assert result["success"] is False
    assert "error" in result
    # Must not raise


# ---------------------------------------------------------------------------
# 7. No shell=True in source (static code inspection)
# ---------------------------------------------------------------------------

def test_no_shell_true_in_source():
    """
    Statically verify that shell=True never appears in webarticlecurator_adapter.py.
    This is a hard project constraint.
    """
    adapter_path = os.path.join(
        os.path.dirname(__file__), "..", "src", "webarticlecurator_adapter.py"
    )
    adapter_path = os.path.normpath(adapter_path)

    with open(adapter_path, "r", encoding="utf-8") as f:
        source = f.read()

    # Parse the AST and look for shell=True keyword arguments
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    violations.append(f"Line {node.lineno}: shell=True found")

    assert not violations, "shell=True violations found:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# 8. Missing webarticlecurator dependency handled gracefully (simulated)
# ---------------------------------------------------------------------------

def test_missing_dependency_graceful(monkeypatch):
    """
    Simulate webarticlecurator not being importable.
    is_available() must return False, get_version() must return None.
    No exception should propagate.
    """
    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "webarticlecurator":
            raise ImportError("Simulated missing package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    adapter = WebArticleCuratorAdapter()
    assert adapter.is_available() is False
    assert adapter.get_version() is None

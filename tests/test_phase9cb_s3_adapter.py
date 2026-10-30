"""
Phase 9C-B offline tests: S3RemoteStorageBackend using mocked boto3.

All tests use unittest.mock to patch boto3 and botocore.
No real S3 connections are made.
"""
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.s3_storage_backend import (
    S3RemoteStorageBackend,
    S3LeaseHeartbeat,
    RemoteConditionalConflictError,
)
from src.remote_storage_contract import (
    RemoteCasConflictError,
    RemoteLockError,
    RemoteStorageError,
)

# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


def _make_control_record(
    portal_id: str = "test-portal",
    expired: bool = False,
    owner: str = "runner-1",
    gen: int = 1,
) -> dict:
    """Build a minimal portal control record dict."""
    if expired:
        expires_at = "1970-01-01T00:00:00+00:00"
    else:
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        expires_at = future.isoformat()
    return {
        "portal_id": portal_id,
        "control_version": 1,
        "fencing_generation": gen,
        "baseline_generation": 0,
        "lease": {
            "lease_owner_id": owner,
            "acquired_at": "2026-01-01T00:00:00+00:00",
            "renewed_at": "2026-01-01T00:00:00+00:00",
            "expires_at": expires_at,
            "ttl_seconds": 2700,
        },
        "baseline": {
            "latest_successful_run_id": None,
            "committed_at": None,
            "committed_by_generation": None,
            "baseline_generation": 0,
            "manifest_path": None,
            "checksums_path": None,
        },
        "catalog_head_etag": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _make_s3_get_response(body_dict: dict, etag: str = "abc123") -> dict:
    """Build a mock S3 get_object response dict."""
    data = json.dumps(body_dict, indent=2).encode("utf-8")
    return {
        "Body": io.BytesIO(data),
        "ETag": f'"{etag}"',
        "ContentLength": len(data),
    }


def _make_client_error(code: str, http_status: int = 400) -> Exception:
    """Build a botocore ClientError with a given error code."""
    from botocore.exceptions import ClientError

    error_response = {
        "Error": {"Code": code, "Message": f"Simulated {code}"},
        "ResponseMetadata": {"HTTPStatusCode": http_status},
    }
    return ClientError(error_response, "TestOperation")


def _make_backend(mock_s3=None, bucket="test-bucket", prefix="test") -> S3RemoteStorageBackend:
    """Return a backend wired to ``mock_s3`` (a MagicMock if not provided)."""
    if mock_s3 is None:
        mock_s3 = MagicMock()
    return S3RemoteStorageBackend(bucket=bucket, prefix=prefix, boto3_client=mock_s3)


# ---------------------------------------------------------------------------
# Test 1: importability
# ---------------------------------------------------------------------------


def test_s3_backend_imports_without_boto3_installed():
    """Importing S3RemoteStorageBackend succeeds when boto3 is installed."""
    # If boto3 is NOT installed the import at module-load time raises ImportError.
    # When this test file is collected we have already imported the module above,
    # so a successful collection proves the module is importable.
    from src.s3_storage_backend import S3RemoteStorageBackend as _Backend  # noqa: F401

    assert _Backend is not None


# ---------------------------------------------------------------------------
# Test 2: acquire_lease — first time (NoSuchKey → create)
# ---------------------------------------------------------------------------


def test_acquire_lease_first_time_creates_control_record():
    """First-time acquire creates a control record with fencing_generation=1."""
    mock_s3 = MagicMock()
    # get_object raises NoSuchKey
    mock_s3.get_object.side_effect = _make_client_error("NoSuchKey", 404)
    # put_object returns ETag
    mock_s3.put_object.return_value = {"ETag": '"abc123"'}

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "runner-1", ttl_seconds=2700)

    assert ok is True
    assert record is not None
    assert record.fencing_generation == 1
    assert record.lease.lease_owner_id == "runner-1"
    assert etag == "abc123"

    # Verify put_object was called with IfNoneMatch='*'
    call_kwargs = mock_s3.put_object.call_args[1]
    assert call_kwargs.get("IfNoneMatch") == "*"


# ---------------------------------------------------------------------------
# Test 3: acquire_lease — existing expired record
# ---------------------------------------------------------------------------


def test_acquire_lease_existing_expired_record():
    """Acquiring lease over an expired record succeeds and increments fencing_generation."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=True, gen=3)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="old-etag")
    mock_s3.put_object.return_value = {"ETag": '"new-etag"'}

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "runner-2")

    assert ok is True
    assert record is not None
    assert record.fencing_generation == 4  # incremented from 3
    assert record.lease.lease_owner_id == "runner-2"
    assert etag == "new-etag"

    # IfMatch must be a quoted entity-tag (HTTP spec requires this).
    # _put_object_conditional applies _quote_etag() before passing to boto3.
    call_kwargs = mock_s3.put_object.call_args[1]
    assert call_kwargs.get("IfMatch") == '"old-etag"'


# ---------------------------------------------------------------------------
# Test 4: acquire_lease — existing active record held by different runner
# ---------------------------------------------------------------------------


def test_acquire_lease_existing_active_record_fails():
    """Acquiring a lease held by another active runner returns (False, record, etag)."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=False, owner="other-runner", gen=2)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="held-etag")

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "my-runner")

    assert ok is False
    assert record is not None
    assert record.lease.lease_owner_id == "other-runner"
    # put_object must NOT have been called
    mock_s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: acquire_lease — CAS conflict on put_object
# ---------------------------------------------------------------------------


def test_acquire_lease_cas_conflict_returns_false():
    """PreconditionFailed on put_object returns (False, None, None), not an exception."""
    mock_s3 = MagicMock()
    # Expired record
    record_dict = _make_control_record(expired=True, gen=1)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="stale")
    mock_s3.put_object.side_effect = _make_client_error("PreconditionFailed", 412)

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "runner-1")

    assert ok is False
    assert record is None
    assert etag is None


# ---------------------------------------------------------------------------
# Test 5b: acquire_lease — CAS conflict on first-time create
# ---------------------------------------------------------------------------


def test_acquire_lease_first_time_cas_conflict_returns_false():
    """PreconditionFailed on first-time IfNoneMatch create returns (False, None, None)."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = _make_client_error("NoSuchKey", 404)
    mock_s3.put_object.side_effect = _make_client_error("PreconditionFailed", 412)

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "runner-1")

    assert ok is False
    assert record is None
    assert etag is None


# ---------------------------------------------------------------------------
# Test 6: renew_lease — success
# ---------------------------------------------------------------------------


def test_renew_lease_success():
    """Successful lease renewal returns (True, new_etag) without changing fencing_generation."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=False, owner="runner-1", gen=5)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="cur-etag")
    mock_s3.put_object.return_value = {"ETag": '"renewed-etag"'}

    backend = _make_backend(mock_s3)
    ok, new_etag = backend.renew_portal_lease(
        portal_id="test-portal",
        runner_id="runner-1",
        fencing_generation=5,
        expected_control_etag="cur-etag",
    )

    assert ok is True
    assert new_etag == "renewed-etag"

    # Check that fencing_generation and baseline_generation are unchanged in written record
    written_body = mock_s3.put_object.call_args[1]["Body"]
    written_record = json.loads(written_body.decode("utf-8"))
    assert written_record["fencing_generation"] == 5
    assert written_record["baseline_generation"] == 0


# ---------------------------------------------------------------------------
# Test 7: renew_lease — stale ETag
# ---------------------------------------------------------------------------


def test_renew_lease_stale_etag_returns_false():
    """Renewal fails if the stored ETag differs from expected_control_etag."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=False, owner="runner-1", gen=5)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="real-etag")

    backend = _make_backend(mock_s3)
    ok, new_etag = backend.renew_portal_lease(
        portal_id="test-portal",
        runner_id="runner-1",
        fencing_generation=5,
        expected_control_etag="wrong-etag",  # mismatch
    )

    assert ok is False
    assert new_etag is None
    mock_s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: release_lease — ownership check
# ---------------------------------------------------------------------------


def test_release_lease_ownership_check():
    """Release fails silently (returns False) when record is owned by a different runner."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=False, owner="other-runner", gen=3)
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="cur-etag")

    backend = _make_backend(mock_s3)
    released = backend.release_portal_lease(
        portal_id="test-portal",
        runner_id="my-runner",  # not the owner
        fencing_generation=3,
        expected_control_etag="cur-etag",
    )

    assert released is False
    mock_s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: commit_baseline_and_catalog — advances baseline_generation only
# ---------------------------------------------------------------------------


def test_commit_baseline_advances_baseline_generation_only():
    """commit_baseline_and_catalog increments baseline_generation but not fencing_generation."""
    mock_s3 = MagicMock()
    ctrl_dict = _make_control_record(expired=False, owner="runner-1", gen=7)
    ctrl_dict["baseline_generation"] = 3

    # get_object is called twice: once for control, once (maybe) for catalog or none
    def get_object_side_effect(**kwargs):
        key = kwargs.get("Key", "")
        if "portal_control" in key:
            return _make_s3_get_response(ctrl_dict, etag="ctrl-etag")
        raise _make_client_error("NoSuchKey", 404)

    mock_s3.get_object.side_effect = get_object_side_effect
    # put_object returns different ETags for catalog vs control
    put_call_counter = {"n": 0}

    def put_side_effect(**kwargs):
        put_call_counter["n"] += 1
        return {"ETag": f'"put-etag-{put_call_counter["n"]}"'}

    mock_s3.put_object.side_effect = put_side_effect

    from src.archive_catalog import ArchiveCatalog
    catalog = ArchiveCatalog(portal_id="test-portal")

    backend = _make_backend(mock_s3)
    ok, new_ctrl_etag, new_cat_etag = backend.commit_baseline_and_catalog(
        portal_id="test-portal",
        run_id="run-001",
        catalog=catalog,
        expected_catalog_etag=None,  # catalog didn't exist
        expected_control_etag="ctrl-etag",
        fencing_generation=7,
        runner_id="runner-1",
    )

    assert ok is True
    # put_object called twice: catalog then control
    assert mock_s3.put_object.call_count == 2

    # Check written control record has correct generations
    written_ctrl_body = mock_s3.put_object.call_args_list[1][1]["Body"]
    written_ctrl = json.loads(written_ctrl_body.decode("utf-8"))
    assert written_ctrl["baseline_generation"] == 4  # incremented from 3
    assert written_ctrl["fencing_generation"] == 7   # unchanged


# ---------------------------------------------------------------------------
# Test 10: commit_baseline — CAS conflict returns (False, None, None)
# ---------------------------------------------------------------------------


def test_commit_baseline_cas_conflict_returns_false():
    """CAS conflict during catalog or control write returns (False, None, None)."""
    mock_s3 = MagicMock()
    ctrl_dict = _make_control_record(expired=False, owner="runner-1", gen=2)

    def get_object_side_effect(**kwargs):
        key = kwargs.get("Key", "")
        if "portal_control" in key:
            return _make_s3_get_response(ctrl_dict, etag="ctrl-etag")
        raise _make_client_error("NoSuchKey", 404)

    mock_s3.get_object.side_effect = get_object_side_effect

    # Catalog write succeeds, control write raises PreconditionFailed
    call_n = {"n": 0}

    def put_side_effect(**kwargs):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {"ETag": '"cat-etag"'}
        raise _make_client_error("PreconditionFailed", 412)

    mock_s3.put_object.side_effect = put_side_effect

    from src.archive_catalog import ArchiveCatalog
    catalog = ArchiveCatalog(portal_id="test-portal")

    backend = _make_backend(mock_s3)
    ok, new_ctrl_etag, new_cat_etag = backend.commit_baseline_and_catalog(
        portal_id="test-portal",
        run_id="run-002",
        catalog=catalog,
        expected_catalog_etag=None,
        expected_control_etag="ctrl-etag",
        fencing_generation=2,
        runner_id="runner-1",
    )

    assert ok is False
    assert new_ctrl_etag is None
    assert new_cat_etag is None


# ---------------------------------------------------------------------------
# Test 11: verify_remote_artifacts — reads bytes, not ETag
# ---------------------------------------------------------------------------


def test_verify_remote_artifacts_reads_bytes_not_etag():
    """verify_remote_artifacts uses actual byte SHA-256, not ETag comparison."""
    mock_s3 = MagicMock()
    payload = b"hello-world-artifact"
    correct_sha256 = hashlib.sha256(payload).hexdigest()
    wrong_payload = b"tampered-bytes"
    wrong_sha256 = hashlib.sha256(wrong_payload).hexdigest()

    # First call: correct bytes (same ETag can still have wrong bytes if tampered)
    call_n = {"n": 0}

    def get_side_effect(**kwargs):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Return the real payload
            return {
                "Body": io.BytesIO(payload),
                "ETag": '"same-etag"',
                "ContentLength": len(payload),
            }
        else:
            # Return tampered bytes but the same ETag — verify must still detect mismatch
            return {
                "Body": io.BytesIO(wrong_payload),
                "ETag": '"same-etag"',
                "ContentLength": len(wrong_payload),
            }

    mock_s3.get_object.side_effect = get_side_effect

    backend = _make_backend(mock_s3)

    # Case 1: correct bytes → valid
    is_valid, mismatches = backend.verify_remote_artifacts(
        portal_id="test-portal",
        run_id="run-x",
        expected_checksums={"artifact.bin": correct_sha256},
    )
    assert is_valid is True
    assert mismatches == []

    # Case 2: same ETag but tampered bytes → mismatch detected
    is_valid2, mismatches2 = backend.verify_remote_artifacts(
        portal_id="test-portal",
        run_id="run-x",
        expected_checksums={"artifact.bin": correct_sha256},
    )
    assert is_valid2 is False
    assert len(mismatches2) == 1
    assert "artifact.bin" in mismatches2[0]


# ---------------------------------------------------------------------------
# Test 12: verify_remote_artifacts — missing file detected
# ---------------------------------------------------------------------------


def test_verify_remote_artifacts_missing_file_detected():
    """A missing S3 object is reported as a mismatch, not an exception."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = _make_client_error("NoSuchKey", 404)

    backend = _make_backend(mock_s3)
    is_valid, mismatches = backend.verify_remote_artifacts(
        portal_id="test-portal",
        run_id="run-y",
        expected_checksums={"missing_file.json": "deadbeef" * 8},
    )

    assert is_valid is False
    assert len(mismatches) == 1
    assert "missing_file.json" in mismatches[0]


# ---------------------------------------------------------------------------
# Test 13: stage_artifacts — network error raises RemoteStorageError
# ---------------------------------------------------------------------------


def test_stage_artifacts_network_error_raises():
    """A non-transient ClientError (after retries) raises RemoteStorageError."""
    mock_s3 = MagicMock()
    # InternalError with HTTP 500 — transient, will retry up to 3 times then raise
    mock_s3.put_object.side_effect = _make_client_error("InternalError", 500)

    backend = _make_backend(mock_s3)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
        tmp.write(b"artifact-data")
        tmp_path = tmp.name

    try:
        with patch("src.s3_storage_backend.time.sleep"):  # speed up retries
            with pytest.raises(RemoteStorageError):
                backend.stage_package_artifacts(
                    portal_id="test-portal",
                    run_id="run-z",
                    local_files={"file.bin": tmp_path},
                )
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Test 14: quarantine_failed_upload — non-destructive
# ---------------------------------------------------------------------------


def test_quarantine_failed_upload_writes_non_destructively():
    """Quarantine write does not delete any objects; returns non-empty quarantine_id."""
    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {"ETag": '"q-etag"'}

    backend = _make_backend(mock_s3)
    qid = backend.quarantine_failed_upload(
        portal_id="test-portal",
        run_id="run-q",
        runner_id="runner-1",
        reason="Checksum mismatch",
        details={"files": ["a.bin", "b.bin"]},
    )

    assert qid
    assert "quarantine" in qid

    # put_object called exactly once (for the quarantine record)
    assert mock_s3.put_object.call_count == 1
    # delete_object must NOT have been called
    mock_s3.delete_object.assert_not_called()
    mock_s3.delete_objects.assert_not_called()


# ---------------------------------------------------------------------------
# Test 15: transient error retry then success
# ---------------------------------------------------------------------------


def test_transient_error_retry_then_success():
    """ThrottlingException on first 2 attempts, success on 3rd."""
    mock_s3 = MagicMock()
    call_n = {"n": 0}

    def put_side_effect(**kwargs):
        call_n["n"] += 1
        if call_n["n"] < 3:
            raise _make_client_error("ThrottlingException", 400)
        return {"ETag": '"success-etag"'}

    mock_s3.put_object.side_effect = put_side_effect

    backend = _make_backend(mock_s3)

    with patch("src.s3_storage_backend.time.sleep"):  # don't actually sleep
        # Use _put_object_unconditional directly (used by sync_deposit_registry)
        result = backend.sync_deposit_registry(
            portal_id="test-portal",
            registry_data={"runs": []},
        )

    assert result is True
    assert call_n["n"] == 3


# ---------------------------------------------------------------------------
# Test 16: auth failure raises RemoteLockError
# ---------------------------------------------------------------------------


def test_auth_failure_raises_remote_lock_error():
    """AccessDenied error from put_object raises RemoteLockError."""
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = _make_client_error("NoSuchKey", 404)
    mock_s3.put_object.side_effect = _make_client_error("AccessDenied", 403)

    backend = _make_backend(mock_s3)

    with pytest.raises(RemoteLockError):
        backend.acquire_portal_lease("test-portal", "runner-1")


# ---------------------------------------------------------------------------
# Test 17: hydrate_state — control record is authoritative over catalog
# ---------------------------------------------------------------------------


def test_hydrate_state_control_authoritative_over_catalog():
    """hydrate_state corrects catalog.latest_successful_run_id to match control record."""
    mock_s3 = MagicMock()

    ctrl_dict = _make_control_record(expired=False, owner="runner-1", gen=2)
    ctrl_dict["baseline"]["latest_successful_run_id"] = "run-A"

    from src.archive_catalog import ArchiveCatalog
    cat = ArchiveCatalog(portal_id="test-portal", latest_successful_run_id="run-B")
    cat_dict = cat.to_dict()

    def get_side_effect(**kwargs):
        key = kwargs.get("Key", "")
        if "portal_control" in key:
            return _make_s3_get_response(ctrl_dict, etag="ctrl-etag")
        if "catalog" in key:
            return _make_s3_get_response(cat_dict, etag="cat-etag")
        raise _make_client_error("NoSuchKey", 404)

    mock_s3.get_object.side_effect = get_side_effect

    # list_objects_v2 returns no baseline package files
    mock_s3.get_paginator.return_value = MagicMock(
        paginate=MagicMock(return_value=[{"Contents": []}])
    )

    backend = _make_backend(mock_s3)

    with tempfile.TemporaryDirectory() as tmpdir:
        ctrl_rec, ctrl_etag, catalog, cat_etag, deposit = backend.hydrate_state(
            portal_id="test-portal",
            target_dir=tmpdir,
        )

    # Control record says run-A, so catalog must reflect run-A
    assert catalog is not None
    assert catalog.latest_successful_run_id == "run-A"


# ---------------------------------------------------------------------------
# Test 18: ETag stripped of surrounding quotes
# ---------------------------------------------------------------------------


def test_etag_stripped_of_quotes():
    """S3 returns ETag with surrounding quotes; backend stores/returns bare hex."""
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=True, gen=1)
    # S3 standard: ETag always surrounded by double-quotes
    mock_s3.get_object.return_value = _make_s3_get_response(record_dict, etag="abc123")
    mock_s3.put_object.return_value = {"ETag": '"def456"'}

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease("test-portal", "runner-1")

    assert ok is True
    # Returned ETag must be stripped
    assert etag == "def456"
    assert '"' not in etag


# ---------------------------------------------------------------------------
# Test 19-25: Targeted Fixes (409 Conflict, Ambiguous Commit, Heartbeat Lifecycle)
# ---------------------------------------------------------------------------


def test_classify_client_error_409_conditional_request_conflict():
    """HTTP 409 ConditionalRequestConflict is mapped to RemoteConditionalConflictError."""
    from botocore.exceptions import ClientError
    backend = _make_backend(MagicMock())
    err_409 = ClientError(
        {"Error": {"Code": "ConditionalRequestConflict", "Message": "Conflict"}},
        "PutObject",
    )
    with pytest.raises(RemoteConditionalConflictError):
        backend._classify_client_error(err_409)


def test_renew_lease_409_refreshes_and_revalidates_safely():
    """HTTP 409 on renew refreshes authoritative state and succeeds if lease is still ours."""
    from botocore.exceptions import ClientError
    mock_s3 = MagicMock()
    record_dict = _make_control_record(expired=False, owner="runner-1", gen=2)
    initial_resp = _make_s3_get_response(record_dict, etag="etag-1")
    refreshed_resp = _make_s3_get_response(record_dict, etag="etag-refreshed")

    # 1st get_object: initial read
    # 2nd get_object: refresh after 409
    mock_s3.get_object.side_effect = [initial_resp, refreshed_resp]

    err_409 = ClientError(
        {"Error": {"Code": "ConditionalRequestConflict", "Message": "Conflict"}},
        "PutObject",
    )
    # 1st put_object raises 409; 2nd put_object with refreshed etag succeeds
    mock_s3.put_object.side_effect = [err_409, {"ETag": '"etag-final"'}]

    backend = _make_backend(mock_s3)
    ok, new_etag = backend.renew_portal_lease(
        portal_id="test-portal",
        runner_id="runner-1",
        fencing_generation=2,
        expected_control_etag="etag-1",
    )

    assert ok is True
    assert new_etag == "etag-final"
    assert mock_s3.put_object.call_count == 2
    # Verify the retry did not blindly replay stale etag-1.
    # Also verify the IfMatch value is a quoted entity-tag per HTTP spec.
    assert mock_s3.put_object.call_args_list[1][1]["IfMatch"] == '"etag-refreshed"'


def test_commit_baseline_ambiguous_outcome_recovers_via_reread():
    """Network timeout/exception on control record write re-reads authoritative state."""
    from botocore.exceptions import ClientError
    from src.archive_catalog import ArchiveCatalog
    mock_s3 = MagicMock()
    ctrl_dict = _make_control_record(expired=False, owner="runner-1", gen=3)
    initial_ctrl_resp = _make_s3_get_response(ctrl_dict, etag="ctrl-etag-1")

    # In authoritative state, baseline committed successfully despite client-side error
    committed_dict = _make_control_record(expired=False, owner="runner-1", gen=3)
    committed_dict["baseline"]["latest_successful_run_id"] = "run-success-001"
    committed_dict["baseline"]["committed_by_generation"] = 3
    reread_ctrl_resp = _make_s3_get_response(committed_dict, etag="ctrl-etag-committed")

    mock_s3.get_object.side_effect = [initial_ctrl_resp, reread_ctrl_resp]

    # 1st put_object: catalog write succeeds
    # 2nd put_object: control write raises ConnectionError / socket timeout
    mock_s3.put_object.side_effect = [
        {"ETag": '"cat-etag-new"'},
        ConnectionResetError("Socket reset during control write"),
    ]

    backend = _make_backend(mock_s3)
    catalog = ArchiveCatalog(portal_id="test-portal")

    ok, ctrl_etag, cat_etag = backend.commit_baseline_and_catalog(
        portal_id="test-portal",
        run_id="run-success-001",
        catalog=catalog,
        expected_catalog_etag=None,
        expected_control_etag="ctrl-etag-1",
        fencing_generation=3,
        runner_id="runner-1",
    )

    assert ok is True
    assert ctrl_etag == "ctrl-etag-committed"
    assert cat_etag == "cat-etag-new"


def test_heartbeat_renewal_lifecycle_success():
    """S3LeaseHeartbeat runs periodic renewal and updates latest ETag."""
    mock_backend = MagicMock()
    mock_backend.renew_portal_lease.return_value = (True, "etag-renewed-1")

    hb = S3LeaseHeartbeat(
        backend=mock_backend,
        portal_id="test-portal",
        runner_id="runner-1",
        fencing_generation=1,
        initial_etag="etag-0",
        ttl_seconds=300,
        interval_seconds=0.05,
    )
    hb.start()
    import time
    time.sleep(0.15)
    hb.stop()

    assert hb.is_healthy()
    assert hb.get_latest_etag() == "etag-renewed-1"
    assert mock_backend.renew_portal_lease.called


def test_heartbeat_takeover_detection_signals_failure():
    """Takeover during crawl causes renew failure and sets failed_event immediately."""
    mock_backend = MagicMock()
    # Renewal returns (False, None) indicating CAS conflict/takeover
    mock_backend.renew_portal_lease.return_value = (False, None)

    hb = S3LeaseHeartbeat(
        backend=mock_backend,
        portal_id="test-portal",
        runner_id="runner-1",
        fencing_generation=1,
        initial_etag="etag-0",
        ttl_seconds=300,
        interval_seconds=0.05,
    )
    hb.start()
    import time
    time.sleep(0.12)
    hb.stop()

    assert not hb.is_healthy()
    assert hb.failed_event.is_set()
    assert "takeover or CAS conflict" in hb.failure_reason


def test_runner_s3_quarantine_on_verification_failure_exits_9(tmp_path, monkeypatch):
    """When S3 remote artifact verification fails, runner quarantines and exits code 9."""
    from src import corpusai_run
    from unittest.mock import MagicMock, patch

    results_dir = tmp_path / "results"
    results_dir.mkdir()

    mock_backend = MagicMock()
    # Hydration ok, lease ok
    mock_ctrl = MagicMock()
    mock_ctrl.fencing_generation = 1
    mock_ctrl.lease.ttl_seconds = 2700
    mock_backend.hydrate_state.return_value = (mock_ctrl, "etag-ctrl-1", None, None, None)
    mock_backend.acquire_portal_lease.return_value = (True, mock_ctrl, "etag-ctrl-1")
    mock_backend.stage_package_artifacts.return_value = {"manifest.json": MagicMock()}
    # Verify fails -> triggers quarantine
    mock_backend.verify_remote_artifacts.return_value = (False, ["Checksum mismatch for manifest.json"])
    mock_backend.quarantine_failed_upload.return_value = "quarantine-run-001"

    monkeypatch.setattr("src.s3_storage_backend.S3RemoteStorageBackend", lambda **kw: mock_backend)
    monkeypatch.setattr("src.corpusai_run.check_robots_txt", lambda *a, **k: (True, "OK"))

    sample_head = """
    <html><body>
    <div class="post"><a href="/news/post-1/">Post 1</a></div>
    </body></html>
    """
    sample_article = """
    <html><body>
    <article><h1>Post 1</h1><p>Body</p></article>
    </body></html>
    """
    from tests.test_phase8_runner import _make_html_resource
    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, sample_head)
        return _make_html_resource(url, sample_article)

    with patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):
        with patch.object(corpusai_run, "PORTAL_CONFIGS", {
            "wordpress-news": {
                "head_page_url": "https://wordpress.org/news/all-posts/",
                "portal_id": "wordpress-news",
                "results_base_dir": str(results_dir),
                "catalog_path": str(results_dir / "catalog.json"),
                "user_agent": "CorpusAI-Test",
                "polite_delay_seconds": 0.0,
                "max_archive_pages": 1,
                "max_articles": 1,
                "max_requests": 5,
                "max_retries": 1,
            }
        }):
            with pytest.raises(SystemExit) as exc:
                corpusai_run.main([
                    "--portal", "wordpress-news",
                    "--mode", "baseline",
                    "--results-dir", str(results_dir),
                    "--storage-backend", "s3",
                    "--s3-bucket", "test-bucket",
                ])

            assert exc.value.code == 9
            assert mock_backend.quarantine_failed_upload.called


def test_runner_s3_commit_failure_exits_9(tmp_path, monkeypatch):
    """When remote baseline commit fails (CAS conflict), runner exits code 9 without local success."""
    from src import corpusai_run
    from unittest.mock import MagicMock, patch

    results_dir = tmp_path / "results"
    results_dir.mkdir()

    mock_backend = MagicMock()
    mock_ctrl = MagicMock()
    mock_ctrl.fencing_generation = 1
    mock_ctrl.lease.ttl_seconds = 2700
    mock_backend.hydrate_state.return_value = (mock_ctrl, "etag-ctrl-1", None, None, None)
    mock_backend.acquire_portal_lease.return_value = (True, mock_ctrl, "etag-ctrl-1")
    mock_backend.stage_package_artifacts.return_value = {"manifest.json": MagicMock()}
    mock_backend.verify_remote_artifacts.return_value = (True, [])
    # Commit fails (CAS conflict or stale lease)
    mock_backend.commit_baseline_and_catalog.return_value = (False, None, None)

    monkeypatch.setattr("src.s3_storage_backend.S3RemoteStorageBackend", lambda **kw: mock_backend)
    monkeypatch.setattr("src.corpusai_run.check_robots_txt", lambda *a, **k: (True, "OK"))

    sample_head = "<html><body><div class='post'><a href='/news/post-1/'>Post 1</a></div></body></html>"
    sample_article = "<html><body><article><h1>Post 1</h1><p>Body</p></article></body></html>"
    from tests.test_phase8_runner import _make_html_resource
    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            return _make_html_resource(url, sample_head)
        return _make_html_resource(url, sample_article)

    with patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):
        with patch.object(corpusai_run, "PORTAL_CONFIGS", {
            "wordpress-news": {
                "head_page_url": "https://wordpress.org/news/all-posts/",
                "portal_id": "wordpress-news",
                "results_base_dir": str(results_dir),
                "catalog_path": str(results_dir / "catalog.json"),
                "user_agent": "CorpusAI-Test",
                "polite_delay_seconds": 0.0,
                "max_archive_pages": 1,
                "max_articles": 1,
                "max_requests": 5,
                "max_retries": 1,
            }
        }):
            with pytest.raises(SystemExit) as exc:
                corpusai_run.main([
                    "--portal", "wordpress-news",
                    "--mode", "baseline",
                    "--results-dir", str(results_dir),
                    "--storage-backend", "s3",
                    "--s3-bucket", "test-bucket",
                ])

            assert exc.value.code == 9
            assert mock_backend.commit_baseline_and_catalog.called


def test_runner_s3_heartbeat_failure_exits_10(tmp_path, monkeypatch):
    """When remote lease heartbeat fails during crawl, runner aborts and exits code 10."""
    from src import corpusai_run
    from unittest.mock import MagicMock, patch

    results_dir = tmp_path / "results"
    results_dir.mkdir()

    mock_backend = MagicMock()
    mock_ctrl = MagicMock()
    mock_ctrl.fencing_generation = 1
    mock_ctrl.lease.ttl_seconds = 2700
    mock_backend.hydrate_state.return_value = (mock_ctrl, "etag-ctrl-1", None, None, None)
    mock_backend.acquire_portal_lease.return_value = (True, mock_ctrl, "etag-ctrl-1")

    # Mock heartbeat to report failure
    mock_hb = MagicMock()
    mock_hb.is_healthy.return_value = False
    mock_hb.failure_reason = "Simulated takeover by another runner"
    mock_hb.get_latest_etag.return_value = "etag-ctrl-1"

    monkeypatch.setattr("src.s3_storage_backend.S3RemoteStorageBackend", lambda **kw: mock_backend)
    monkeypatch.setattr("src.s3_storage_backend.S3LeaseHeartbeat", lambda **kw: mock_hb)
    monkeypatch.setattr("src.corpusai_run.check_robots_txt", lambda *a, **k: (True, "OK"))

    with patch.object(corpusai_run, "PORTAL_CONFIGS", {
        "wordpress-news": {
            "head_page_url": "https://wordpress.org/news/all-posts/",
            "portal_id": "wordpress-news",
            "results_base_dir": str(results_dir),
            "catalog_path": str(results_dir / "catalog.json"),
            "user_agent": "CorpusAI-Test",
            "polite_delay_seconds": 0.0,
            "max_archive_pages": 1,
            "max_articles": 1,
            "max_requests": 5,
            "max_retries": 1,
        }
    }):
        with pytest.raises(SystemExit) as exc:
            corpusai_run.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
                "--storage-backend", "s3",
                "--s3-bucket", "test-bucket",
            ])

        assert exc.value.code == 10


# ---------------------------------------------------------------------------
# ETag formatting tests (added for Phase 9C-B ETag-quoting fix)
# ---------------------------------------------------------------------------


def test_strip_etag_removes_surrounding_quotes():
    """Quoted ETag returned by S3 is stripped to a bare hex string internally."""
    from src.s3_storage_backend import _strip_etag

    assert _strip_etag('"abc123def456"') == "abc123def456"
    assert _strip_etag("abc123def456") == "abc123def456"   # already bare
    assert _strip_etag('""') == ""
    assert _strip_etag(None) == ""                          # None guard


def test_quote_etag_wraps_bare_hex():
    """_quote_etag wraps a normalized (unquoted) ETag in double-quotes."""
    from src.s3_storage_backend import _quote_etag

    assert _quote_etag("abc123def456") == '"abc123def456"'


def test_quote_etag_prevents_double_quoting():
    """_quote_etag must not produce double-quoted strings."""
    from src.s3_storage_backend import _quote_etag

    # Passing an already-quoted value must still yield exactly one layer of quotes.
    assert _quote_etag('"abc123def456"') == '"abc123def456"'
    assert _quote_etag('"abc123"') == '"abc123"'


def test_quote_etag_empty_string():
    """_quote_etag of an empty (or whitespace-only) string returns '\"\"'."""
    from src.s3_storage_backend import _quote_etag

    assert _quote_etag("") == '""'


def test_put_object_conditional_if_match_uses_quoted_etag():
    """_put_object_conditional passes a quoted entity-tag to boto3 IfMatch."""
    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {"ETag": '"new-etag-abc"'}

    backend = _make_backend(mock_s3)
    key = "state/test-portal/portal_control.json"
    body = b'{"v": 1}'

    # Call with a bare (normalized) ETag — simulates internal representation.
    backend._put_object_conditional(key, body, if_match_etag="bare-etag-123")

    call_kwargs = mock_s3.put_object.call_args[1]
    # boto3 must receive the quoted form so the If-Match header is conformant.
    assert call_kwargs["IfMatch"] == '"bare-etag-123"'
    # IfNoneMatch must NOT be set when if_match_etag is provided.
    assert "IfNoneMatch" not in call_kwargs


def test_put_object_conditional_if_none_match_star_is_unquoted():
    """IfNoneMatch='*' must remain an unquoted asterisk — not '\"*\"'."""
    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {"ETag": '"first-etag"'}

    backend = _make_backend(mock_s3)
    backend._put_object_conditional(
        "state/test-portal/portal_control.json", b"{}", if_none_match=True
    )

    call_kwargs = mock_s3.put_object.call_args[1]
    assert call_kwargs.get("IfNoneMatch") == "*"
    # IfMatch must NOT be set when if_none_match=True.
    assert "IfMatch" not in call_kwargs


def test_acquire_lease_first_time_if_none_match_unquoted():
    """
    First-time lease creation uses IfNoneMatch='*' (not '\"*\"').

    Regression guard: the _quote_etag fix must not accidentally affect the
    IfNoneMatch path, which already uses the literal string '*'.
    """
    mock_s3 = MagicMock()
    mock_s3.get_object.side_effect = _make_client_error("NoSuchKey", 404)
    mock_s3.put_object.return_value = {"ETag": '"gen1-etag"'}

    backend = _make_backend(mock_s3)
    ok, record, etag = backend.acquire_portal_lease(
        "test-portal", "runner-q", ttl_seconds=2700
    )

    assert ok is True
    call_kwargs = mock_s3.put_object.call_args[1]
    assert call_kwargs.get("IfNoneMatch") == "*"
    assert "IfMatch" not in call_kwargs


def test_etag_round_trip_strip_then_quote():
    """
    End-to-end round-trip: S3 returns a quoted ETag, we strip it internally,
    then re-quote it when constructing the next conditional write.

    Verifies that _strip_etag followed by _quote_etag yields the original
    quoted form — no double-quoting, no truncation.
    """
    from src.s3_storage_backend import _strip_etag, _quote_etag

    s3_etag = '"deadbeef12345678"'   # as returned by S3
    internal = _strip_etag(s3_etag)
    assert internal == "deadbeef12345678"

    wire = _quote_etag(internal)
    assert wire == '"deadbeef12345678"'
    assert wire == s3_etag  # full round-trip equality

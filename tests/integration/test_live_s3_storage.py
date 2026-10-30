"""
Phase 9C-B Live Integration Tests for S3RemoteStorageBackend.

GUARDS: These tests require explicit opt-in via environment variables.
They will NOT run in CI (tests.yml does not set CORPUSAI_STORAGE_LIVE_TEST).
They make real AWS S3 API calls and create/delete real objects.

Required environment variables:
  CORPUSAI_STORAGE_LIVE_TEST=1   — Must be set to '1' to enable these tests
  CORPUSAI_S3_BUCKET             — Target S3 bucket name
  CORPUSAI_S3_PREFIX             — S3 key prefix (use a disposable test prefix)

IAM permissions required:
  s3:PutObject, s3:GetObject, s3:DeleteObject, s3:ListObjectsV2
  on the target bucket.
"""
import os
import pytest

LIVE_TEST_ENABLED = os.environ.get("CORPUSAI_STORAGE_LIVE_TEST") == "1"
LIVE_TEST_BUCKET = os.environ.get("CORPUSAI_S3_BUCKET", "")
LIVE_TEST_PREFIX = os.environ.get("CORPUSAI_S3_PREFIX", "corpusai-live-test")

pytestmark = pytest.mark.skipif(
    not LIVE_TEST_ENABLED,
    reason="Live S3 integration tests require CORPUSAI_STORAGE_LIVE_TEST=1",
)


@pytest.fixture(scope="module")
def live_backend():
    """Create a live S3RemoteStorageBackend and clean up test objects after tests."""
    if not LIVE_TEST_BUCKET:
        pytest.skip("CORPUSAI_S3_BUCKET not set")
    from src.s3_storage_backend import S3RemoteStorageBackend
    import boto3
    backend = S3RemoteStorageBackend(bucket=LIVE_TEST_BUCKET, prefix=LIVE_TEST_PREFIX)
    yield backend
    # Teardown: delete all test objects
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=LIVE_TEST_BUCKET, Prefix=LIVE_TEST_PREFIX + "/"):
        objects = page.get("Contents", [])
        if objects:
            s3.delete_objects(
                Bucket=LIVE_TEST_BUCKET,
                Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
            )


def test_live_acquire_lease_and_release(live_backend):
    """Live: acquire a portal lease, verify record, release it."""
    ok, record, etag = live_backend.acquire_portal_lease("live-test-portal", "runner-live-1")
    assert ok
    assert record is not None
    assert record.fencing_generation >= 1
    assert etag is not None
    released = live_backend.release_portal_lease(
        "live-test-portal", "runner-live-1", record.fencing_generation, etag
    )
    assert released


def test_live_double_acquire_fails(live_backend):
    """Live: second acquire while first is active fails."""
    ok1, record1, etag1 = live_backend.acquire_portal_lease("live-double-portal", "runner-A")
    assert ok1
    ok2, _, _ = live_backend.acquire_portal_lease("live-double-portal", "runner-B")
    assert not ok2
    live_backend.release_portal_lease(
        "live-double-portal", "runner-A", record1.fencing_generation, etag1
    )


def test_live_stage_and_verify_artifacts(live_backend, tmp_path):
    """Live: upload a file, read it back, verify SHA-256."""
    import hashlib
    test_file = tmp_path / "test_artifact.bin"
    test_file.write_bytes(b"hello-s3-artifact-123")
    expected_sha256 = hashlib.sha256(b"hello-s3-artifact-123").hexdigest()

    staged = live_backend.stage_package_artifacts(
        "live-test-portal", "run-live-001", {"test_artifact.bin": str(test_file)}
    )
    assert "test_artifact.bin" in staged

    is_valid, mismatches = live_backend.verify_remote_artifacts(
        "live-test-portal", "run-live-001", {"test_artifact.bin": expected_sha256}
    )
    assert is_valid
    assert mismatches == []

"""
Phase 9C-B: AWS S3 Remote Storage Backend for CorpusAI.

Implements S3RemoteStorageBackend, a concrete implementation of RemoteStorageContract
that persists portal control records, catalogs, and package artifacts in AWS S3.

Key Semantics:
- All objects live under a configurable prefix: ``<prefix>/state/<portal_id>/`` and
  ``<prefix>/packages/<portal_id>/runs/<run_id>/``.
- Lease locking uses S3 conditional writes (``If-None-Match: *`` for first creation;
  ``If-Match: <etag>`` for conditional overwrite).  These are atomic at the S3 layer
  and provide the same CAS guarantees as the in-memory mock.
- ETags are stored and compared *without* surrounding double-quotes.  S3 always returns
  ETags wrapped in quotes (``"abc123"``); this backend strips them on every read and
  passes the bare hex string to ``IfMatch``.
- Fencing generation monotonically increases on every successful lease acquisition
  or takeover.  Baseline generation monotonically increases only when a new baseline
  is published via ``commit_baseline_and_catalog``.
- Verification reads actual bytes from S3 and computes SHA-256 locally; it does not
  rely on ETags or user-metadata alone.

Note: S3 ``If-Match`` / ``If-None-Match`` conditional writes are atomic at the point
of write (same guarantee as the MockRemoteStorageBackend).  The PreconditionFailed
(HTTP 412) response means another writer has already modified the object; the caller
should treat this as a CAS conflict and not retry blindly.
"""

import datetime
import hashlib
import json
import logging
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.remote_storage_contract import (
    BaselinePointer,
    LeaseInfo,
    PortalControlRecord,
    RemoteArtifactMetadata,
    RemoteQuarantineRecord,
    RemoteCasConflictError,
    RemoteLockError,
    RemoteStorageContract,
    RemoteStorageError,
)

import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transient error detection
# ---------------------------------------------------------------------------

TRANSIENT_CODES = frozenset([
    "ThrottlingException",
    "RequestThrottled",
    "Throttling",
    "ServiceUnavailable",
    "RequestTimeout",
    "InternalError",
    "SlowDown",
])


class RemoteConditionalConflictError(RemoteCasConflictError):
    """Raised when an operation encounters an HTTP 409 ConditionalRequestConflict."""
    pass


def _is_transient(e: ClientError) -> bool:
    """Return True if the ClientError represents a transient, retriable condition."""
    code = e.response["Error"]["Code"]
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    # 409 and 412 are conditional check conflicts, never transient network errors
    if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
        return False
    return code in TRANSIENT_CODES or status >= 500


def _strip_etag(raw: str) -> str:
    """Strip surrounding double-quotes from an S3 ETag to maintain a normalized internal form."""
    if raw is None:
        return ""
    return raw.strip('"')


def _quote_etag(etag: str) -> str:
    """Format a normalized ETag as an HTTP entity-tag with surrounding quotes for wire headers."""
    if not etag:
        return '""'
    stripped = etag.strip('"')
    return f'"{stripped}"'


class S3LeaseHeartbeat:
    """
    Background heartbeat thread that periodically renews the S3 portal lease.

    Guarantees:
    - Thread-safe coordination with the main runner via Lock.
    - If renewal fails (e.g. CAS conflict due to takeover, lease expiry, or auth error),
      sets `failed_event` to signal the runner immediately.
    - Updates `current_etag` on each successful renewal so subsequent renewals and
      commits use the fresh ETag.
    """

    def __init__(
        self,
        backend: "S3RemoteStorageBackend",
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        initial_etag: str,
        ttl_seconds: int = 2700,
        interval_seconds: Optional[float] = None,
    ) -> None:
        self.backend = backend
        self.portal_id = portal_id
        self.runner_id = runner_id
        self.fencing_generation = fencing_generation
        self.current_etag = initial_etag
        self.ttl_seconds = ttl_seconds
        # By default renew at ~1/3 of the TTL or every 60s, whichever is shorter
        self.interval_seconds = interval_seconds if interval_seconds is not None else min(60.0, ttl_seconds / 3.0)

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.failed_event = threading.Event()
        self.failure_reason: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background heartbeat thread."""
        self._thread = threading.Thread(
            target=self._run,
            name=f"S3LeaseHeartbeat-{self.portal_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        logger.info(
            "S3 lease heartbeat started for %s (interval=%.1fs, ttl=%ds)",
            self.portal_id,
            self.interval_seconds,
            self.ttl_seconds,
        )
        while not self.stop_event.wait(timeout=self.interval_seconds):
            if self.stop_event.is_set():
                break
            with self.lock:
                try:
                    success, new_etag = self.backend.renew_portal_lease(
                        portal_id=self.portal_id,
                        runner_id=self.runner_id,
                        fencing_generation=self.fencing_generation,
                        expected_control_etag=self.current_etag,
                        ttl_seconds=self.ttl_seconds,
                    )
                    if success and new_etag:
                        self.current_etag = new_etag
                        logger.debug("S3 lease renewed for %s (new_etag=%s)", self.portal_id, new_etag)
                    else:
                        self.failure_reason = "Lease renewal rejected by storage (takeover or CAS conflict)"
                        self.failed_event.set()
                        logger.error("S3 lease renewal failed for %s: %s", self.portal_id, self.failure_reason)
                        break
                except Exception as e:
                    self.failure_reason = f"Lease renewal raised exception: {e}"
                    self.failed_event.set()
                    logger.error("S3 lease renewal exception for %s: %s", self.portal_id, e)
                    break

    def stop(self) -> None:
        """Stop the heartbeat thread and join it."""
        self.stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def is_healthy(self) -> bool:
        """Return True if no heartbeat failure has occurred."""
        return not self.failed_event.is_set()

    def get_latest_etag(self) -> str:
        """Return the current control record ETag in a thread-safe manner."""
        with self.lock:
            return self.current_etag


class S3RemoteStorageBackend(RemoteStorageContract):
    """
    AWS S3 implementation of RemoteStorageContract.

    All state objects are stored under ``<bucket>/<prefix>/``.  When ``prefix``
    is empty the objects live directly at the bucket root.

    Parameters
    ----------
    bucket:
        S3 bucket name.
    prefix:
        Optional key prefix prepended to all object keys (no trailing slash).
    boto3_client:
        Optional pre-built boto3 S3 client.  When ``None`` a default client is
        created via ``boto3.client("s3")``.  Injecting a mock client is the
        recommended pattern for unit tests.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        boto3_client=None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") if prefix else ""
        self._s3 = boto3_client if boto3_client is not None else boto3.client("s3")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _key(self, path: str) -> str:
        """Build a full S3 key from a logical path."""
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def _control_key(self, portal_id: str) -> str:
        return self._key(f"state/{portal_id}/portal_control.json")

    def _catalog_key(self, portal_id: str) -> str:
        return self._key(f"state/{portal_id}/catalog.json")

    def _deposit_key(self, portal_id: str) -> str:
        return self._key(f"state/{portal_id}/deposit_registry.json")

    def _package_prefix(self, portal_id: str, run_id: str) -> str:
        return self._key(f"packages/{portal_id}/runs/{run_id}/")

    def _artifact_key(self, portal_id: str, run_id: str, rel_path: str) -> str:
        norm = rel_path.replace("\\", "/")
        return self._key(f"packages/{portal_id}/runs/{run_id}/{norm}")

    def _quarantine_key(self, portal_id: str, quarantine_id: str) -> str:
        return self._key(f"state/{portal_id}/quarantine/{quarantine_id}.json")

    def _retry_s3_op(self, op, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
        """
        Execute ``op(*args, **kwargs)`` with exponential-backoff retry for
        transient S3 errors.  Up to ``max_retries`` additional attempts are made
        after the first failure (total up to ``max_retries + 1`` calls).
        """
        for attempt in range(max_retries + 1):
            try:
                return op(*args, **kwargs)
            except ClientError as e:
                if _is_transient(e) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Transient S3 error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        max_retries,
                        delay,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise

    def _classify_client_error(self, e: ClientError) -> None:
        """Re-raise a ClientError as the appropriate RemoteStorageError subclass."""
        code = e.response["Error"]["Code"]
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if code == "ConditionalRequestConflict" or status == 409:
            raise RemoteConditionalConflictError(str(e)) from e
        if code == "PreconditionFailed" or status == 412:
            raise RemoteCasConflictError(str(e)) from e
        if code in ("403", "AccessDenied", "ForbiddenException") or status == 403:
            raise RemoteLockError(str(e)) from e
        raise RemoteStorageError(str(e)) from e

    def _get_object(self, key: str) -> Tuple[bytes, str]:
        """
        Get an object from S3.  Returns ``(body_bytes, etag)`` where etag has
        no surrounding quotes.

        Raises ClientError with ``NoSuchKey`` if the key does not exist.
        """
        resp = self._retry_s3_op(self._s3.get_object, Bucket=self.bucket, Key=key)
        raw_etag = resp["ETag"]
        etag = _strip_etag(raw_etag)
        body = resp["Body"].read()
        return body, etag

    def _put_object_conditional(
        self,
        key: str,
        body: bytes,
        *,
        if_none_match: bool = False,
        if_match_etag: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        content_type: str = "application/json",
    ) -> str:
        """
        Upload ``body`` to ``key`` with an optional conditional header.

        Returns the new ETag (stripped of quotes) on success.

        Raises:
            RemoteCasConflictError: if the conditional check fails (HTTP 412).
            RemoteLockError: on auth failures (403/AccessDenied).
            RemoteStorageError: on all other S3 errors.
        """
        kwargs: Dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if metadata:
            kwargs["Metadata"] = metadata
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        elif if_match_etag is not None:
            kwargs["IfMatch"] = _quote_etag(if_match_etag)

        try:
            resp = self._retry_s3_op(self._s3.put_object, **kwargs)
            raw_etag = resp.get("ETag", "")
            return _strip_etag(raw_etag)
        except ClientError as e:
            self._classify_client_error(e)

    def _put_object_unconditional(
        self,
        key: str,
        body: bytes,
        *,
        metadata: Optional[Dict[str, str]] = None,
        content_type: str = "application/json",
    ) -> str:
        """Upload ``body`` unconditionally.  Returns stripped ETag."""
        kwargs: Dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if metadata:
            kwargs["Metadata"] = metadata
        try:
            resp = self._retry_s3_op(self._s3.put_object, **kwargs)
            return _strip_etag(resp.get("ETag", ""))
        except ClientError as e:
            self._classify_client_error(e)

    # ------------------------------------------------------------------
    # acquire_portal_lease
    # ------------------------------------------------------------------

    def acquire_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[PortalControlRecord], Optional[str]]:
        """
        Atomically acquire the exclusive lease lock for ``portal_id``.

        On the first call the control record is created with ``fencing_generation=1``.
        On subsequent calls, if the existing lease is expired or absent, the
        ``fencing_generation`` is incremented and the lease is updated via
        ``IfMatch`` CAS.  If the CAS fails, ``(False, None, None)`` is returned.

        Returns (acquired, control_record, control_etag).
        """
        control_key = self._control_key(portal_id)
        now_dt = (
            datetime.datetime.now(datetime.timezone.utc)
            if current_time_iso is None
            else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        )
        now_iso = now_dt.isoformat()
        expires_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        expires_iso = expires_dt.isoformat()

        # Attempt to read existing control record
        try:
            body, current_etag = self._get_object(control_key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "NoSuchKey":
                self._classify_client_error(e)
            # First-time creation
            record = PortalControlRecord(
                portal_id=portal_id,
                control_version=1,
                fencing_generation=1,
                baseline_generation=0,
                lease=LeaseInfo(
                    lease_owner_id=runner_id,
                    acquired_at=now_iso,
                    renewed_at=now_iso,
                    expires_at=expires_iso,
                    ttl_seconds=ttl_seconds,
                ),
                updated_at=now_iso,
            )
            data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
            try:
                new_etag = self._put_object_conditional(
                    control_key, data_bytes, if_none_match=True
                )
                return True, record, new_etag
            except RemoteConditionalConflictError:
                # 409 Conflict: refresh authoritative state and determine if acquisition/takeover is safe
                logger.info("409 ConditionalRequestConflict on initial lease creation; refreshing state for %s", portal_id)
                try:
                    body, current_etag = self._get_object(control_key)
                except Exception:
                    return False, None, None
            except RemoteCasConflictError:
                # 412 PreconditionFailed: Another runner created the record first; signal caller
                return False, None, None

        # Record exists — check if we can take the lease
        record = PortalControlRecord.from_dict(json.loads(body.decode("utf-8")))
        if not record.lease.is_expired(now_iso):
            # Lease is actively held by someone else
            return False, record, current_etag

        # Lease is expired or vacant — perform takeover
        record.fencing_generation += 1
        record.control_version += 1
        record.lease = LeaseInfo(
            lease_owner_id=runner_id,
            acquired_at=now_iso,
            renewed_at=now_iso,
            expires_at=expires_iso,
            ttl_seconds=ttl_seconds,
        )
        record.updated_at = now_iso
        data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        try:
            new_etag = self._put_object_conditional(
                control_key, data_bytes, if_match_etag=current_etag
            )
            return True, record, new_etag
        except RemoteCasConflictError:
            return False, None, None

    # ------------------------------------------------------------------
    # renew_portal_lease
    # ------------------------------------------------------------------

    def renew_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
        ttl_seconds: int = 2700,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Renew the active lease.

        Does NOT advance ``fencing_generation`` or ``baseline_generation``.
        Returns ``(success, new_control_etag)``.
        """
        control_key = self._control_key(portal_id)

        try:
            body, current_etag = self._get_object(control_key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchKey":
                return False, None
            self._classify_client_error(e)

        # Stale ETag check
        if current_etag != expected_control_etag:
            return False, None

        record = PortalControlRecord.from_dict(json.loads(body.decode("utf-8")))

        # Ownership and generation guards
        if record.lease.lease_owner_id != runner_id:
            return False, None
        if record.fencing_generation != fencing_generation:
            return False, None

        now_dt = (
            datetime.datetime.now(datetime.timezone.utc)
            if current_time_iso is None
            else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        )
        now_iso = now_dt.isoformat()

        if record.lease.is_expired(now_iso):
            return False, None

        expires_dt = now_dt + datetime.timedelta(seconds=ttl_seconds)
        record.lease.renewed_at = now_iso
        record.lease.expires_at = expires_dt.isoformat()
        record.control_version += 1
        record.updated_at = now_iso
        # Invariant: fencing_generation and baseline_generation are NOT advanced

        data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        try:
            new_etag = self._put_object_conditional(
                control_key, data_bytes, if_match_etag=current_etag
            )
            return True, new_etag
        except RemoteConditionalConflictError:
            # 409 Conflict: refresh authoritative state to verify if lease is still ours
            logger.info("409 ConditionalRequestConflict on lease renewal; re-evaluating state for %s", portal_id)
            try:
                refreshed_body, refreshed_etag = self._get_object(control_key)
                refreshed_record = PortalControlRecord.from_dict(json.loads(refreshed_body.decode("utf-8")))
                if (
                    refreshed_record.lease.lease_owner_id == runner_id
                    and refreshed_record.fencing_generation == fencing_generation
                    and not refreshed_record.lease.is_expired(now_iso)
                ):
                    # Lease is still validly ours, attempt conditional update with refreshed ETag
                    refreshed_record.lease.renewed_at = now_iso
                    refreshed_record.lease.expires_at = expires_dt.isoformat()
                    refreshed_record.control_version += 1
                    refreshed_record.updated_at = now_iso
                    retry_bytes = json.dumps(refreshed_record.to_dict(), indent=2).encode("utf-8")
                    retry_etag = self._put_object_conditional(
                        control_key, retry_bytes, if_match_etag=refreshed_etag
                    )
                    return True, retry_etag
            except Exception:
                pass
            return False, None
        except RemoteCasConflictError:
            # 412: Stale ETag or another writer updated record; never blindly replay
            return False, None

    # ------------------------------------------------------------------
    # release_portal_lease
    # ------------------------------------------------------------------

    def release_portal_lease(
        self,
        portal_id: str,
        runner_id: str,
        fencing_generation: int,
        expected_control_etag: str,
    ) -> bool:
        """
        Vacate the lease by writing ``lease_owner_id=None`` and zeroing expiry.

        Preserves ``fencing_generation``, ``baseline_generation``, and the active
        baseline pointer.  Returns ``False`` on CAS conflict (stale release is a
        silent no-op, NOT an exception).
        """
        control_key = self._control_key(portal_id)

        try:
            body, current_etag = self._get_object(control_key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchKey":
                return False
            self._classify_client_error(e)

        if current_etag != expected_control_etag:
            return False

        record = PortalControlRecord.from_dict(json.loads(body.decode("utf-8")))

        if record.lease.lease_owner_id != runner_id:
            return False
        if record.fencing_generation != fencing_generation:
            return False

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record.lease = LeaseInfo(
            lease_owner_id=None,
            acquired_at=None,
            renewed_at=None,
            expires_at="1970-01-01T00:00:00Z",
            ttl_seconds=record.lease.ttl_seconds,
        )
        record.control_version += 1
        record.updated_at = now_iso

        data_bytes = json.dumps(record.to_dict(), indent=2).encode("utf-8")
        try:
            self._put_object_conditional(
                control_key, data_bytes, if_match_etag=current_etag
            )
            return True
        except RemoteCasConflictError:
            return False

    # ------------------------------------------------------------------
    # hydrate_state
    # ------------------------------------------------------------------

    def hydrate_state(
        self,
        portal_id: str,
        target_dir: str,
    ) -> Tuple[
        Optional[PortalControlRecord],
        Optional[str],
        Optional[ArchiveCatalog],
        Optional[str],
        Optional[Dict[str, Any]],
    ]:
        """
        Download portal_control.json, catalog.json, baseline packages, and
        deposit_registry.json into ``target_dir``.

        The control record is strictly authoritative: if its
        ``baseline.latest_successful_run_id`` differs from the catalog's
        ``latest_successful_run_id``, the local catalog is updated to match.

        Returns (control_record, control_etag, catalog, catalog_etag, deposit_data).
        """
        os.makedirs(target_dir, exist_ok=True)

        control_record: Optional[PortalControlRecord] = None
        control_etag: Optional[str] = None
        catalog: Optional[ArchiveCatalog] = None
        catalog_etag: Optional[str] = None
        deposit_data: Optional[Dict[str, Any]] = None

        # --- Control record ---
        control_key = self._control_key(portal_id)
        try:
            body, control_etag = self._get_object(control_key)
            c_dict = json.loads(body.decode("utf-8"))
            local_control_path = os.path.join(target_dir, "portal_control.json")
            with open(local_control_path, "w", encoding="utf-8") as f:
                json.dump(c_dict, f, indent=2)
            control_record = PortalControlRecord.from_dict(c_dict)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "NoSuchKey":
                self._classify_client_error(e)
            # No control record yet — fresh portal

        # --- Catalog ---
        catalog_key = self._catalog_key(portal_id)
        try:
            body, catalog_etag = self._get_object(catalog_key)
            cat_dict = json.loads(body.decode("utf-8"))
            local_cat_path = os.path.join(target_dir, "catalog.json")
            with open(local_cat_path, "w", encoding="utf-8") as f:
                json.dump(cat_dict, f, indent=2)
            catalog = ArchiveCatalog.from_dict(cat_dict)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "NoSuchKey":
                self._classify_client_error(e)

        # --- Enforce control record as authoritative over catalog ---
        active_baseline_id: Optional[str] = None
        if control_record and control_record.baseline.latest_successful_run_id:
            active_baseline_id = control_record.baseline.latest_successful_run_id
            if catalog and catalog.latest_successful_run_id != active_baseline_id:
                catalog.latest_successful_run_id = active_baseline_id
                local_cat_path = os.path.join(target_dir, "catalog.json")
                catalog.save_to_json(local_cat_path)

        # --- Download baseline package artifacts ---
        if active_baseline_id:
            pkg_prefix = self._package_prefix(portal_id, active_baseline_id)
            try:
                paginator = self._s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=self.bucket, Prefix=pkg_prefix):
                    for obj_info in page.get("Contents", []):
                        obj_key = obj_info["Key"]
                        rel_file = obj_key[len(pkg_prefix):]
                        if not rel_file:
                            continue
                        dst_path = os.path.join(
                            target_dir,
                            "packages",
                            portal_id,
                            "runs",
                            active_baseline_id,
                            rel_file,
                        )
                        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                        try:
                            file_body, _ = self._get_object(obj_key)
                            with open(dst_path, "wb") as f:
                                f.write(file_body)
                        except ClientError as e2:
                            code2 = e2.response["Error"]["Code"]
                            if code2 != "NoSuchKey":
                                logger.warning(
                                    "hydrate_state: failed to download artifact %s: %s",
                                    obj_key,
                                    e2,
                                )
            except ClientError as e:
                logger.warning(
                    "hydrate_state: failed to list baseline artifacts for run %s: %s",
                    active_baseline_id,
                    e,
                )

        # --- Deposit registry ---
        deposit_key = self._deposit_key(portal_id)
        try:
            body, _ = self._get_object(deposit_key)
            deposit_data = json.loads(body.decode("utf-8"))
            local_dep_path = os.path.join(target_dir, "deposit_registry.json")
            with open(local_dep_path, "w", encoding="utf-8") as f:
                json.dump(deposit_data, f, indent=2)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "NoSuchKey":
                logger.warning("hydrate_state: error fetching deposit_registry: %s", e)

        return control_record, control_etag, catalog, catalog_etag, deposit_data

    # ------------------------------------------------------------------
    # stage_package_artifacts
    # ------------------------------------------------------------------

    def stage_package_artifacts(
        self,
        portal_id: str,
        run_id: str,
        local_files: Dict[str, str],
    ) -> Dict[str, RemoteArtifactMetadata]:
        """
        Upload local package files to S3 under
        ``packages/<portal_id>/runs/<run_id>/``.

        Computes SHA-256 for each file and stores it in ``x-amz-meta-sha256``
        metadata.  Returns a dict of ``rel_path -> RemoteArtifactMetadata``.

        Raises RemoteStorageError on network or non-CAS errors.
        """
        result: Dict[str, RemoteArtifactMetadata] = {}
        for rel_path, local_abs_path in local_files.items():
            norm_rel = rel_path.replace("\\", "/")
            s3_key = self._artifact_key(portal_id, run_id, norm_rel)

            try:
                with open(local_abs_path, "rb") as fh:
                    file_bytes = fh.read()
            except OSError as e:
                raise RemoteStorageError(
                    f"Cannot read local artifact '{local_abs_path}': {e}"
                ) from e

            sha256 = hashlib.sha256(file_bytes).hexdigest()
            meta = {"sha256": sha256}

            try:
                resp = self._retry_s3_op(
                    self._s3.put_object,
                    Bucket=self.bucket,
                    Key=s3_key,
                    Body=file_bytes,
                    ContentType="application/octet-stream",
                    Metadata=meta,
                )
                etag = _strip_etag(resp.get("ETag", ""))
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("403", "AccessDenied", "ForbiddenException"):
                    raise RemoteLockError(str(e)) from e
                raise RemoteStorageError(
                    f"S3 error uploading artifact '{norm_rel}': {e}"
                ) from e

            result[norm_rel] = RemoteArtifactMetadata(
                key=s3_key,
                size=len(file_bytes),
                etag=etag,
                sha256=sha256,
                content_type="application/octet-stream",
                metadata=meta,
            )

        return result

    # ------------------------------------------------------------------
    # verify_remote_artifacts
    # ------------------------------------------------------------------

    def verify_remote_artifacts(
        self,
        portal_id: str,
        run_id: str,
        expected_checksums: Dict[str, str],
    ) -> Tuple[bool, List[str]]:
        """
        Verify uploaded artifacts by reading the full byte stream and computing
        SHA-256 locally.

        User metadata (``x-amz-meta-sha256``) is checked as a sanity hint, but
        the authoritative proof is always the actual byte hash.

        Returns ``(is_valid, list_of_mismatch_descriptions)``.
        """
        mismatches: List[str] = []
        for rel_path, expected_sha256 in expected_checksums.items():
            norm_rel = rel_path.replace("\\", "/")
            s3_key = self._artifact_key(portal_id, run_id, norm_rel)

            try:
                resp = self._retry_s3_op(
                    self._s3.get_object, Bucket=self.bucket, Key=s3_key
                )
                body = resp["Body"].read()
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "NoSuchKey":
                    mismatches.append(f"Missing remote artifact: {norm_rel}")
                    continue
                mismatches.append(
                    f"S3 error reading artifact {norm_rel}: {e.response['Error']['Code']}"
                )
                continue

            actual_sha256 = hashlib.sha256(body).hexdigest()
            if actual_sha256 != expected_sha256:
                mismatches.append(
                    f"Checksum mismatch for {norm_rel}: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )

        return len(mismatches) == 0, mismatches

    # ------------------------------------------------------------------
    # quarantine_failed_upload
    # ------------------------------------------------------------------

    def quarantine_failed_upload(
        self,
        portal_id: str,
        run_id: str,
        runner_id: str,
        reason: str,
        details: Dict[str, Any],
    ) -> str:
        """
        Write a non-destructive quarantine manifest to
        ``state/<portal_id>/quarantine/<quarantine_id>.json``.

        Does NOT delete any package artifacts.  Does NOT raise on failure;
        instead logs a warning and returns the quarantine ID regardless.
        """
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_id = f"quarantine-{run_id}-{timestamp}"
        rec = RemoteQuarantineRecord(
            quarantine_id=quarantine_id,
            portal_id=portal_id,
            run_id=run_id,
            reason=reason,
            detected_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            runner_id=runner_id,
            details=details,
        )
        q_key = self._quarantine_key(portal_id, quarantine_id)
        data_bytes = json.dumps(rec.to_dict(), indent=2).encode("utf-8")
        try:
            self._retry_s3_op(
                self._s3.put_object,
                Bucket=self.bucket,
                Key=q_key,
                Body=data_bytes,
                ContentType="application/json",
            )
        except ClientError as e:
            logger.warning(
                "quarantine_failed_upload: failed to write quarantine record %s: %s",
                quarantine_id,
                e,
            )
        return quarantine_id

    # ------------------------------------------------------------------
    # commit_baseline_and_catalog
    # ------------------------------------------------------------------

    def commit_baseline_and_catalog(
        self,
        portal_id: str,
        run_id: str,
        catalog: ArchiveCatalog,
        expected_catalog_etag: Optional[str],
        expected_control_etag: str,
        fencing_generation: int,
        runner_id: str,
        current_time_iso: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Atomically publish a new baseline.

        Steps:
        1. Fetch and verify control record (ETag, ownership, fencing generation).
        2. Commit catalog with conditional write.
        3. Update control record with conditional write (advancing baseline_generation).

        If the catalog write succeeds but the control write fails, this method
        returns ``(False, None, None)`` without rolling back the catalog write
        (S3 has no transactions).  The caller must treat this as a conflict.

        Returns ``(success, new_control_etag, new_catalog_etag)``.
        """
        control_key = self._control_key(portal_id)
        catalog_key = self._catalog_key(portal_id)

        # Step 1: Verify control record
        try:
            ctrl_body, current_ctrl_etag = self._get_object(control_key)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NoSuchKey":
                return False, None, None
            self._classify_client_error(e)

        if current_ctrl_etag != expected_control_etag:
            return False, None, None

        ctrl_record = PortalControlRecord.from_dict(
            json.loads(ctrl_body.decode("utf-8"))
        )
        if ctrl_record.lease.lease_owner_id != runner_id:
            return False, None, None
        if ctrl_record.fencing_generation != fencing_generation:
            return False, None, None

        # Step 2: Commit catalog (conditional)
        now_dt = (
            datetime.datetime.now(datetime.timezone.utc)
            if current_time_iso is None
            else datetime.datetime.fromisoformat(current_time_iso.replace("Z", "+00:00"))
        )
        now_iso = now_dt.isoformat()

        cat_bytes = json.dumps(catalog.to_dict(), indent=2).encode("utf-8")
        try:
            if expected_catalog_etag is None:
                new_cat_etag = self._put_object_conditional(
                    catalog_key, cat_bytes, if_none_match=True
                )
            else:
                new_cat_etag = self._put_object_conditional(
                    catalog_key, cat_bytes, if_match_etag=expected_catalog_etag
                )
        except RemoteCasConflictError:
            return False, None, None

        # Step 3: Update control record (advancing baseline_generation only)
        ctrl_record.control_version += 1
        ctrl_record.baseline_generation += 1
        ctrl_record.baseline = BaselinePointer(
            latest_successful_run_id=run_id,
            committed_at=now_iso,
            committed_by_generation=fencing_generation,
            baseline_generation=ctrl_record.baseline_generation,
            manifest_path=f"packages/{portal_id}/runs/{run_id}/manifest.json",
            checksums_path=f"packages/{portal_id}/runs/{run_id}/checksums.sha256",
        )
        ctrl_record.catalog_head_etag = new_cat_etag
        ctrl_record.updated_at = now_iso

        ctrl_bytes = json.dumps(ctrl_record.to_dict(), indent=2).encode("utf-8")
        try:
            new_ctrl_etag = self._put_object_conditional(
                control_key, ctrl_bytes, if_match_etag=expected_control_etag
            )
        except RemoteCasConflictError:
            # Catalog was already written; cannot roll back.  Caller must reconcile.
            return False, None, None
        except Exception as e:
            # Ambiguous publication outcome: network timeout/socket error after S3 may have processed write
            logger.warning("Ambiguous outcome on control record write for %s: %s; re-reading authoritative state", portal_id, e)
            try:
                verify_body, verify_etag = self._get_object(control_key)
                verified_record = PortalControlRecord.from_dict(json.loads(verify_body.decode("utf-8")))
                if (
                    verified_record.baseline.latest_successful_run_id == run_id
                    and verified_record.baseline.committed_by_generation == fencing_generation
                ):
                    logger.info("Authoritative verification confirmed commit succeeded despite network ambiguity")
                    return True, verify_etag, new_cat_etag
            except Exception as reread_e:
                logger.error("Failed to re-read control record after ambiguous write: %s", reread_e)
            return False, None, None

        return True, new_ctrl_etag, new_cat_etag

    # ------------------------------------------------------------------
    # project_control_to_catalog
    # ------------------------------------------------------------------

    def project_control_to_catalog(
        self,
        portal_id: str,
        control_record: PortalControlRecord,
        verified_run_entry: CatalogRunEntry,
        expected_catalog_etag: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Project an already-committed baseline from portal_control.json onto catalog.json.

        Enforces strict non-regression: if the catalog already points to a newer run
        (started_at timestamp is more recent), the projection is refused.

        Returns ``(success, new_or_current_catalog_etag)``.
        """
        catalog_key = self._catalog_key(portal_id)

        # Fetch current catalog
        current_cat_etag: Optional[str] = None
        cat_dict: Dict[str, Any] = {"portal_id": portal_id, "runs": {}}
        try:
            body, current_cat_etag = self._get_object(catalog_key)
            cat_dict = json.loads(body.decode("utf-8"))
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code != "NoSuchKey":
                self._classify_client_error(e)

        # ETag guard
        if expected_catalog_etag is not None and expected_catalog_etag != current_cat_etag:
            return False, current_cat_etag

        catalog = ArchiveCatalog.from_dict(cat_dict)
        target_baseline_id = control_record.baseline.latest_successful_run_id
        if not target_baseline_id:
            return False, current_cat_etag

        # Non-regression invariant
        if catalog.latest_successful_run_id and catalog.latest_successful_run_id != target_baseline_id:
            if catalog.latest_successful_run_id in catalog.runs:
                curr_entry = catalog.runs[catalog.latest_successful_run_id]
                if curr_entry.started_at > verified_run_entry.started_at:
                    return False, current_cat_etag

        catalog.register_run(verified_run_entry, is_successful=True)
        catalog.latest_successful_run_id = target_baseline_id

        cat_bytes = json.dumps(catalog.to_dict(), indent=2).encode("utf-8")
        try:
            if current_cat_etag is None:
                new_etag = self._put_object_conditional(
                    catalog_key, cat_bytes, if_none_match=True
                )
            else:
                new_etag = self._put_object_conditional(
                    catalog_key, cat_bytes, if_match_etag=current_cat_etag
                )
            return True, new_etag
        except RemoteCasConflictError:
            return False, current_cat_etag

    # ------------------------------------------------------------------
    # sync_deposit_registry
    # ------------------------------------------------------------------

    def sync_deposit_registry(
        self,
        portal_id: str,
        registry_data: Dict[str, Any],
    ) -> bool:
        """
        Upload deposit_registry.json unconditionally (last-writer-wins).

        Returns ``True`` on success, ``False`` on failure.
        """
        dep_key = self._deposit_key(portal_id)
        data_bytes = json.dumps(registry_data, indent=2).encode("utf-8")
        try:
            self._put_object_unconditional(dep_key, data_bytes)
            return True
        except (RemoteStorageError, RemoteLockError) as e:
            logger.warning("sync_deposit_registry failed for portal %s: %s", portal_id, e)
            return False
        except ClientError as e:
            logger.warning("sync_deposit_registry S3 error for portal %s: %s", portal_id, e)
            return False

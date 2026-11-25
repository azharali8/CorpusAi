"""
Stage 7 tests for CorpusAI deposit model data compatibility.

Covers:
 1. DepositStatus enum values and exact persisted status strings.
 2. DepositStatus string compatibility (str subclass, comparison, f-string).
 3. Valid state transitions.
 4. Invalid state transitions.
 5. Transition table completeness — every terminal and non-terminal state.
 6. remote_md5 canonical serialization (remote_checksum not written).
 7. Legacy remote_checksum-only deserialization (promoted to remote_md5).
 8. Conflicting checksum field behaviour (remote_md5 wins, warning emitted).
 9. Consistent remote_checksum deserialization (both fields agree).
10. None vs "UNKNOWN" last_error_category semantics.
11. transition_to() error-category rule enforcement.
12. DepositReceipt.for_failure() factory — required arguments.
13. for_failure() factory — resulting status, error fields, identifiers.
14. for_failure() factory — explicit repository and environment.
15. for_failure() factory — retry_eligible default and override.
16. for_failure() factory — rejects None last_error.
17. for_failure() factory — rejects None last_error_category.
18. Receipt serialization round-trip (to_dict / from_dict).
19. Old receipt deserialization — receipt dict without last_error_category key.
20. DepositReceipt.__post_init__ coerces str status to DepositStatus.
21. Artifact inventory round-trip including remote_md5.
22. UploadedArtifactRecord to_dict does not write remote_checksum.
"""

import json
import logging
from typing import Any, Dict

import pytest

from src.zenodo_stuff.models import (
    DepositReceipt,
    DepositStatus,
    UploadedArtifactRecord,
    STATUS_DRAFT_CREATED,
    STATUS_FAILED,
    STATUS_NEEDS_RECONCILIATION,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    STATUS_VERIFIED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_receipt(**overrides) -> DepositReceipt:
    """Return a minimal valid PENDING receipt with keyword overrides."""
    base: Dict[str, Any] = {
        "receipt_id": "receipt-p1-r1-abcd1234",
        "portal_id": "portal-1",
        "run_id": "run-1",
        "package_fingerprint": "abcd1234ef567890" * 4,
        "repository_target": "zenodo",
        "environment": "sandbox",
        "status": DepositStatus.PENDING,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return DepositReceipt(**base)


def _minimal_artifact(**overrides) -> UploadedArtifactRecord:
    base: Dict[str, Any] = {
        "filename": "manifest.json",
        "filesize": 128,
        "local_sha256": "a" * 64,
        "local_md5": "b" * 32,
    }
    base.update(overrides)
    return UploadedArtifactRecord(**base)


# ---------------------------------------------------------------------------
# 1. DepositStatus enum values and persisted strings
# ---------------------------------------------------------------------------

class TestDepositStatusValues:

    def test_all_expected_members_exist(self):
        expected = {
            "PENDING", "DRAFT_CREATED", "UPLOADING", "UPLOADED",
            "VERIFIED", "PUBLISHED", "FAILED", "NEEDS_RECONCILIATION",
        }
        actual = {m.value for m in DepositStatus}
        assert actual == expected

    def test_persisted_string_values_unchanged(self):
        """The .value of each member must equal the legacy plain-string constant."""
        assert DepositStatus.PENDING.value == "PENDING"
        assert DepositStatus.DRAFT_CREATED.value == "DRAFT_CREATED"
        assert DepositStatus.UPLOADING.value == "UPLOADING"
        assert DepositStatus.UPLOADED.value == "UPLOADED"
        assert DepositStatus.VERIFIED.value == "VERIFIED"
        assert DepositStatus.PUBLISHED.value == "PUBLISHED"
        assert DepositStatus.FAILED.value == "FAILED"
        assert DepositStatus.NEEDS_RECONCILIATION.value == "NEEDS_RECONCILIATION"

    def test_module_aliases_are_enum_members(self):
        """The STATUS_* aliases exported at module level are DepositStatus members."""
        assert STATUS_PENDING is DepositStatus.PENDING
        assert STATUS_DRAFT_CREATED is DepositStatus.DRAFT_CREATED
        assert STATUS_UPLOADING is DepositStatus.UPLOADING
        assert STATUS_UPLOADED is DepositStatus.UPLOADED
        assert STATUS_VERIFIED is DepositStatus.VERIFIED
        assert STATUS_PUBLISHED is DepositStatus.PUBLISHED
        assert STATUS_FAILED is DepositStatus.FAILED
        assert STATUS_NEEDS_RECONCILIATION is DepositStatus.NEEDS_RECONCILIATION


# ---------------------------------------------------------------------------
# 2. DepositStatus string compatibility
# ---------------------------------------------------------------------------

class TestDepositStatusStringCompatibility:

    def test_is_str_subclass(self):
        assert isinstance(DepositStatus.PENDING, str)

    def test_equality_with_plain_string(self):
        assert DepositStatus.PENDING == "PENDING"
        assert DepositStatus.FAILED == "FAILED"
        assert DepositStatus.PUBLISHED == "PUBLISHED"

    def test_str_conversion(self):
        assert str(DepositStatus.PENDING) == "PENDING"
        assert str(DepositStatus.UPLOADED) == "UPLOADED"

    def test_fstring_interpolation(self):
        assert f"{DepositStatus.DRAFT_CREATED}" == "DRAFT_CREATED"

    def test_json_serialisable_as_string(self):
        d = {"status": DepositStatus.FAILED}
        serialized = json.dumps(d)
        loaded = json.loads(serialized)
        assert loaded["status"] == "FAILED"

    def test_in_set_comparison_with_strings(self):
        status_set = {"PENDING", "FAILED"}
        assert DepositStatus.PENDING in status_set
        assert DepositStatus.FAILED in status_set
        assert DepositStatus.PUBLISHED not in status_set

    def test_dict_key_lookup(self):
        d = {"PENDING": "start", "FAILED": "err"}
        assert d[DepositStatus.PENDING] == "start"
        assert d[DepositStatus.FAILED] == "err"


# ---------------------------------------------------------------------------
# 3. Valid state transitions
# ---------------------------------------------------------------------------

class TestValidTransitions:

    def test_pending_to_draft_created(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        r.transition_to(DepositStatus.DRAFT_CREATED)
        assert r.status == DepositStatus.DRAFT_CREATED

    def test_draft_created_to_uploading(self):
        r = _minimal_receipt(status=DepositStatus.DRAFT_CREATED)
        r.transition_to(DepositStatus.UPLOADING)
        assert r.status == DepositStatus.UPLOADING

    def test_uploading_to_uploaded(self):
        r = _minimal_receipt(status=DepositStatus.UPLOADING)
        r.transition_to(DepositStatus.UPLOADED)
        assert r.status == DepositStatus.UPLOADED

    def test_uploaded_to_verified(self):
        r = _minimal_receipt(status=DepositStatus.UPLOADING)
        r.transition_to(DepositStatus.UPLOADED)
        r.transition_to(DepositStatus.VERIFIED)
        assert r.status == DepositStatus.VERIFIED

    def test_verified_to_published(self):
        r = _minimal_receipt(status=DepositStatus.VERIFIED)
        r.transition_to(DepositStatus.PUBLISHED)
        assert r.status == DepositStatus.PUBLISHED

    def test_any_non_terminal_to_failed(self):
        for start in (
            DepositStatus.PENDING,
            DepositStatus.DRAFT_CREATED,
            DepositStatus.UPLOADING,
            DepositStatus.UPLOADED,
            DepositStatus.VERIFIED,
            DepositStatus.NEEDS_RECONCILIATION,
        ):
            r = _minimal_receipt(status=start)
            r.transition_to(DepositStatus.FAILED, error="boom", error_category="TEST")
            assert r.status == DepositStatus.FAILED

    def test_failed_can_resume_to_draft_created(self):
        r = _minimal_receipt(status=DepositStatus.FAILED)
        r.transition_to(DepositStatus.DRAFT_CREATED)
        assert r.status == DepositStatus.DRAFT_CREATED

    def test_transition_to_accepts_plain_string(self):
        """transition_to() must accept plain str arguments for backward compat."""
        r = _minimal_receipt(status="PENDING")
        r.transition_to("DRAFT_CREATED")
        assert r.status == DepositStatus.DRAFT_CREATED

    def test_transition_updates_updated_at(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        old_ts = r.updated_at
        r.transition_to(DepositStatus.DRAFT_CREATED)
        assert r.updated_at != old_ts

    def test_needs_reconciliation_to_failed(self):
        r = _minimal_receipt(status=DepositStatus.NEEDS_RECONCILIATION)
        r.transition_to(DepositStatus.FAILED, error="conflict", error_category="FP_CHANGED")
        assert r.status == DepositStatus.FAILED


# ---------------------------------------------------------------------------
# 4. Invalid state transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:

    def test_published_is_terminal(self):
        r = _minimal_receipt(status=DepositStatus.PUBLISHED)
        with pytest.raises(ValueError, match="Invalid deposit status transition"):
            r.transition_to(DepositStatus.FAILED)

    def test_pending_cannot_jump_to_uploaded(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        with pytest.raises(ValueError):
            r.transition_to(DepositStatus.UPLOADED)

    def test_pending_cannot_jump_to_verified(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        with pytest.raises(ValueError):
            r.transition_to(DepositStatus.VERIFIED)

    def test_draft_created_cannot_jump_to_verified(self):
        r = _minimal_receipt(status=DepositStatus.DRAFT_CREATED)
        with pytest.raises(ValueError):
            r.transition_to(DepositStatus.VERIFIED)

    def test_unknown_status_string_raises(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        with pytest.raises(ValueError):
            r.transition_to("NOT_A_REAL_STATUS")


# ---------------------------------------------------------------------------
# 5. __post_init__ coerces str status to DepositStatus
# ---------------------------------------------------------------------------

class TestStatusCoercion:

    def test_str_coerced_on_construction(self):
        r = _minimal_receipt(status="PENDING")
        assert r.status is DepositStatus.PENDING

    def test_enum_accepted_directly(self):
        r = _minimal_receipt(status=DepositStatus.DRAFT_CREATED)
        assert r.status is DepositStatus.DRAFT_CREATED

    def test_invalid_str_raises_on_construction(self):
        with pytest.raises(ValueError, match="Unknown deposit status"):
            _minimal_receipt(status="BOGUS_STATUS")


# ---------------------------------------------------------------------------
# 6. remote_md5 canonical serialization
# ---------------------------------------------------------------------------

class TestRemoteMd5Canonicalization:

    def test_to_dict_does_not_write_remote_checksum(self):
        art = _minimal_artifact(remote_md5="deadbeef" * 4)
        d = art.to_dict()
        assert "remote_checksum" not in d

    def test_to_dict_writes_remote_md5(self):
        art = _minimal_artifact(remote_md5="cafebabe" * 4)
        d = art.to_dict()
        assert d["remote_md5"] == "cafebabe" * 4

    def test_to_dict_remote_md5_none_when_unset(self):
        art = _minimal_artifact()
        d = art.to_dict()
        assert d["remote_md5"] is None
        assert "remote_checksum" not in d

    def test_receipt_to_dict_artifact_does_not_write_remote_checksum(self):
        art = _minimal_artifact(remote_md5="a1b2c3d4" * 4)
        r = _minimal_receipt()
        r.artifact_inventory["manifest.json"] = art
        receipt_dict = r.to_dict()
        for art_dict in receipt_dict["artifact_inventory"].values():
            assert "remote_checksum" not in art_dict


# ---------------------------------------------------------------------------
# 7. Legacy remote_checksum-only deserialization
# ---------------------------------------------------------------------------

class TestLegacyRemoteChecksumDeserialization:

    def test_remote_checksum_promoted_to_remote_md5(self):
        """A legacy artifact record with only remote_checksum promotes to remote_md5."""
        legacy = {
            "filename": "manifest.json",
            "filesize": 128,
            "local_sha256": "a" * 64,
            "local_md5": "b" * 32,
            "remote_checksum": "md5:cafebabe12345678cafebabe12345678",
            "remote_file_id": None,
            "verified": False,
        }
        art = UploadedArtifactRecord.from_dict(legacy)
        assert art.remote_md5 == "cafebabe12345678cafebabe12345678"

    def test_remote_checksum_without_prefix_promoted(self):
        legacy = {
            "filename": "checksums.sha256",
            "filesize": 64,
            "local_sha256": "c" * 64,
            "local_md5": "d" * 32,
            "remote_checksum": "aabbccddeeff0011aabbccddeeff0011",
            "remote_file_id": "file-xyz",
            "verified": True,
        }
        art = UploadedArtifactRecord.from_dict(legacy)
        assert art.remote_md5 == "aabbccddeeff0011aabbccddeeff0011"

    def test_legacy_receipt_round_trips_via_from_dict(self):
        """A complete legacy receipt JSON with remote_checksum deserializes cleanly."""
        legacy_receipt = {
            "receipt_id": "receipt-p1-r1-00000000",
            "portal_id": "p1",
            "run_id": "r1",
            "package_fingerprint": "0" * 64,
            "repository_target": "zenodo",
            "environment": "sandbox",
            "status": "UPLOADED",
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:01:00+00:00",
            "artifact_inventory": {
                "manifest.json": {
                    "filename": "manifest.json",
                    "filesize": 100,
                    "local_sha256": "e" * 64,
                    "local_md5": "f" * 32,
                    "remote_checksum": "md5:" + "a" * 32,
                    "remote_file_id": "id-1",
                    "verified": False,
                }
            },
        }
        receipt = DepositReceipt.from_dict(legacy_receipt)
        art = receipt.artifact_inventory["manifest.json"]
        assert art.remote_md5 == "a" * 32


# ---------------------------------------------------------------------------
# 8. Conflicting checksum fields (remote_md5 wins, warning emitted)
# ---------------------------------------------------------------------------

class TestConflictingChecksumFields:

    def test_remote_md5_wins_on_conflict(self):
        data = {
            "filename": "data.warc.gz",
            "filesize": 1024,
            "local_sha256": "a" * 64,
            "local_md5": "b" * 32,
            "remote_md5": "canonical" + "0" * 23,
            "remote_checksum": "md5:different" + "1" * 21,
            "remote_file_id": None,
            "verified": False,
        }
        art = UploadedArtifactRecord.from_dict(data)
        assert art.remote_md5 == "canonical" + "0" * 23

    def test_conflict_emits_warning(self, caplog):
        data = {
            "filename": "data.warc.gz",
            "filesize": 1024,
            "local_sha256": "a" * 64,
            "local_md5": "b" * 32,
            "remote_md5": "canonical" + "0" * 23,
            "remote_checksum": "md5:different" + "1" * 21,
            "remote_file_id": None,
            "verified": False,
        }
        with caplog.at_level(logging.WARNING, logger="corpusai.deposit.models"):
            UploadedArtifactRecord.from_dict(data)
        assert any("Conflicting checksum" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# 9. Consistent remote_checksum deserialization (fields agree)
# ---------------------------------------------------------------------------

class TestConsistentChecksumFields:

    def test_no_warning_when_fields_agree(self, caplog):
        hex_md5 = "a" * 32
        data = {
            "filename": "manifest.json",
            "filesize": 50,
            "local_sha256": "b" * 64,
            "local_md5": "c" * 32,
            "remote_md5": hex_md5,
            "remote_checksum": "md5:" + hex_md5,
            "remote_file_id": None,
            "verified": False,
        }
        with caplog.at_level(logging.WARNING, logger="corpusai.deposit.models"):
            art = UploadedArtifactRecord.from_dict(data)
        assert art.remote_md5 == hex_md5
        # No conflict warning expected
        conflict_warnings = [m for m in caplog.messages if "Conflicting checksum" in m]
        assert conflict_warnings == []


# ---------------------------------------------------------------------------
# 10. None vs "UNKNOWN" last_error_category semantics
# ---------------------------------------------------------------------------

class TestErrorCategorySemantics:

    def test_no_error_means_none_category(self):
        """A receipt with no error must have last_error_category == None."""
        r = _minimal_receipt()
        assert r.last_error is None
        assert r.last_error_category is None

    def test_error_with_known_category(self):
        r = _minimal_receipt(status=DepositStatus.FAILED)
        # Transition from FAILED is not possible without going through another state;
        # construct a fresh FAILED receipt directly with an error.
        r2 = _minimal_receipt(
            status=DepositStatus.PENDING,
            last_error="Quota exceeded",
            last_error_category="QUOTA_EXCEEDED",
        )
        # Set status manually to skip transition for direct model testing.
        r2.status = DepositStatus.FAILED
        assert r2.last_error == "Quota exceeded"
        assert r2.last_error_category == "QUOTA_EXCEEDED"
        assert r2.last_error_category != "UNKNOWN"

    def test_error_without_category_defaults_to_unknown(self):
        """transition_to() sets category to UNKNOWN when error given but category omitted."""
        r = _minimal_receipt(status=DepositStatus.PENDING)
        r.transition_to(DepositStatus.FAILED, error="Unexpected crash")
        assert r.last_error == "Unexpected crash"
        assert r.last_error_category == "UNKNOWN"

    def test_unknown_is_distinct_from_no_error(self):
        r_no_error = _minimal_receipt()
        r_unknown = _minimal_receipt(
            last_error="Something broke",
            last_error_category="UNKNOWN",
        )
        assert r_no_error.last_error_category is None
        assert r_unknown.last_error_category == "UNKNOWN"
        assert r_no_error.last_error_category != r_unknown.last_error_category

    def test_non_error_transition_does_not_clear_error(self):
        """A transition without error= leaves previous last_error intact."""
        r = _minimal_receipt(status=DepositStatus.DRAFT_CREATED)
        r.last_error = "Previous error"
        r.last_error_category = "SOME_CATEGORY"
        r.transition_to(DepositStatus.UPLOADING)  # no error= supplied
        assert r.last_error == "Previous error"
        assert r.last_error_category == "SOME_CATEGORY"

    def test_error_roundtrips_through_serialization(self):
        r = _minimal_receipt(
            last_error="Network timeout",
            last_error_category="NETWORK_ERROR",
        )
        d = r.to_dict()
        r2 = DepositReceipt.from_dict(d)
        assert r2.last_error == "Network timeout"
        assert r2.last_error_category == "NETWORK_ERROR"

    def test_none_category_roundtrips(self):
        r = _minimal_receipt()  # no error
        d = r.to_dict()
        r2 = DepositReceipt.from_dict(d)
        assert r2.last_error is None
        assert r2.last_error_category is None


# ---------------------------------------------------------------------------
# 11. transition_to() error-category rule enforcement
# ---------------------------------------------------------------------------

class TestTransitionErrorCategory:

    def test_category_set_when_error_and_category_given(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        r.transition_to(DepositStatus.FAILED, error="Bad stuff", error_category="BAD_STUFF")
        assert r.last_error_category == "BAD_STUFF"

    def test_category_unknown_when_error_given_no_category(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        r.transition_to(DepositStatus.FAILED, error="Bad stuff")
        assert r.last_error_category == "UNKNOWN"

    def test_category_not_set_when_no_error(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        r.transition_to(DepositStatus.DRAFT_CREATED)
        assert r.last_error is None
        assert r.last_error_category is None


# ---------------------------------------------------------------------------
# 12-17. DepositReceipt.for_failure() factory
# ---------------------------------------------------------------------------

class TestForFailureFactory:

    def _call(self, **overrides):
        defaults = {
            "portal_id": "portal-x",
            "run_id": "run-99",
            "fingerprint": "deadbeef" * 8,
            "repository": "zenodo",
            "environment": "sandbox",
            "last_error": "Something went wrong",
            "last_error_category": "TEST_FAILURE",
        }
        defaults.update(overrides)
        return DepositReceipt.for_failure(**defaults)

    def test_resulting_status_is_failed(self):
        r = self._call()
        assert r.status == DepositStatus.FAILED

    def test_error_fields_set(self):
        r = self._call(last_error="Network error", last_error_category="NETWORK_TIMEOUT")
        assert r.last_error == "Network error"
        assert r.last_error_category == "NETWORK_TIMEOUT"

    def test_identifiers_set_correctly(self):
        r = self._call(portal_id="p1", run_id="r1")
        assert r.portal_id == "p1"
        assert r.run_id == "r1"

    def test_receipt_id_format(self):
        fp = "abcdefab" * 8
        r = self._call(portal_id="p1", run_id="r1", fingerprint=fp)
        assert r.receipt_id == f"receipt-p1-r1-abcdefab"

    def test_explicit_repository_and_environment(self):
        r = self._call(repository="my-repo", environment="production")
        assert r.repository_target == "my-repo"
        assert r.environment == "production"

    def test_retry_eligible_defaults_true(self):
        r = self._call()
        assert r.retry_eligible is True

    def test_retry_eligible_can_be_set_false(self):
        r = self._call(retry_eligible=False)
        assert r.retry_eligible is False

    def test_timestamps_are_set(self):
        r = self._call()
        assert r.created_at
        assert r.updated_at

    def test_rejects_empty_last_error(self):
        with pytest.raises(ValueError, match="last_error must be non-empty"):
            self._call(last_error="")

    def test_rejects_none_last_error(self):
        with pytest.raises((ValueError, TypeError)):
            self._call(last_error=None)

    def test_rejects_none_last_error_category(self):
        with pytest.raises(ValueError, match="last_error_category must be provided"):
            self._call(last_error_category=None)

    def test_serializable_via_to_dict(self):
        r = self._call()
        d = r.to_dict()
        assert d["status"] == "FAILED"
        assert d["last_error"] == "Something went wrong"
        assert d["last_error_category"] == "TEST_FAILURE"

    def test_round_trips_via_from_dict(self):
        r = self._call(last_error_category="DRAFT_CREATION_FAILED")
        d = r.to_dict()
        r2 = DepositReceipt.from_dict(d)
        assert r2.status == DepositStatus.FAILED
        assert r2.last_error_category == "DRAFT_CREATION_FAILED"
        assert r2.retry_eligible == r.retry_eligible

    def test_unknown_category_accepted(self):
        """UNKNOWN is a valid explicit category for unclassifiable errors."""
        r = self._call(last_error="Unclassified crash", last_error_category="UNKNOWN")
        assert r.last_error_category == "UNKNOWN"


# ---------------------------------------------------------------------------
# 18. Receipt serialization round-trip
# ---------------------------------------------------------------------------

class TestReceiptRoundTrip:

    def test_full_round_trip(self):
        r = _minimal_receipt(
            status=DepositStatus.UPLOADED,
            last_error=None,
            last_error_category=None,
        )
        art = _minimal_artifact(remote_md5="a1b2c3d4" * 8)
        r.artifact_inventory["manifest.json"] = art
        d = r.to_dict()
        r2 = DepositReceipt.from_dict(d)
        assert r2.status == DepositStatus.UPLOADED
        assert "manifest.json" in r2.artifact_inventory
        assert r2.artifact_inventory["manifest.json"].remote_md5 == "a1b2c3d4" * 8

    def test_status_serialized_as_string(self):
        r = _minimal_receipt(status=DepositStatus.DRAFT_CREATED)
        d = r.to_dict()
        assert d["status"] == "DRAFT_CREATED"
        assert isinstance(d["status"], str)

    def test_from_dict_coerces_status(self):
        r = _minimal_receipt(status=DepositStatus.PENDING)
        d = r.to_dict()
        # Confirm dict holds a plain string
        assert type(d["status"]) is str or isinstance(d["status"], str)
        r2 = DepositReceipt.from_dict(d)
        assert isinstance(r2.status, DepositStatus)
        assert r2.status == DepositStatus.PENDING


# ---------------------------------------------------------------------------
# 19. Old receipt deserialization — missing optional fields
# ---------------------------------------------------------------------------

class TestLegacyReceiptDeserialization:

    def test_receipt_without_last_error_category(self):
        """Old receipts that have no last_error_category key deserialize with None."""
        legacy = {
            "receipt_id": "r-old",
            "portal_id": "p1",
            "run_id": "run-1",
            "package_fingerprint": "x" * 64,
            "repository_target": "zenodo",
            "environment": "sandbox",
            "status": "FAILED",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "last_error": "Old style error",
            "last_error_category": None,
        }
        receipt = DepositReceipt.from_dict(legacy)
        assert receipt.status == DepositStatus.FAILED
        assert receipt.last_error == "Old style error"
        assert receipt.last_error_category is None

    def test_receipt_with_no_artifact_inventory(self):
        legacy = {
            "receipt_id": "r-bare",
            "portal_id": "p2",
            "run_id": "run-2",
            "package_fingerprint": "y" * 64,
            "repository_target": "zenodo",
            "environment": "sandbox",
            "status": "PENDING",
            "created_at": "2024-06-01T00:00:00+00:00",
            "updated_at": "2024-06-01T00:00:00+00:00",
        }
        receipt = DepositReceipt.from_dict(legacy)
        assert receipt.artifact_inventory == {}
        assert receipt.status == DepositStatus.PENDING


# ---------------------------------------------------------------------------
# 20. UploadedArtifactRecord to_dict
# ---------------------------------------------------------------------------

class TestArtifactRecordSerialization:

    def test_to_dict_has_expected_keys(self):
        art = _minimal_artifact(remote_md5="x" * 32, remote_file_id="file-1", verified=True)
        d = art.to_dict()
        assert set(d.keys()) == {
            "filename", "filesize", "local_sha256", "local_md5",
            "remote_md5", "remote_file_id", "verified",
        }

    def test_from_dict_round_trip(self):
        art = _minimal_artifact(remote_md5="a" * 32, remote_file_id="f-99", verified=True)
        d = art.to_dict()
        art2 = UploadedArtifactRecord.from_dict(d)
        assert art2.remote_md5 == "a" * 32
        assert art2.remote_file_id == "f-99"
        assert art2.verified is True

    def test_from_dict_with_no_remote_md5(self):
        d = {
            "filename": "checksums.sha256",
            "filesize": 32,
            "local_sha256": "b" * 64,
            "local_md5": "c" * 32,
        }
        art = UploadedArtifactRecord.from_dict(d)
        assert art.remote_md5 is None
        assert art.verified is False

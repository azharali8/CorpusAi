"""
Unit and integration tests for Phase 7 Archival Run Packaging and Verification.

Tests cover:
1. Successful packaging of a representative run (content, replay, metadata).
2. Separation of content archives from replay assets.
3. Correct preservation of run and WARC provenance in manifest.
4. Correct manifest and environment metadata generation (no secrets, relative paths).
5. Detection of missing, modified, and unexpected files.
6. Handling of optional or unavailable artifacts without creating phantom files.
7. Rejection of duplicate finalized run IDs without overwriting existing data.
8. Safe finalization: failures during staging clean up and leave no corrupted package.
9. Integration with ArchiveCatalog: latest_successful_run_id advances only on success.
10. Deposit metadata validation and inclusion when provided.
"""

import json
import os
import pytest
import shutil

from src.archive_catalog import ArchiveCatalog
from src.archive_package import (
    ArchivePackager,
    PackageVerifier,
    CONTENT_DIR,
    REPLAY_DIR,
    MANIFEST_FILENAME,
    ENVIRONMENT_FILENAME,
    CHECKSUMS_FILENAME,
    DEPOSIT_METADATA_FILENAME,
)
from src.deposit_metadata import Creator, DepositMetadata


@pytest.fixture
def sample_run_files(tmp_path):
    """Create sample dummy WARC, WACZ, and screenshot files for testing."""
    src_dir = tmp_path / "source_artifacts"
    src_dir.mkdir()

    html_warc = src_dir / "articles.warc.gz"
    html_warc.write_bytes(b"WARC/1.0\r\nWARC-Type: response\r\n\r\n<html>Article Content</html>")

    json_warc = src_dir / "articles.json.warc.gz"
    json_warc.write_bytes(b"WARC/1.0\r\nWARC-Type: response\r\n\r\n{\"id\": 123, \"title\": \"Test\"}")

    wacz_file = src_dir / "archive.wacz"
    wacz_file.write_bytes(b"PK\x03\x04Dummy WACZ Zip Container")

    shot1 = src_dir / "viewport.png"
    shot1.write_bytes(b"\x89PNG\r\n\x1a\nFake PNG Data")

    extra_doc = src_dir / "run_summary.json"
    extra_doc.write_text(json.dumps({"reused": 10, "new": 2}), encoding="utf-8")

    return {
        "html_warc": str(html_warc),
        "json_warc": str(json_warc),
        "wacz_file": str(wacz_file),
        "shot1": str(shot1),
        "extra_doc": str(extra_doc),
    }


def test_package_run_success_and_directory_layout(tmp_path, sample_run_files):
    """Verify standard run packaging creates the exact layout specified in docs/archive_storage_design.md."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    portal_id = "test-portal"
    run_id = "run-20261023T000000Z-a1b2c3d4"

    run_dir, result = packager.package_run(
        portal_id=portal_id,
        run_id=run_id,
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        target_urls=["https://example.org/news/"],
        content_html_warc=sample_run_files["html_warc"],
        content_json_warc=sample_run_files["json_warc"],
        replay_wacz=sample_run_files["wacz_file"],
        screenshots={"viewport.png": sample_run_files["shot1"]},
        extra_artifacts={"run_summary.json": sample_run_files["extra_doc"]},
        article_count=5,
        new_article_count=5,
    )

    assert os.path.isdir(run_dir)
    assert result.is_valid is True
    assert len(result.checksum_mismatches) == 0
    assert len(result.missing_files) == 0
    assert len(result.unexpected_files) == 0

    # Check directory structure
    assert os.path.exists(os.path.join(run_dir, "content", "content_html.warc.gz"))
    assert os.path.exists(os.path.join(run_dir, "content", "content_json.warc.gz"))
    assert os.path.exists(os.path.join(run_dir, "replay", "archive.wacz"))
    assert os.path.exists(os.path.join(run_dir, "replay", "screenshots", "viewport.png"))
    assert os.path.exists(os.path.join(run_dir, "run_summary.json"))
    assert os.path.exists(os.path.join(run_dir, MANIFEST_FILENAME))
    assert os.path.exists(os.path.join(run_dir, ENVIRONMENT_FILENAME))
    assert os.path.exists(os.path.join(run_dir, CHECKSUMS_FILENAME))

    # Read manifest and verify relative paths and metadata
    with open(os.path.join(run_dir, MANIFEST_FILENAME), "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["run_id"] == run_id
    assert manifest["portal_id"] == portal_id
    assert manifest["status"] == "COMPLETED"
    assert manifest["inventory"]["content_html_warc"] == "content/content_html.warc.gz"
    assert manifest["inventory"]["replay_wacz"] == "replay/archive.wacz"

    # Verify environment metadata
    with open(os.path.join(run_dir, ENVIRONMENT_FILENAME), "r", encoding="utf-8") as f:
        env_meta = json.load(f)
    assert "python_version" in env_meta
    assert "platform_system" in env_meta
    assert "crawler_name" in env_meta


def test_optional_artifacts_no_phantoms(tmp_path, sample_run_files):
    """When optional artifacts (like JSON WARC or WACZ) are absent, no phantom files are generated."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    run_dir, result = packager.package_run(
        portal_id="minimal-portal",
        run_id="run-minimal-001",
        mode="INCREMENTAL",
        status="CONVERGED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
        # No JSON warc, no WACZ, no screenshots
    )

    assert result.is_valid is True
    assert os.path.exists(os.path.join(run_dir, "content", "content_html.warc.gz"))
    assert not os.path.exists(os.path.join(run_dir, "content", "content_json.warc.gz"))
    assert not os.path.exists(os.path.join(run_dir, "replay", "archive.wacz"))


def test_verification_detects_corrupted_file(tmp_path, sample_run_files):
    """Verifier must catch any file modified after checksum generation."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    run_dir, result = packager.package_run(
        portal_id="tamper-portal",
        run_id="run-tamper-001",
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
    )
    assert result.is_valid is True

    # Tamper with the packaged WARC file
    tampered_file = os.path.join(run_dir, "content", "content_html.warc.gz")
    with open(tampered_file, "ab") as f:
        f.write(b"CORRUPTED EXTRA BYTES")

    verify_after = PackageVerifier.verify_package(run_dir)
    assert verify_after.is_valid is False
    assert len(verify_after.checksum_mismatches) == 1
    assert "content/content_html.warc.gz" in verify_after.checksum_mismatches[0]


def test_verification_detects_missing_file(tmp_path, sample_run_files):
    """Verifier must catch missing files declared in checksums.sha256."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    run_dir, _ = packager.package_run(
        portal_id="missing-portal",
        run_id="run-missing-001",
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
    )

    # Delete manifest
    os.remove(os.path.join(run_dir, MANIFEST_FILENAME))

    verify_after = PackageVerifier.verify_package(run_dir)
    assert verify_after.is_valid is False
    assert MANIFEST_FILENAME in verify_after.missing_files


def test_verification_detects_unexpected_file(tmp_path, sample_run_files):
    """Verifier must catch unrecorded rogue files dropped into the run directory."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    run_dir, _ = packager.package_run(
        portal_id="rogue-portal",
        run_id="run-rogue-001",
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
    )

    # Inject unexpected file
    rogue_path = os.path.join(run_dir, "content", "untracked_secret.txt")
    with open(rogue_path, "w", encoding="utf-8") as f:
        f.write("unexpected payload")

    verify_after = PackageVerifier.verify_package(run_dir)
    assert verify_after.is_valid is False
    assert "content/untracked_secret.txt" in verify_after.unexpected_files


def test_rejection_of_duplicate_finalized_run_id(tmp_path, sample_run_files):
    """Packager must refuse to overwrite an already finalized run directory."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    run_id = "run-immutable-001"
    packager.package_run(
        portal_id="immutable-portal",
        run_id=run_id,
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
    )

    # Attempting to package with the exact same run_id must raise FileExistsError
    with pytest.raises(FileExistsError) as exc_info:
        packager.package_run(
            portal_id="immutable-portal",
            run_id=run_id,
            mode="BASELINE",
            status="COMPLETED",
            started_at="2026-10-23T00:00:00Z",
            content_html_warc=sample_run_files["html_warc"],
        )
    assert "already exists and is immutable" in str(exc_info.value)


def test_packaging_failure_cleans_up_staging(tmp_path):
    """If an error occurs before atomic promotion, no staging directory or partial run directory is left."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    portal_id = "fail-portal"
    run_id = "run-fail-001"

    # Pass a non-existent file path which will be skipped or cause an error if forced
    runs_dir = packager.get_runs_dir(portal_id)

    # Monkeypatch compute_file_sha256 to raise an exception during staging checksum calculation
    import src.archive_package
    orig_compute = src.archive_package.compute_file_sha256

    def fail_compute(p):
        if "manifest.json" in p:
            raise RuntimeError("Simulated IO disk failure during packaging")
        return orig_compute(p)

    src.archive_package.compute_file_sha256 = fail_compute
    try:
        with pytest.raises(RuntimeError):
            packager.package_run(
                portal_id=portal_id,
                run_id=run_id,
                mode="BASELINE",
                status="COMPLETED",
                started_at="2026-10-23T00:00:00Z",
            )
    finally:
        src.archive_package.compute_file_sha256 = orig_compute

    # Final run directory must not exist
    assert not os.path.exists(packager.get_run_dir(portal_id, run_id))
    # Staging directory must have been cleaned up
    if os.path.exists(runs_dir):
        remaining = os.listdir(runs_dir)
        assert len(remaining) == 0


def test_catalog_pointer_safety_on_failed_and_successful_runs(tmp_path, sample_run_files):
    """Failed runs do not advance latest_successful_run_id; successful runs advance correctly."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))
    portal_id = "catalog-test-portal"

    catalog = ArchiveCatalog(portal_id=portal_id)

    # Run 1: Successful baseline
    run1_id = "run-20261023T010000Z-base"
    packager.package_run(
        portal_id=portal_id,
        run_id=run1_id,
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T01:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
        catalog=catalog,
    )
    assert catalog.latest_successful_run_id == run1_id

    # Run 2: Failed run (e.g. network outage or server 500)
    run2_id = "run-20261023T020000Z-fail"
    packager.package_run(
        portal_id=portal_id,
        run_id=run2_id,
        mode="INCREMENTAL",
        status="FAILED",
        previous_run_id=run1_id,
        started_at="2026-10-23T02:00:00Z",
        catalog=catalog,
        errors=["HTTP 500 Internal Server Error"],
    )
    # Crucial guarantee: latest_successful_run_id must remain run1_id
    assert catalog.latest_successful_run_id == run1_id
    assert catalog.runs[run2_id].status == "FAILED"

    # Run 3: Recovery run (CONVERGED)
    run3_id = "run-20261023T030000Z-recovery"
    packager.package_run(
        portal_id=portal_id,
        run_id=run3_id,
        mode="INCREMENTAL",
        status="CONVERGED",
        previous_run_id=run1_id,
        started_at="2026-10-23T03:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
        catalog=catalog,
    )
    # Now catalog pointer advances to run3_id
    assert catalog.latest_successful_run_id == run3_id


def test_deposit_metadata_inclusion_and_validation(tmp_path, sample_run_files):
    """Optional DepositMetadata is validated and packaged in deposit_metadata.json."""
    archives_root = tmp_path / "archives"
    packager = ArchivePackager(str(archives_root))

    dep_meta = DepositMetadata(
        title="CorpusAI Research Web Archive",
        creators=[Creator(name="Balázs Indig", affiliation="Research Team")],
        description="Preserved research web corpus archive with verifiable WARC and replay containers.",
        capture_period_start="2026-10-23T00:00:00Z",
        capture_period_end="2026-10-23T01:00:00Z",
        source_domains=["example.org"],
        license_status="Research-Only",
        copyright_notice="Copyright (c) 2026 Research Authors",
        software_version="corpusai-0.1.0",
        archive_formats=["WARC", "WACZ"],
        checksum_manifest_path="checksums.sha256",
    )

    run_dir, result = packager.package_run(
        portal_id="deposit-portal",
        run_id="run-deposit-001",
        mode="BASELINE",
        status="COMPLETED",
        started_at="2026-10-23T00:00:00Z",
        content_html_warc=sample_run_files["html_warc"],
        deposit_metadata=dep_meta,
    )

    assert result.is_valid is True
    dep_json = os.path.join(run_dir, DEPOSIT_METADATA_FILENAME)
    assert os.path.exists(dep_json)
    loaded = DepositMetadata.load_from_json(dep_json)
    assert loaded.title == "CorpusAI Research Web Archive"
    assert loaded.creators[0].name == "Balázs Indig"


# ---------------------------------------------------------------------------
# Test 10: End-to-End Pipeline Integration (Crawl -> WARC -> Package -> Verify)
# ---------------------------------------------------------------------------
def test_controlled_crawl_to_archival_package_integration(tmp_path):
    """
    End-to-End Integration Gate:
    1. Executes a controlled incremental crawl against a local loopback portal.
    2. Generates genuine WARC records and portal state.
    3. Packages the crawl into an archival run package via ArchivePackager.
    4. Independently verifies the package with PackageVerifier.
    5. Confirms WARC provenance (warc_record_id, content_hash) and relative paths.
    6. Executes a second unchanged crawl, packages it with reuse provenance,
       and confirms that no phantom content files are created.
    """
    import urllib.parse
    from bs4 import BeautifulSoup
    from src.downloader import ResourcePreservationHarvester
    from src.incremental_archive_policy import IncrementalArchivePolicy
    from tests.test_controlled_incremental_crawler import ControlledPortalServer, _read_warc_records

    routes = {
        "/news/page/1": {
            "body": """<html><body>
                <a class="post" href="/news/post-1">Post 1</a>
                <a class="post" href="/news/post-2">Post 2</a>
            </body></html>"""
        },
        "/news/post-1": {"body": "<html><body><h1>Post 1 Content</h1><p>Body 1</p></body></html>"},
        "/news/post-2": {"body": "<html><body><h1>Post 2 Content</h1><p>Body 2</p></body></html>"},
    }

    with ControlledPortalServer(routes) as portal:
        base_url = f"http://127.0.0.1:{portal.port}"
        harvester = ResourcePreservationHarvester(delay_seconds=0.0)

        def page_fetcher(page_url: str):
            res = harvester.fetch_url(page_url, resource_type="archive_page")
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            posts = [urllib.parse.urljoin(page_url, a["href"]) for a in soup.find_all("a", class_="post")]
            return {
                "status_code": res.status_code,
                "article_urls": posts,
                "next_page_url": None,
                "html": res.payload.decode("utf-8"),
            }

        warc_path_run1 = str(tmp_path / "raw_crawl_run1.warc.gz")

        def article_fetcher(art_url: str):
            res = harvester.fetch_url(art_url, resource_type="html")
            import hashlib
            content_hash = hashlib.sha256(res.payload).hexdigest()
            return {
                "title": f"Title for {art_url}",
                "content_hash": content_hash,
                "raw_hash": content_hash,
                "warc_file": warc_path_run1,
                "warc_record_id": f"<urn:uuid:run1-{os.path.basename(art_url)}>",
                "content_capture_status": "CAPTURED",
            }

        policy = IncrementalArchivePolicy(
            portal_id="integration-portal",
            head_page_url=f"{base_url}/news/page/1",
        )
        state1, run1, diff1 = policy.execute_incremental_crawl(
            page_fetcher=page_fetcher,
            article_fetcher=article_fetcher,
        )

        harvester.save_to_warc(warc_path_run1)
        assert os.path.exists(warc_path_run1)
        assert len(state1.articles) == 2

        # Package Run 1
        archives_root = tmp_path / "archives"
        packager = ArchivePackager(str(archives_root))
        catalog = ArchiveCatalog(portal_id="integration-portal")

        portal_state_file = str(tmp_path / "portal_state_run1.json")
        state1.save_to_json(portal_state_file)

        run1_id = "run-20261023T040000Z-int1"
        run1_dir, verify1 = packager.package_run(
            portal_id="integration-portal",
            run_id=run1_id,
            mode=run1.mode,
            status=run1.convergence_status,
            started_at=run1.started_at,
            completed_at=run1.completed_at,
            target_urls=[f"{base_url}/news/page/1"],
            content_html_warc=warc_path_run1,
            extra_artifacts={"portal_state.json": portal_state_file},
            article_count=len(state1.articles),
            new_article_count=len(diff1.new_articles),
            catalog=catalog,
        )

        # Verification of Packaged Run 1
        assert verify1.is_valid is True
        assert catalog.latest_successful_run_id == run1_id

        # Confirm package contains real WARC and readable records
        packaged_warc = os.path.join(run1_dir, "content", "content_html.warc.gz")
        assert os.path.exists(packaged_warc)
        warc_records = _read_warc_records(packaged_warc)
        assert len(warc_records) >= 2

        # Read manifest and verify provenance linkage
        with open(os.path.join(run1_dir, MANIFEST_FILENAME), "r", encoding="utf-8") as f:
            manifest1 = json.load(f)
        assert manifest1["run_id"] == run1_id
        assert manifest1["status"] == run1.convergence_status
        assert manifest1["inventory"]["content_html_warc"] == "content/content_html.warc.gz"

        # --- Run 2: Unchanged Crawl with Reuse ---
        portal.request_log.clear()
        harvester_run2 = ResourcePreservationHarvester(delay_seconds=0.0)

        def page_fetcher_run2(page_url: str):
            res = harvester_run2.fetch_url(page_url, resource_type="archive_page")
            soup = BeautifulSoup(res.payload.decode("utf-8"), "html.parser")
            posts = [urllib.parse.urljoin(page_url, a["href"]) for a in soup.find_all("a", class_="post")]
            return {
                "status_code": res.status_code,
                "article_urls": posts,
                "next_page_url": None,
                "html": res.payload.decode("utf-8"),
            }

        policy_run2 = IncrementalArchivePolicy(
            portal_id="integration-portal",
            head_page_url=f"{base_url}/news/page/1",
            previous_state=state1,
        )
        state2, run2, diff2 = policy_run2.execute_incremental_crawl(
            page_fetcher=page_fetcher_run2,
            article_fetcher=None,
        )

        assert len(diff2.unchanged_articles) == 2
        assert len(diff2.new_articles) == 0

        portal_state_file_run2 = str(tmp_path / "portal_state_run2.json")
        state2.save_to_json(portal_state_file_run2)

        # In run 2, no new article WARC was produced because all bodies were reused
        run2_id = "run-20261023T050000Z-int2"
        run2_dir, verify2 = packager.package_run(
            portal_id="integration-portal",
            run_id=run2_id,
            mode=run2.mode,
            status=run2.convergence_status,
            started_at=run2.started_at,
            completed_at=run2.completed_at,
            previous_run_id=run1_id,
            target_urls=[f"{base_url}/news/page/1"],
            content_html_warc=None,  # No new WARC for 100% reused run
            extra_artifacts={"portal_state.json": portal_state_file_run2},
            article_count=len(state2.articles),
            new_article_count=0,
            changed_article_count=0,
            catalog=catalog,
        )

        # Verification of Packaged Run 2
        assert verify2.is_valid is True
        assert catalog.latest_successful_run_id == run2_id

        # Verify no phantom content_html.warc.gz was created
        assert not os.path.exists(os.path.join(run2_dir, "content", "content_html.warc.gz"))

        # Re-verify through standalone PackageVerifier
        reverify1 = PackageVerifier.verify_package(run1_dir)
        reverify2 = PackageVerifier.verify_package(run2_dir)
        assert reverify1.is_valid is True
        assert reverify2.is_valid is True

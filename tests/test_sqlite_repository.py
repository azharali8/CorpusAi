"""
Unit tests for CorpusAI SQLite Repository, DAO, and Migration Tool.
"""

import pytest
import json
from pathlib import Path
from src.storage.sqlite_backend import CorpusAIStateDatabase
from src.storage.repository import CorpusAIRepository
from src.storage.migration_tool import StateMigrationTool
from src.archive_catalog import ArchiveCatalog, CatalogRunEntry
from src.portal_state import PortalState, ArticleState, ArchivePageState
from src.content_url_registry import ContentURLRegistry, ContentURLEntry


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / "dao_test.db"
    database = CorpusAIStateDatabase(db_path=db_file)
    yield database
    database.close()


def test_repository_upsert_portal_and_run(db):
    repo = CorpusAIRepository(db)
    repo.upsert_portal("portal-1", "News Portal", "https://news.example.com", "wordpress")
    p = repo.get_portal("portal-1")
    assert p is not None
    assert p["name"] == "News Portal"

    repo.upsert_run(
        portal_id="portal-1",
        run_id="run-001",
        started_at="2026-11-15T10:00:00Z",
        status="COMPLETED",
        articles_discovered=42,
    )
    r = repo.get_run("portal-1", "run-001")
    assert r is not None
    assert r["articles_discovered"] == 42
    assert r["status"] == "COMPLETED"


def test_repository_discovered_url(db):
    repo = CorpusAIRepository(db)
    repo.upsert_portal("portal-1", "News", "https://n.com")
    repo.upsert_run("portal-1", "r-1", "2026-11-15T10:00:00Z", "COMPLETED")

    repo.upsert_discovered_url(
        portal_id="portal-1",
        url_hash="hash-abc",
        normalized_url="https://n.com/art1",
        first_seen_run_id="r-1",
        first_seen_at="2026-11-15T10:00:00Z",
        last_seen_run_id="r-1",
        last_seen_at="2026-11-15T10:00:00Z",
        http_status=200,
    )

    url_rec = repo.get_discovered_url("portal-1", "hash-abc")
    assert url_rec is not None
    assert url_rec["normalized_url"] == "https://n.com/art1"
    assert url_rec["http_status"] == 200


def test_migration_tool_catalog_round_trip(db, tmp_path):
    tool = StateMigrationTool(db)
    cat_file = tmp_path / "catalog.json"
    run_entry = CatalogRunEntry(
        run_id="run-1",
        previous_run_id=None,
        mode="incremental",
        started_at="2026-11-15T10:00:00Z",
        completed_at="2026-11-15T10:05:00Z",
        status="COMPLETED",
        article_count=15,
        new_article_count=15,
        changed_article_count=0,
        run_dir="runs/run-1",
    )
    cat_orig = ArchiveCatalog(
        portal_id="p-test",
        latest_successful_run_id="run-1",
        all_run_ids=["run-1"],
        runs={"run-1": run_entry},
    )
    cat_orig.save_to_json(cat_file)

    # Import
    imported_count = tool.import_catalog_json(cat_file, "p-test", "Portal Test", "https://test.com")
    assert imported_count == 1

    # Export
    export_file = tmp_path / "exported_catalog.json"
    cat_exported = tool.export_catalog_json("p-test", export_file)
    assert cat_exported.portal_id == "p-test"
    assert cat_exported.latest_successful_run_id == "run-1"
    assert len(cat_exported.runs) == 1
    assert cat_exported.runs["run-1"].article_count == 15
    assert cat_exported.runs["run-1"].run_id == "run-1"


def test_migration_tool_portal_state_round_trip(db, tmp_path):
    tool = StateMigrationTool(db)
    repo = CorpusAIRepository(db)

    # Ensure parent portal and run exist
    repo.upsert_portal("p-state", "Portal State Test", "https://statetest.com")
    repo.upsert_run("p-state", "run-st-1", "2026-11-15T10:00:00Z", "COMPLETED")

    state_file = tmp_path / "portal_state.json"
    art = ArticleState(
        canonical_url="https://statetest.com/art1",
        first_seen_run_id="run-st-1",
        last_seen_run_id="run-st-1",
        title="Article 1",
        content_hash="hash-111",
        status="UNCHANGED",
    )
    page = ArchivePageState(
        url="https://statetest.com/page/1",
        ordered_fingerprint="fp-1",
        unordered_fingerprint="fp-1",
        article_urls=["https://statetest.com/art1"],
        page_number=1,
    )
    state_orig = PortalState(
        portal_id="p-state",
        run_id="run-st-1",
        archive_pages={"https://statetest.com/page/1": page},
        articles={"https://statetest.com/art1": art},
        archive_order=["https://statetest.com/art1"],
    )
    state_orig.save_to_json(state_file)

    # Import
    p_cnt, a_cnt = tool.import_portal_state_json(state_file, "p-state", "run-st-1")
    assert p_cnt == 1
    assert a_cnt == 1

    # Export
    exp_file = tmp_path / "exported_portal_state.json"
    state_exp = tool.export_portal_state_json("p-state", "run-st-1", exp_file)
    assert state_exp.portal_id == "p-state"
    assert state_exp.run_id == "run-st-1"
    assert len(state_exp.archive_pages) == 1
    assert len(state_exp.articles) == 1
    assert state_exp.articles["https://statetest.com/art1"].title == "Article 1"


def test_runner_live_crawl_authoritative_sqlite_cutover(tmp_path):
    """
    Test 5: Live execution of corpusai_run.main() populates SQLite authoritative tables:
    - portals
    - runs (with CONVERGED status)
    - portal_states
    - archive_page_observations
    - article_states
    - discovered_urls
    - packages
    """
    from unittest import mock
    import src.corpusai_run as runner
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.downloader import DownloadedResource

    results_dir = tmp_path / "results"

    sample_head = """
    <html><body>
        <article><a href="https://wordpress.org/news/post-1/">Post 1</a></article>
        <article><a href="https://wordpress.org/news/post-2/">Post 2</a></article>
    </body></html>
    """
    sample_art_1 = "<html><head><title>Post 1</title></head><body><h1>Content 1</h1></body></html>"
    sample_art_2 = "<html><head><title>Post 2</title></head><body><h1>Content 2</h1></body></html>"

    def mock_fetch(url, resource_type="asset"):
        if "all-posts" in url:
            content = sample_head.encode("utf-8")
        elif "post-1" in url:
            content = sample_art_1.encode("utf-8")
        elif "post-2" in url:
            content = sample_art_2.encode("utf-8")
        else:
            content = b"<html><body>OK</body></html>"
        import hashlib
        return DownloadedResource(
            url=url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            payload=content,
            sha256=hashlib.sha256(content).hexdigest(),
            resource_type="html",
        )

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            runner.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
            ])
        assert exc_info.value.code == 0

    # Inspect authoritative SQLite state
    db_file = results_dir / "storage" / "corpusai_state.db"
    assert db_file.exists()

    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)

    # 1. Portal check
    portal = repo.get_portal("wordpress-news")
    assert portal is not None
    assert portal["latest_successful_run_id"] is not None
    run_id = portal["latest_successful_run_id"]

    # 2. Run check
    run = repo.get_run("wordpress-news", run_id)
    assert run is not None
    assert run["status"] == "CONVERGED"
    assert run["articles_discovered"] == 2
    assert run["new_articles_count"] == 2

    # 3. Portal state snapshot check
    state_snap = repo.get_latest_portal_state("wordpress-news")
    assert state_snap is not None
    assert state_snap["run_id"] == run_id
    assert state_snap["total_articles_count"] == 2

    # 4. Article states check
    conn = db.connect()
    art_count = conn.execute(
        "SELECT count(*) FROM article_states WHERE portal_id = ? AND last_extracted_run_id = ?;",
        ("wordpress-news", run_id)
    ).fetchone()[0]
    assert art_count == 2

    # 5. Archive page observation check
    page_count = conn.execute(
        "SELECT count(*) FROM archive_page_observations WHERE portal_id = ? AND run_id = ?;",
        ("wordpress-news", run_id)
    ).fetchone()[0]
    assert page_count >= 1

    # 6. Discovered URLs check
    url_count = conn.execute(
        "SELECT count(*) FROM discovered_urls WHERE portal_id = ?;",
        ("wordpress-news",)
    ).fetchone()[0]
    assert url_count >= 2

    # 7. Package registration check
    pkg_row = conn.execute(
        "SELECT * FROM packages WHERE portal_id = ? AND package_id = ?;",
        ("wordpress-news", run_id)
    ).fetchone()
    assert pkg_row is not None
    assert pkg_row["package_status"] == "SEALED"
    assert pkg_row["total_articles"] == 2


def test_interrupted_run_reconciliation_on_startup(tmp_path):
    """
    Test 6: Verify interrupted-run recovery behavior:
    1. A previous run left in RUNNING status is detected on startup.
    2. It is reconciled and transitioned to INTERRUPTED status.
    3. The interrupted run is never promoted to successful.
    4. A new run can execute and become the authoritative latest run.
    """
    from unittest import mock
    import src.corpusai_run as runner
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.downloader import DownloadedResource

    results_dir = tmp_path / "results_interrupted"
    db_file = results_dir / "storage" / "corpusai_state.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Pre-seed an orphaned RUNNING run in SQLite
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)
    repo.upsert_portal("wordpress-news", "WordPress News", "https://wordpress.org/news/all-posts/")
    repo.upsert_run(
        portal_id="wordpress-news",
        run_id="run-orphaned-1",
        started_at="2026-11-15T00:00:00Z",
        status="RUNNING",
        articles_discovered=1,
    )
    db.close()

    sample_head = "<html><body><article><a href='https://wordpress.org/news/p1/'>P1</a></article></body></html>"
    sample_art = "<html><head><title>P1</title></head><body><h1>Content</h1></body></html>"

    def mock_fetch(url, resource_type="asset"):
        content = sample_head.encode("utf-8") if "all-posts" in url else sample_art.encode("utf-8")
        import hashlib
        return DownloadedResource(
            url=url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            payload=content,
            sha256=hashlib.sha256(content).hexdigest(),
            resource_type="html",
        )

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            runner.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
            ])
        assert exc_info.value.code == 0

    # Verify reconciliation in SQLite
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)

    # Orphaned run must now be marked INTERRUPTED
    orphaned_run = repo.get_run("wordpress-news", "run-orphaned-1")
    assert orphaned_run is not None
    assert orphaned_run["status"] == "INTERRUPTED"
    assert orphaned_run["interrupted"] == 1
    assert "interrupted" in (orphaned_run["error_message"] or "").lower()

    # The portal's latest_successful_run_id must NOT be the orphaned run
    portal = repo.get_portal("wordpress-news")
    assert portal["latest_successful_run_id"] != "run-orphaned-1"
    assert portal["latest_successful_run_id"] is not None
    db.close()


def test_new_portal_startup_without_catalog_json(tmp_path):
    """
    Test 7: Verify startup of a new portal when no catalog.json exists at all.
    SQLite must initialize cleanly, export a fresh catalog from DB, and succeed.
    """
    from unittest import mock
    import src.corpusai_run as runner
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.downloader import DownloadedResource

    results_dir = tmp_path / "results_new_portal"
    catalog_file = results_dir / "catalog.json"
    assert not catalog_file.exists()

    sample_head = "<html><body><article><a href='https://wordpress.org/news/p1/'>P1</a></article></body></html>"
    sample_art = "<html><head><title>P1</title></head><body><h1>Content</h1></body></html>"

    def mock_fetch(url, resource_type="asset"):
        content = sample_head.encode("utf-8") if "all-posts" in url else sample_art.encode("utf-8")
        import hashlib
        return DownloadedResource(
            url=url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            payload=content,
            sha256=hashlib.sha256(content).hexdigest(),
            resource_type="html",
        )

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            runner.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
            ])
        assert exc_info.value.code == 0

    assert catalog_file.exists()
    db_file = results_dir / "storage" / "corpusai_state.db"
    assert db_file.exists()
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)
    portal = repo.get_portal("wordpress-news")
    assert portal is not None
    assert portal["latest_successful_run_id"] is not None
    db.close()


def test_incremental_run_full_metrics_persisted_to_sqlite(tmp_path):
    """
    Test 8: Verify that all IncrementalRun metrics (counter fields, anomaly info,
    retry logs, and metadata) are fully persisted to SQLite runs table.
    """
    from unittest import mock
    import src.corpusai_run as runner
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.downloader import DownloadedResource

    results_dir = tmp_path / "results_metrics"

    sample_head = "<html><body><article><a href='https://wordpress.org/news/p1/'>P1</a></article></body></html>"
    sample_art = "<html><head><title>P1</title></head><body><h1>Content</h1></body></html>"

    def mock_fetch(url, resource_type="asset"):
        content = sample_head.encode("utf-8") if "all-posts" in url else sample_art.encode("utf-8")
        import hashlib
        return DownloadedResource(
            url=url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            payload=content,
            sha256=hashlib.sha256(content).hexdigest(),
            resource_type="html",
        )

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch):

        with pytest.raises(SystemExit) as exc_info:
            runner.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
            ])
        assert exc_info.value.code == 0

    db_file = results_dir / "storage" / "corpusai_state.db"
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)
    portal = repo.get_portal("wordpress-news")
    run_id = portal["latest_successful_run_id"]
    run_row = repo.get_run("wordpress-news", run_id)

    assert run_row["articles_discovered"] == 1
    assert run_row["new_articles_count"] == 1
    assert run_row["archive_page_count"] >= 1
    assert run_row["status"] == "CONVERGED"
    assert run_row["interrupted"] == 0
    assert run_row["scope_completion"] is not None
    meta = json.loads(run_row["metadata_json"])
    assert "missing_article_count" in meta
    assert "fetch_failure_count" in meta
    assert "retry_log" in meta
    db.close()


def test_finalizing_run_recovery_with_valid_promoted_package(tmp_path):
    """
    Test 9: Verify that an interrupted FINALIZING run with an existing valid package
    is recovered, verified, promoted to CONVERGED, and updates the portal pointer.
    """
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    import hashlib

    results_dir = tmp_path / "results_finalizing"
    pkg_dir = results_dir / "packages" / "wordpress-news" / "runs" / "run-fin-1"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Create package files
    (pkg_dir / "manifest.json").write_text('{"run_id": "run-fin-1", "portal_id": "wordpress-news"}', encoding="utf-8")
    (pkg_dir / "environment.json").write_text('{"python_version": "3.13"}', encoding="utf-8")
    (pkg_dir / "portal_state.json").write_text('{}', encoding="utf-8")
    m_hash = hashlib.sha256(b'{"run_id": "run-fin-1", "portal_id": "wordpress-news"}').hexdigest()
    e_hash = hashlib.sha256(b'{"python_version": "3.13"}').hexdigest()
    s_hash = hashlib.sha256(b'{}').hexdigest()
    checksums_content = f"{m_hash}  manifest.json\n{e_hash}  environment.json\n{s_hash}  portal_state.json\n"
    (pkg_dir / "checksums.sha256").write_text(checksums_content, encoding="utf-8")

    db_file = results_dir / "storage" / "corpusai_state.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)

    repo.upsert_portal("wordpress-news", "WordPress News", "https://wordpress.org/news/")
    repo.upsert_run(
        portal_id="wordpress-news",
        run_id="run-fin-1",
        started_at="2026-11-15T12:00:00Z",
        status="FINALIZING",
    )

    # Trigger recovery
    status = repo.recover_finalizing_run("wordpress-news", "run-fin-1", str(results_dir))
    assert status == "CONVERGED"

    run_row = repo.get_run("wordpress-news", "run-fin-1")
    assert run_row["status"] == "CONVERGED"

    portal = repo.get_portal("wordpress-news")
    assert portal["latest_successful_run_id"] == "run-fin-1"
    db.close()


def test_deposit_registry_sqlite_round_trip(tmp_path):
    """
    Test 10: Verify DepositRegistry import into SQLite and reverse export parity.
    """
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.storage.migration_tool import StateMigrationTool
    from src.zenodo_stuff.models import DepositReceipt, UploadedArtifactRecord
    from src.zenodo_stuff.registry import DepositRegistry

    db_file = tmp_path / "deposit_test.db"
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)
    tool = StateMigrationTool(db)

    repo.upsert_portal("p-dep", "Deposit Portal", "https://dep.org")
    repo.upsert_run("p-dep", "run-dep-1", "2026-11-15T10:00:00Z", "CONVERGED")
    repo.upsert_package("p-dep", "run-dep-1", "/tmp/pkg/run-dep-1", package_status="SEALED")

    # Create dummy deposit_registry.json
    reg_path = tmp_path / "deposit_registry.json"
    registry = DepositRegistry(reg_path)
    art1 = UploadedArtifactRecord(
        filename="archive.warc.gz",
        filesize=12345,
        local_sha256="sha-12345",
        local_md5="md5-123",
        remote_file_id="rec-file-99",
        verified=True,
    )
    receipt = DepositReceipt(
        receipt_id="rec-001",
        portal_id="p-dep",
        run_id="run-dep-1",
        package_fingerprint="pkg-fp-abc",
        repository_target="zenodo_sandbox",
        environment="sandbox",
        status="PUBLISHED",
        created_at="2026-11-15T10:10:00Z",
        updated_at="2026-11-15T10:15:00Z",
        remote_record_id="zenodo-1234",
        doi="10.5281/zenodo.1234",
        artifact_inventory={"archive.warc.gz": art1},
        checksum_verification_outcome="VERIFIED",
    )
    registry.register_receipt(receipt)

    # Import into SQLite
    cnt = tool.import_deposit_registry_json(reg_path, "p-dep")
    assert cnt == 1

    # Verify rows in DB
    db_rec = repo.get_deposit_record("p-dep", "rec-001")
    assert db_rec is not None
    assert db_rec["doi"] == "10.5281/zenodo.1234"
    assert db_rec["status"] == "PUBLISHED"

    db_arts = repo.get_deposit_artifacts("p-dep", "rec-001")
    assert len(db_arts) == 1
    assert db_arts[0]["artifact_filename"] == "archive.warc.gz"
    assert db_arts[0]["upload_status"] == "VERIFIED"

    # Export back and verify parity
    exp_path = tmp_path / "exported_deposit_registry.json"
    exp_reg = tool.export_deposit_registry_json("p-dep", exp_path)
    exp_receipt = exp_reg.get_receipt("p-dep", "run-dep-1")
    assert exp_receipt is not None
    assert exp_receipt.doi == "10.5281/zenodo.1234"
    assert exp_receipt.status == "PUBLISHED"
    assert "archive.warc.gz" in exp_receipt.artifact_inventory
    assert exp_receipt.artifact_inventory["archive.warc.gz"].verified is True
    db.close()


def test_interrupted_crawl_durability_before_completion(tmp_path):
    """
    Test 11: Verify incremental operational state durability during crawl execution.
    If the crawl is interrupted after page or article fetch, observations and article
    states committed at transaction boundaries survive database close and reopen.
    """
    from unittest import mock
    import src.corpusai_run as runner
    from src.storage import CorpusAIStateDatabase
    from src.storage.repository import CorpusAIRepository
    from src.downloader import DownloadedResource

    results_dir = tmp_path / "results_durable"
    sample_head = "<html><body><article><a href='https://wordpress.org/news/p1/'>P1</a></article></body></html>"
    sample_art = "<html><head><title>P1</title></head><body><h1>Content</h1></body></html>"

    fetch_count = 0
    def mock_fetch_with_crash(url, resource_type="asset"):
        nonlocal fetch_count
        fetch_count += 1
        if "all-posts" in url:
            content = sample_head.encode("utf-8")
        elif "p1" in url:
            content = sample_art.encode("utf-8")
        else:
            content = b"OK"
        import hashlib
        res = DownloadedResource(
            url=url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            payload=content,
            sha256=hashlib.sha256(content).hexdigest(),
            resource_type="html",
        )
        if fetch_count >= 2:
            # Simulate sudden unhandled crash during crawl after fetching head & 1st article
            raise KeyboardInterrupt("Simulated sudden process interruption during crawl")
        return res

    with mock.patch("src.corpusai_run.check_robots_txt", return_value=(True, "Allowed")), \
         mock.patch("src.downloader.ResourcePreservationHarvester.fetch_url", side_effect=mock_fetch_with_crash):

        with pytest.raises((KeyboardInterrupt, SystemExit)):
            runner.main([
                "--portal", "wordpress-news",
                "--mode", "baseline",
                "--results-dir", str(results_dir),
            ])

    # Reopen database and inspect durable state
    db_file = results_dir / "storage" / "corpusai_state.db"
    assert db_file.exists()
    db = CorpusAIStateDatabase(db_file)
    repo = CorpusAIRepository(db)

    # Archive page observation was durably written
    conn = db.connect()
    pages = conn.execute("SELECT * FROM archive_page_observations WHERE portal_id = 'wordpress-news';").fetchall()
    assert len(pages) >= 1
    assert "all-posts" in pages[0]["page_url"]

    # Discovered URL was durably written
    urls = conn.execute("SELECT * FROM discovered_urls WHERE portal_id = 'wordpress-news';").fetchall()
    assert len(urls) >= 1
    assert "https://wordpress.org/news/p1" in urls[0]["normalized_url"]

    # Run remains in RUNNING state (not marked successful)
    runs = [r for r in repo.list_runs("wordpress-news") if r["run_id"] != "__system__"]
    assert len(runs) >= 1
    assert runs[0]["status"] == "RUNNING"
    db.close()




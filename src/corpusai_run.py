"""
CorpusAI Archival Run Orchestrator and CLI Runner.

Executes baseline and incremental crawls for web portals with:
- Strict intra-run retry policies and request budget controls.
- Checkpointing, anomaly detection, and state persistence.
- Verified packaging with SHA-256 tamper-evident checksums.
- Archive catalog lineage tracking and atomic pointer updates.
- Structured file and console operational logging.
"""

import argparse
import datetime
import hashlib
import json
import logging
from pathlib import Path
import socket
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
import urllib.robotparser

import requests

from src.archive_catalog import CatalogRunEntry
from src.archive_package import ArchivePackager, PackageVerifier
from src.crawling_logic.portal_fetchers import build_portal_fetchers
from src.downloader import ResourcePreservationHarvester
from src.incremental_archive_policy import (
    IncrementalArchivePolicy,
    IncrementalCrawlBudget,
)
from src.portal_state import PortalState
from src.replay_validation import generate_sha256_manifest
from src.storage_backend import LocalStateStorageBackend
from src.utils.paths import resolve_contained_path
from src.catalog_recovery import (
    validate_baseline_integrity,
    BaselineIntegrityError,
    CatalogReconciler,
    StorageRetentionManager,
)
from src.storage import CorpusAIStateDatabase
from src.storage.repository import CorpusAIRepository
from src.storage.migration_tool import StateMigrationTool


PORTAL_CONFIGS: Dict[str, Dict[str, Any]] = {
    "wordpress-news": {
        "head_page_url": "https://wordpress.org/news/all-posts/",
        "portal_id": "wordpress-news",
        "results_base_dir": "results/wordpress_validation/phase6b",
        "catalog_path": "results/wordpress_validation/phase6b/catalog.json",
        "user_agent": "CorpusAI-Research-Validator/1.0 (+https://github.com/CorpusAI; Academic Research Evaluation)",
        "polite_delay_seconds": 1.5,
        "max_archive_pages": 5,
        "max_articles": 25,
        "max_requests": 50,
        "max_retries": 2,
    }
}


def setup_logging(
    run_dir: str,
    log_level: str = "INFO",
    portal_id: str = "",
    run_id: str = "",
) -> logging.Logger:
    """Configure file + console logging for a run."""
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    log_path = run_path / "run.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    logger = logging.getLogger(f"corpusai.{portal_id}")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear existing handlers to prevent duplicate lines when rerun in same process
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)

    # File handler — always DEBUG level for full diagnostics
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    # Console handler — user-specified level
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False
    return logger


def check_robots_txt(url: str, user_agent: str) -> Tuple[bool, str]:
    """Check target URL accessibility against robots.txt."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        with requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=10.0) as resp:
            if not 200 <= resp.status_code < 300:
                raise requests.HTTPError(f"HTTP Error {resp.status_code}: {resp.reason}", response=resp)
            content = resp.content.decode("utf-8", errors="replace")
            rp.parse(content.splitlines())
            allowed = rp.can_fetch(user_agent, url)
            return allowed, f"robots.txt fetched successfully from {robots_url}"
    except Exception as e:
        # Default allow with warning on unreachable robots.txt
        return True, f"robots.txt check warning: {e}"


def flush_logger(logger: logging.Logger) -> None:
    """Flush all handlers for a logger."""
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    """
    Main entrypoint for CorpusAI CLI runner.

    Exit codes:
      0 = CONVERGED and package verified
      1 = FAILED or unrecoverable error
      2 = invalid arguments
      3 = robots.txt disallowed
      4 = no baseline found for incremental mode
      5 = package verification failed
      6 = BUDGET_EXHAUSTED (partial run, checkpoint saved)
      7 = MANUAL_REVIEW_REQUIRED (anomaly detected)
      8 = LOCK_ACQUISITION_FAILED (concurrent process holds portal lock)
      9 = REMOTE_STORAGE_FAILED (remote verification, quarantine, or baseline commit failed)
      10 = REMOTE_LEASE_LOST (heartbeat failure or lease takeover during crawl)
    """
    parser = argparse.ArgumentParser(description="CorpusAI Archival Run Orchestrator")
    parser.add_argument("--portal", required=True, help="Target portal identifier (e.g. wordpress-news)")
    parser.add_argument("--mode", required=True, choices=["baseline", "incremental"], help="Crawl execution mode")
    parser.add_argument("--results-dir", default=None, help="Optional override for results base directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse args and resolve config but make no HTTP calls or file writes",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level (default: INFO)",
    )
    parser.add_argument(
        "--reconcile-catalog",
        action="store_true",
        help="Audit and reconcile catalog with verified packages before run",
    )
    parser.add_argument(
        "--cleanup-staging",
        action="store_true",
        help="Execute conservative retention cleanup on raw staging runs",
    )
    parser.add_argument(
        "--storage-backend",
        default="local",
        choices=["local", "s3"],
        help="Remote storage backend (default: local). Use 's3' for AWS S3 durable state.",
    )
    parser.add_argument(
        "--s3-bucket",
        default=None,
        help="S3 bucket name. Required when --storage-backend=s3.",
    )
    parser.add_argument(
        "--s3-prefix",
        default="",
        help="S3 key prefix for all objects (default: empty string).",
    )
    parser.add_argument(
        "--runner-id",
        default=None,
        help="Unique runner identifier for lease tracking. Auto-generated if not provided.",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        sys.exit(2)

    if args.portal not in PORTAL_CONFIGS:
        sys.stderr.write(f"Error: Unknown portal '{args.portal}'. Known portals: {list(PORTAL_CONFIGS.keys())}\n")
        sys.exit(2)

    if args.storage_backend == "s3" and not args.s3_bucket:
        sys.stderr.write("Error: --s3-bucket is required when --storage-backend=s3.\n")
        sys.exit(2)

    runner_id = args.runner_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    config = dict(PORTAL_CONFIGS[args.portal])
    results_base_dir = args.results_dir or config["results_base_dir"]
    catalog_path = str(Path(results_base_dir) / "catalog.json") if args.results_dir else config["catalog_path"]

    # Dry-run handling: make no HTTP requests or disk modifications
    if args.dry_run:
        sys.stdout.write(f"[DRY-RUN] Portal: {args.portal}\n")
        sys.stdout.write(f"[DRY-RUN] Mode: {args.mode}\n")
        sys.stdout.write(f"[DRY-RUN] Results Dir: {results_base_dir}\n")
        sys.stdout.write(f"[DRY-RUN] Head URL: {config['head_page_url']}\n")
        sys.stdout.write(f"[DRY-RUN] Max Archive Pages: {config['max_archive_pages']}\n")
        sys.stdout.write(f"[DRY-RUN] Max Articles: {config['max_articles']}\n")
        sys.stdout.write(f"[DRY-RUN] Max Requests: {config['max_requests']}\n")
        sys.stdout.write(f"[DRY-RUN] Max Retries: {config['max_retries']}\n")
        sys.exit(0)

    # 3. Concurrency Protection: Acquire exclusive OS-level portal lock
    storage_backend = LocalStateStorageBackend(results_base_dir)
    portal_lock = storage_backend.get_lock(args.portal)
    if not portal_lock.acquire():
        sys.stderr.write(
            f"Error: Could not acquire portal lock for '{args.portal}'. "
            f"Another process is currently holding the lock at '{portal_lock.lock_file_path}'.\n"
        )
        sys.exit(8)

    remote_backend = None
    remote_control_etag = None
    remote_catalog_etag = None
    remote_control_record = None
    heartbeat = None

    if args.storage_backend == "s3":
        from src.s3_storage_backend import S3RemoteStorageBackend, S3LeaseHeartbeat
        remote_backend = S3RemoteStorageBackend(
            bucket=args.s3_bucket,
            prefix=args.s3_prefix,
        )

    try:
        # Pre-run remote storage hydration and lease acquisition
        if remote_backend is not None:
            try:
                remote_control_record, remote_control_etag, remote_catalog, remote_catalog_etag, _ = (
                    remote_backend.hydrate_state(portal_id=args.portal, target_dir=results_base_dir)
                )
                acq_ok, remote_control_record, remote_control_etag = remote_backend.acquire_portal_lease(
                    portal_id=args.portal,
                    runner_id=runner_id,
                    ttl_seconds=2700,
                )
                if not acq_ok:
                    sys.stderr.write(
                        f"Error: Could not acquire remote portal lease for '{args.portal}'. "
                        f"Another runner holds the lease or CAS conflict occurred.\n"
                    )
                    sys.exit(8)
            except Exception as e:
                sys.stderr.write(f"Error: Remote storage pre-run failed: {e}\n")
                sys.exit(1)

        # Optional Catalog Reconciliation
        if args.reconcile_catalog:
            reconciler = CatalogReconciler(results_base_dir, args.portal)
            report = reconciler.reconcile(dry_run=False)
            sys.stdout.write(f"Catalog reconciliation completed for '{args.portal}': {report.details}\n")

        # Optional Staging Cleanup
        if args.cleanup_staging:
            retention_mgr = StorageRetentionManager(results_base_dir, args.portal)
            retention_plan = retention_mgr.execute_cleanup(dry_run=False)
            sys.stdout.write(
                f"Staging retention cleanup completed: removed {len(retention_plan.eligible_for_removal)} folders, "
                f"retained {len(retention_plan.retained_runs)} staging runs.\n"
            )

        # 4. Check robots.txt
        allowed, robots_msg = check_robots_txt(config["head_page_url"], config["user_agent"])
        if not allowed:
            sys.stderr.write(f"Error: robots.txt disallowed crawling for {config['head_page_url']}: {robots_msg}\n")
            sys.exit(3)

        # 5. Resolve run directory
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_folder = f"{args.mode}-{timestamp}"
        run_dir_path = Path(results_base_dir) / "runs" / run_folder
        run_id = f"run-{timestamp}-{args.portal[:6]}"

        # Ensure uniqueness across rapid invocations within the same second
        counter = 1
        target_pkg_path = Path(results_base_dir) / "packages" / args.portal / "runs" / run_id
        while run_dir_path.exists() or target_pkg_path.exists():
            run_folder = f"{args.mode}-{timestamp}-{counter}"
            run_dir_path = Path(results_base_dir) / "runs" / run_folder
            run_id = f"run-{timestamp}-{counter}-{args.portal[:6]}"
            target_pkg_path = Path(results_base_dir) / "packages" / args.portal / "runs" / run_id
            counter += 1

        run_dir = str(run_dir_path)

        # Setup operational logging
        logger = setup_logging(run_dir, log_level=args.log_level, portal_id=args.portal, run_id=run_id)
        logger.info(f"Starting CorpusAI {args.mode.upper()} run for portal '{args.portal}'")
        logger.info(f"Run ID: {run_id}")
        logger.info(f"Target head URL: {config['head_page_url']}")
        logger.info(f"Run directory: {run_dir}")
        logger.info(
            f"Configuration: max_pages={config['max_archive_pages']}, "
            f"max_articles={config['max_articles']}, "
            f"max_requests={config['max_requests']}, "
            f"max_retries={config['max_retries']}"
        )
        if remote_backend is not None and remote_control_record is not None:
            logger.info(
                f"Remote storage (S3): hydrated state and acquired portal lease "
                f"(fencing_gen={remote_control_record.fencing_generation}, runner_id={runner_id})"
            )
            # Start background lease heartbeat
            heartbeat = S3LeaseHeartbeat(
                backend=remote_backend,
                portal_id=args.portal,
                runner_id=runner_id,
                fencing_generation=remote_control_record.fencing_generation,
                initial_etag=remote_control_etag,
                ttl_seconds=remote_control_record.lease.ttl_seconds,
            )
            heartbeat.start()

        # 6. Initialize Authoritative SQLite Storage & Load catalog
        state_db_path = Path(results_base_dir) / "storage" / "corpusai_state.db"
        state_db = CorpusAIStateDatabase(db_path=state_db_path)
        state_repo = CorpusAIRepository(state_db)
        migration_tool = StateMigrationTool(state_db)

        # Ensure portal registered
        state_repo.upsert_portal(
            portal_id=args.portal,
            name=config.get("portal_name", args.portal),
            base_url=config["head_page_url"],
            parser_type="wordpress" if "wordpress" in args.portal.lower() else "generic",
        )

        # Check if legacy catalog exists and needs one-time ingestion into SQLite
        catalog_file_path = Path(catalog_path)
        if catalog_file_path.exists():
            # Check if this portal has already been imported or initialized in SQLite
            if not state_repo.is_portal_migrated(args.portal):
                db_runs = state_repo.list_runs(args.portal, limit=1)
                # Only ingest if database has never had any runs for this portal
                if len(db_runs) == 0:
                    try:
                        migration_tool.import_catalog_json(
                            catalog_path=catalog_file_path,
                            portal_id=args.portal,
                            portal_name=config.get("portal_name", args.portal),
                            base_url=config["head_page_url"],
                        )
                        logger.info(f"Ingested legacy catalog from {catalog_path} into authoritative SQLite storage.")
                    except Exception as mig_err:
                        logger.warning(f"Initial catalog ingestion into SQLite skipped or failed: {mig_err}")
                else:
                    # Mark portal as migrated to prevent checking legacy JSON again
                    state_repo.record_portal_migration(args.portal, True)
        else:
            # Genuinely new portal without legacy catalog: record migration status as complete
            state_repo.record_portal_migration(args.portal, True)

        # Check and reconcile previous interrupted runs (RUNNING or FINALIZING)
        interrupted_runs = state_repo.get_interrupted_runs(args.portal)
        if interrupted_runs:
            for ir in interrupted_runs:
                ir_id = ir["run_id"]
                ir_status = ir.get("status")
                if ir_status == "FINALIZING":
                    recovered_status = state_repo.recover_finalizing_run(
                        portal_id=args.portal,
                        run_id=ir_id,
                        results_base_dir=results_base_dir,
                    )
                    logger.info(
                        f"Reconciled orphaned FINALIZING run '{ir_id}': resolved to '{recovered_status}'."
                    )
                else:
                    logger.warning(
                        f"Detected previous interrupted run '{ir_id}' in non-terminal state '{ir_status}'. "
                        f"Marking as INTERRUPTED. Preserving all prior progress."
                    )
                    state_repo.mark_run_interrupted(
                        portal_id=args.portal,
                        run_id=ir_id,
                        error_message=f"Process interrupted while run was in {ir_status} state.",
                    )

        # Authoritatively resolve and export catalog exclusively from SQLite records
        catalog = migration_tool.export_catalog_json(portal_id=args.portal, dest_path=catalog_file_path)

        # Register the new run in SQLite with RUNNING status
        state_repo.upsert_run(
            portal_id=args.portal,
            run_id=run_id,
            started_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            status="RUNNING",
            run_type=args.mode,
            seed_urls=[config["head_page_url"]],
            metadata={
                "head_page_url": config["head_page_url"],
                "run_dir": f"runs/{run_folder}/",
                "mode": args.mode.upper(),
            },
        )

        # 7. Incremental baseline validation
        previous_state: Optional[PortalState] = None
        if args.mode == "incremental":
            prev_run_id = catalog.latest_successful_run_id
            if not prev_run_id or prev_run_id not in catalog.runs:
                logger.error(f"Cannot run incremental crawl: no successful baseline found in catalog '{catalog_path}'.")
                flush_logger(logger)
                sys.exit(4)

            # Validate baseline package integrity before crawl
            try:
                prev_run_id, prev_state_file, previous_state = validate_baseline_integrity(
                    catalog=catalog,
                    results_base_dir=results_base_dir,
                    portal_id=args.portal,
                )
                logger.info(
                    f"Validated baseline run '{prev_run_id}' with package verification. "
                    f"Loaded {len(previous_state.articles)} articles from '{prev_state_file}'."
                )
            except BaselineIntegrityError as bie:
                # Fallback to local state file if package directory not yet created
                prev_entry = catalog.runs[prev_run_id]
                cand = Path(results_base_dir) / prev_entry.run_dir / "portal_state.json"
                if cand.exists():
                    previous_state = PortalState.load_from_json(str(cand))
                    logger.info(f"Loaded previous state from staging run '{prev_run_id}'.")
                else:
                    logger.error(f"Cannot run incremental crawl: baseline integrity check failed: {bie}")
                    flush_logger(logger)
                    sys.exit(4)
        # 8. Build IncrementalCrawlBudget
        budget = IncrementalCrawlBudget(
            max_archive_pages=config["max_archive_pages"],
            max_articles=config["max_articles"],
            max_requests=config["max_requests"],
            max_retries=config["max_retries"],
        )

        # 9. Harvester instance
        harvester = ResourcePreservationHarvester(
            user_agent=config["user_agent"],
            delay_seconds=config["polite_delay_seconds"],
        )

        warc_content_path = str(Path(run_dir) / "content_html.warc.gz")

        # 10. Fetcher closures and confirmation probe with durable SQLite boundaries
        fetchers = build_portal_fetchers(
            harvester=harvester,
            logger=logger,
            warc_content_path=warc_content_path,
            previous_state=previous_state,
            heartbeat=heartbeat,
        )
        raw_page_fetcher = fetchers["page_fetcher"]
        raw_article_fetcher = fetchers["article_fetcher"]
        confirmation_probe = fetchers["confirmation_probe"]

        def page_fetcher(page_url: str):
            res = raw_page_fetcher(page_url)
            # Durable boundary: persist archive page observation and discovered URLs
            try:
                now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                art_urls = res.get("article_urls", [])
                with state_db.transaction() as conn:
                    state_repo.upsert_archive_page_observation(
                        portal_id=args.portal,
                        run_id=run_id,
                        page_url=page_url,
                        page_number=1,
                        observed_at=now_str,
                        discovered_article_count=len(art_urls),
                        conn=conn,
                    )
                    for a_url in art_urls:
                        u_hash = hashlib.sha256(a_url.encode("utf-8")).hexdigest()
                        state_repo.upsert_discovered_url(
                            portal_id=args.portal,
                            url_hash=u_hash,
                            normalized_url=a_url,
                            first_seen_run_id=run_id,
                            first_seen_at=now_str,
                            last_seen_run_id=run_id,
                            last_seen_at=now_str,
                            conn=conn,
                        )
            except Exception as e:
                logger.debug(f"Intermediate archive page observation persist skipped: {e}")
            return res

        def article_fetcher(article_url: str):
            res = raw_article_fetcher(article_url)
            # Durable boundary: persist article state update immediately
            try:
                now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                u_hash = hashlib.sha256(article_url.encode("utf-8")).hexdigest()
                with state_db.transaction() as conn:
                    state_repo.upsert_article_state(
                        portal_id=args.portal,
                        url_hash=u_hash,
                        article_url=article_url,
                        first_discovered_run_id=run_id,
                        first_discovered_at=now_str,
                        last_extracted_run_id=run_id,
                        last_extracted_at=now_str,
                        extraction_status=res.get("status", "NEW"),
                        warc_record_id=res.get("warc_record_id"),
                        warc_path=res.get("warc_file"),
                        content_sha256=res.get("content_hash"),
                        title=res.get("title"),
                        published_date=res.get("published_at"),
                        metadata=res.get("metadata"),
                        conn=conn,
                    )
            except Exception as e:
                logger.debug(f"Intermediate article state persist skipped: {e}")
            return res

        # 12. Instantiate IncrementalArchivePolicy
        policy = IncrementalArchivePolicy(
            portal_id=args.portal,
            budget=budget,
            previous_state=previous_state,
            head_page_url=config["head_page_url"],
            run_id=run_id,
        )

        # 13. Execute crawl
        try:
            portal_state, inc_run, diff = policy.execute_incremental_crawl(
                page_fetcher=page_fetcher,
                article_fetcher=article_fetcher,
                confirmation_probe=confirmation_probe,
            )
        except Exception as e:
            if heartbeat is not None and not heartbeat.is_healthy():
                logger.error(f"Crawl aborted due to remote lease heartbeat failure: {heartbeat.failure_reason}")
                flush_logger(logger)
                sys.exit(10)
            logger.error(f"Unrecoverable error during crawl execution: {e}", exc_info=True)
            flush_logger(logger)
            sys.exit(1)

        # 14. Log result summary
        logger.info(f"Crawl traversal finished. Convergence status: {inc_run.convergence_status}")
        if heartbeat is not None and not heartbeat.is_healthy():
            logger.error(f"Crawl finished with remote lease heartbeat failure: {heartbeat.failure_reason}. Exiting 10.")
            flush_logger(logger)
            sys.exit(10)
        logger.info(
            f"Article metrics: total={inc_run.article_count}, new={inc_run.new_article_count}, "
            f"unchanged={inc_run.unchanged_article_count}, changed={inc_run.changed_article_count}, "
            f"missing={inc_run.missing_article_count}, failures={inc_run.fetch_failure_count}"
        )
        logger.info(f"Requests made: {inc_run.requests_made} (budget: {config['max_requests']})")
        if inc_run.errors:
            for err in inc_run.errors:
                logger.error(f"Crawl error: {err}")
        if inc_run.warnings:
            for w in inc_run.warnings:
                logger.warning(f"Crawl warning: {w}")
        if inc_run.anomalies_detected:
            for a in inc_run.anomalies_detected:
                logger.warning(f"Anomaly detected: {a}")

        # Save WARC if new primary content was captured
        if inc_run.new_article_count > 0:
            new_article_res = [
                r for r in harvester.downloaded_resources.values()
                if r.resource_type == "html" and r.status_code == 200
            ]
            if new_article_res:
                harvester.save_to_warc(warc_content_path, resources=new_article_res)
                inc_run.artifact_paths["content_html_warc"] = warc_content_path
                logger.info(f"Saved content WARC with {len(new_article_res)} articles to {warc_content_path}")

        # Set log path in run artifacts
        inc_run.artifact_paths["run_log"] = str(Path(run_dir) / "run.log")

        # 15. Save portal_state snapshot and observations to SQLite (authoritative) and export JSON
        manifest_wrapper = {
            "source_manifest": inc_run.to_dict(),
            "archive_order": portal_state.archive_order,
            "head_fingerprint": portal_state.head_fingerprint,
            "boundary_fingerprints": portal_state.boundary_fingerprints,
        }
        with state_db.transaction() as db_conn:
            # 15a. Snapshot header
            state_repo.save_portal_state_snapshot(
                portal_id=args.portal,
                run_id=run_id,
                state_timestamp=portal_state.captured_at,
                last_successful_run=getattr(portal_state, "last_successful_run_id", None),
                last_run_timestamp=portal_state.captured_at,
                total_runs_count=getattr(portal_state, "total_runs_count", 0),
                total_articles_count=len(portal_state.articles),
                total_archive_pages_count=len(portal_state.archive_pages),
                source_manifest=manifest_wrapper,
                conn=db_conn,
            )

            # 15b. Archive page observations
            for page_url, p_state in portal_state.archive_pages.items():
                state_repo.upsert_archive_page_observation(
                    portal_id=args.portal,
                    run_id=run_id,
                    page_url=page_url,
                    page_number=p_state.page_number or 0,
                    observed_at=p_state.observed_at or portal_state.captured_at,
                    content_hash=p_state.ordered_fingerprint,
                    discovered_article_count=len(p_state.article_urls),
                    conn=db_conn,
                )

            # 15c. Discovered URLs and Article states
            for art_url, a_state in portal_state.articles.items():
                url_h = hashlib.sha256(art_url.encode("utf-8")).hexdigest()
                first_seen = a_state.first_seen_run_id or run_id
                last_seen = a_state.last_seen_run_id or run_id
                seen_time = a_state.last_captured_at or portal_state.captured_at

                state_repo.upsert_discovered_url(
                    portal_id=args.portal,
                    url_hash=url_h,
                    normalized_url=art_url,
                    first_seen_run_id=first_seen,
                    first_seen_at=seen_time,
                    last_seen_run_id=last_seen,
                    last_seen_at=seen_time,
                    content_hash=a_state.content_hash,
                    conn=db_conn,
                )
                state_repo.add_url_observation(
                    portal_id=args.portal,
                    url_hash=url_h,
                    run_id=run_id,
                    observed_at=seen_time,
                    content_hash=a_state.content_hash,
                    conn=db_conn,
                )
                state_repo.upsert_article_state(
                    portal_id=args.portal,
                    url_hash=url_h,
                    article_url=art_url,
                    first_discovered_run_id=first_seen,
                    first_discovered_at=seen_time,
                    last_extracted_run_id=last_seen,
                    last_extracted_at=seen_time,
                    extraction_status=a_state.status,
                    warc_record_id=a_state.warc_record_id,
                    warc_path=a_state.warc_file,
                    content_sha256=a_state.content_hash,
                    title=a_state.title,
                    published_date=a_state.published_at,
                    metadata=a_state.metadata,
                    conn=db_conn,
                )

        logger.info(f"Persisted authoritative PortalState snapshot and {len(portal_state.articles)} articles to SQLite.")

        # Export compatibility portal_state.json and run_manifest.json
        state_path = Path(run_dir) / "portal_state.json"
        portal_state.save_to_json(str(state_path))
        logger.info(f"Saved PortalState snapshot to {state_path}")

        manifest_path = Path(run_dir) / "run_manifest.json"
        inc_run.save_to_json(str(manifest_path))
        logger.info(f"Saved run manifest to {manifest_path}")

        # 16. Save run_summary.json
        summary_data: Dict[str, Any] = {
            "run_id": run_id,
            "mode": args.mode.upper(),
            "portal_id": args.portal,
            "head_page_url": config["head_page_url"],
            "convergence_status": inc_run.convergence_status,
            "boundary_status": inc_run.boundary_status,
            "article_count": inc_run.article_count,
            "new_article_count": inc_run.new_article_count,
            "unchanged_article_count": inc_run.unchanged_article_count,
            "changed_article_count": inc_run.changed_article_count,
            "missing_article_count": inc_run.missing_article_count,
            "fetch_failure_count": inc_run.fetch_failure_count,
            "requests_made": inc_run.requests_made,
            "started_at": inc_run.started_at,
            "completed_at": inc_run.completed_at,
            "log_file": str(Path(run_dir) / "run.log"),
            "anomalies": inc_run.anomalies_detected,
            "errors": inc_run.errors,
            "warnings": inc_run.warnings,
            "retry_log": inc_run.retry_log,
        }
        summary_path = Path(run_dir) / "run_summary.json"
        with open(summary_path, "w", encoding="UTF-8") as fh:
            json.dump(summary_data, fh, indent=2, ensure_ascii=False)
        logger.info(f"Saved run summary to {summary_path}")

        # Ensure log output is flushed before hashing
        flush_logger(logger)

        # 17. Compute SHA-256 checksums
        files_to_hash: Dict[str, str] = {
            "portal_state.json": str(state_path),
            "run_manifest.json": str(manifest_path),
            "run_summary.json": str(summary_path),
            "run.log": str(Path(run_dir) / "run.log"),
        }
        if Path(warc_content_path).exists():
            files_to_hash["content_html.warc.gz"] = warc_content_path

        checksums_manifest = generate_sha256_manifest(files_to_hash, base_dir=run_dir)
        checksums_path = Path(run_dir) / "checksums.sha256"
        with open(checksums_path, "w", encoding="UTF-8") as fh:
            fh.write(checksums_manifest)
        logger.info(f"Saved SHA-256 checksums to {checksums_path}")

        cat_entry = CatalogRunEntry(
            run_id=run_id,
            previous_run_id=inc_run.previous_run_id,
            mode=args.mode.upper(),
            started_at=inc_run.started_at,
            completed_at=inc_run.completed_at,
            status=inc_run.convergence_status,
            article_count=inc_run.article_count,
            new_article_count=inc_run.new_article_count,
            changed_article_count=inc_run.changed_article_count,
            run_dir=f"runs/{run_folder}/",
            manual_review_status=inc_run.manual_review_status,
            anomalies=inc_run.anomalies_detected,
        )

        # 18. Verified Packaging on CONVERGED
        if inc_run.convergence_status == "CONVERGED":
            packages_root = str(Path(results_base_dir) / "packages")
            packager = ArchivePackager(archives_root=packages_root)
            extra_artifacts = {
                "portal_state.json": str(state_path),
                "run_manifest.json": str(manifest_path),
                "run_summary.json": str(summary_path),
                "run.log": str(Path(run_dir) / "run.log"),
            }
            content_warc = warc_content_path if Path(warc_content_path).exists() else None

            # Mark run as FINALIZING in SQLite before packaging promotion
            state_repo.upsert_run(
                portal_id=args.portal,
                run_id=run_id,
                started_at=inc_run.started_at,
                status="FINALIZING",
                run_type=args.mode,
            )

            try:
                pkg_dir, verification = packager.package_run(
                    portal_id=args.portal,
                    run_id=run_id,
                    mode=args.mode.upper(),
                    status=inc_run.convergence_status,
                    started_at=inc_run.started_at,
                    completed_at=inc_run.completed_at,
                    target_urls=[config["head_page_url"]],
                    previous_run_id=inc_run.previous_run_id,
                    configuration=config,
                    content_html_warc=content_warc,
                    extra_artifacts=extra_artifacts,
                    article_count=inc_run.article_count,
                    new_article_count=inc_run.new_article_count,
                    changed_article_count=inc_run.changed_article_count,
                    anomalies=inc_run.anomalies_detected,
                    errors=inc_run.errors,
                )

                verifier_fn = getattr(PackageVerifier, "verify", PackageVerifier.verify_package)
                verification = verifier_fn(pkg_dir)

                if not verification.is_valid:
                    logger.error(
                        f"Package verification failed for '{pkg_dir}': missing={verification.missing_files}, "
                        f"mismatches={verification.checksum_mismatches}, errors={verification.metadata_errors}"
                    )
                    state_repo.upsert_run(
                        portal_id=args.portal,
                        run_id=run_id,
                        started_at=inc_run.started_at,
                        completed_at=inc_run.completed_at,
                        status="FAILED",
                        articles_discovered=inc_run.article_count,
                        error_message="Package verification failed",
                    )
                    migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                    logger.info("Exiting with code 5 (package verification failed).")
                    flush_logger(logger)
                    sys.exit(5)

                logger.info(f"Package created and verified at {pkg_dir}")

                # Register package in SQLite
                pkg_checksum = None
                chk_file = Path(pkg_dir) / "checksums.sha256"
                if chk_file.exists():
                    pkg_checksum = hashlib.sha256(chk_file.read_bytes()).hexdigest()

                state_repo.upsert_package(
                    portal_id=args.portal,
                    package_id=run_id,
                    package_path=str(pkg_dir),
                    total_articles=inc_run.article_count,
                    warc_count=1 if content_warc else 0,
                    checksum_sha256=pkg_checksum,
                    manifest_path=str(Path(pkg_dir) / "manifest.json"),
                    package_status="SEALED",
                    sealed_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )

                # Remote storage post-run: stage, verify, commit baseline
                if remote_backend is not None and remote_control_etag is not None:
                    # Check lease heartbeat before staging/publishing
                    if heartbeat is not None:
                        if not heartbeat.is_healthy():
                            logger.error(
                                "Cannot publish remote baseline: remote lease heartbeat failed (%s). "
                                "Aborting publication.",
                                heartbeat.failure_reason,
                            )
                            heartbeat.stop()
                            state_repo.upsert_run(
                                portal_id=args.portal,
                                run_id=run_id,
                                started_at=inc_run.started_at,
                                completed_at=inc_run.completed_at,
                                status="FAILED",
                                error_message=f"Remote lease heartbeat failure: {heartbeat.failure_reason}",
                            )
                            migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                            flush_logger(logger)
                            sys.exit(10)
                        # Stop heartbeat before CAS commit to avoid overlapping renewal writes
                        heartbeat.stop()
                        remote_control_etag = heartbeat.get_latest_etag()

                    try:
                        local_files: Dict[str, str] = {}
                        pkg_path = Path(pkg_dir)
                        for item in pkg_path.iterdir():
                            if item.is_file():
                                local_files[item.name] = str(item)

                        staged_meta = remote_backend.stage_package_artifacts(
                            portal_id=args.portal,
                            run_id=run_id,
                            local_files=local_files,
                        )
                        logger.info(f"Remote storage: staged {len(staged_meta)} package artifacts to S3.")

                        pkg_checksums: Dict[str, str] = {}
                        checksums_file = pkg_path / "checksums.sha256"
                        if checksums_file.exists():
                            with open(checksums_file, encoding="UTF-8") as cs_fh:
                                for line_idx, line in enumerate(cs_fh, start=1):
                                    line = line.strip()
                                    if line and not line.startswith("#"):
                                        parts = line.split(None, 1)
                                        if len(parts) == 2:
                                            digest, rel_p = parts[0], parts[1].strip()
                                            try:
                                                _, clean_rel = resolve_contained_path(pkg_path, rel_p)
                                                pkg_checksums[clean_rel] = digest.lower()
                                            except ValueError as ve:
                                                logger.error(f"Illegal checksum entry in {checksums_file}:{line_idx}: {ve}")
                                                raise

                        is_valid, mismatches = remote_backend.verify_remote_artifacts(
                            portal_id=args.portal,
                            run_id=run_id,
                            expected_checksums=pkg_checksums,
                        )
                        if not is_valid:
                            logger.error(f"Remote artifact verification failed: {mismatches}")
                            qid = remote_backend.quarantine_failed_upload(
                                portal_id=args.portal,
                                run_id=run_id,
                                runner_id=runner_id,
                                reason="Remote checksum mismatch",
                                details={"mismatches": mismatches},
                            )
                            logger.error(f"Quarantined failed upload as {qid}. Prior remote baseline preserved.")
                            state_repo.upsert_run(
                                portal_id=args.portal,
                                run_id=run_id,
                                started_at=inc_run.started_at,
                                completed_at=inc_run.completed_at,
                                status="FAILED",
                                error_message=f"Remote artifact verification failed: {mismatches}",
                            )
                            migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                            flush_logger(logger)
                            sys.exit(9)

                        # Commit baseline to remote control record
                        commit_ok, new_ctrl_etag, new_cat_etag = remote_backend.commit_baseline_and_catalog(
                            portal_id=args.portal,
                            run_id=run_id,
                            catalog=catalog,
                            expected_catalog_etag=remote_catalog_etag,
                            expected_control_etag=remote_control_etag,
                            fencing_generation=remote_control_record.fencing_generation,
                            runner_id=runner_id,
                        )
                        if commit_ok:
                            remote_control_etag = new_ctrl_etag
                            remote_catalog_etag = new_cat_etag
                            logger.info(f"Remote baseline committed: run_id={run_id}, new_control_etag={new_ctrl_etag}")
                        else:
                            logger.error(
                                "Remote baseline commit failed (CAS conflict or stale lease). "
                                "Prior remote baseline preserved."
                            )
                            state_repo.upsert_run(
                                portal_id=args.portal,
                                run_id=run_id,
                                started_at=inc_run.started_at,
                                completed_at=inc_run.completed_at,
                                status="FAILED",
                                error_message="Remote baseline commit failed (CAS conflict)",
                            )
                            migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                            flush_logger(logger)
                            sys.exit(9)

                    except Exception as e:
                        logger.error(f"Remote storage post-run exception: {e}", exc_info=True)
                        state_repo.upsert_run(
                            portal_id=args.portal,
                            run_id=run_id,
                            started_at=inc_run.started_at,
                            completed_at=inc_run.completed_at,
                            status="FAILED",
                            error_message=str(e),
                        )
                        migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                        flush_logger(logger)
                        sys.exit(9)

                # Authoritative Run and Portal update in SQLite
                with state_db.transaction() as commit_conn:
                    state_repo.upsert_run(
                        portal_id=args.portal,
                        run_id=run_id,
                        started_at=inc_run.started_at,
                        completed_at=inc_run.completed_at,
                        status=inc_run.convergence_status,
                        articles_discovered=inc_run.article_count,
                        articles_downloaded=inc_run.article_count,
                        articles_extracted=inc_run.article_count,
                        warc_records=1 if content_warc else 0,
                        run_type=args.mode,
                        seed_urls=[config["head_page_url"]],
                        archive_page_count=inc_run.archive_page_count,
                        pages_revisited=inc_run.pages_revisited,
                        scope_completion=inc_run.scope_completion,
                        full_portal_coverage=inc_run.full_portal_coverage,
                        head_changed=inc_run.head_changed,
                        new_articles_count=inc_run.new_article_count,
                        changed_articles_count=inc_run.changed_article_count,
                        unchanged_articles_count=inc_run.unchanged_article_count,
                        terminal_reason=inc_run.convergence_status,
                        last_checkpoint_timestamp=inc_run.completed_at,
                        interrupted=False,
                        metadata={
                            "previous_run_id": inc_run.previous_run_id,
                            "run_dir": f"runs/{run_folder}/",
                            "manual_review_status": inc_run.manual_review_status,
                            "anomalies": inc_run.anomalies_detected,
                            "errors": inc_run.errors,
                            "warnings": inc_run.warnings,
                            "retry_log": inc_run.retry_log,
                            "requests_made": inc_run.requests_made,
                            "missing_article_count": inc_run.missing_article_count,
                            "fetch_failure_count": inc_run.fetch_failure_count,
                            "reordered_article_count": inc_run.reordered_article_count,
                            "boundary_status": inc_run.boundary_status,
                            "artifact_paths": inc_run.artifact_paths,
                            "mode": args.mode.upper(),
                        },
                        conn=commit_conn,
                    )
                    state_repo.upsert_portal(
                        portal_id=args.portal,
                        name=config.get("portal_name", args.portal),
                        base_url=config["head_page_url"],
                        latest_successful_run_id=run_id,
                        conn=commit_conn,
                    )

                exported_catalog = migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                logger.info(
                    f"Committed run {run_id} to authoritative SQLite and exported catalog to {catalog_path} "
                    f"(latest_successful_run_id={run_id})"
                )
                logger.info("Run finished successfully with status CONVERGED. Exiting 0.")
                flush_logger(logger)
                sys.exit(0)

            except Exception as e:
                logger.error(f"Package creation or verification exception: {e}", exc_info=True)
                state_repo.upsert_run(
                    portal_id=args.portal,
                    run_id=run_id,
                    started_at=inc_run.started_at,
                    completed_at=inc_run.completed_at,
                    status="FAILED",
                    error_message=str(e),
                )
                migration_tool.export_catalog_json(args.portal, Path(catalog_path))
                logger.info("Exiting with code 5 (package verification failed).")
                flush_logger(logger)
                sys.exit(5)

        # 19 & 20. Non-converged catalog registration & exit
        state_repo.upsert_run(
            portal_id=args.portal,
            run_id=run_id,
            started_at=inc_run.started_at,
            completed_at=inc_run.completed_at,
            status=inc_run.convergence_status,
            articles_discovered=inc_run.article_count,
            articles_downloaded=inc_run.article_count,
            articles_extracted=inc_run.article_count,
            warc_records=1 if Path(warc_content_path).exists() else 0,
            run_type=args.mode,
            seed_urls=[config["head_page_url"]],
            archive_page_count=inc_run.archive_page_count,
            pages_revisited=inc_run.pages_revisited,
            scope_completion=inc_run.scope_completion,
            full_portal_coverage=inc_run.full_portal_coverage,
            head_changed=inc_run.head_changed,
            new_articles_count=inc_run.new_article_count,
            changed_articles_count=inc_run.changed_article_count,
            unchanged_articles_count=inc_run.unchanged_article_count,
            terminal_reason=inc_run.convergence_status,
            last_checkpoint_timestamp=inc_run.completed_at,
            interrupted=False,
            error_message="; ".join(inc_run.errors) if inc_run.errors else None,
            metadata={
                "previous_run_id": inc_run.previous_run_id,
                "run_dir": f"runs/{run_folder}/",
                "manual_review_status": inc_run.manual_review_status,
                "anomalies": inc_run.anomalies_detected,
                "errors": inc_run.errors,
                "warnings": inc_run.warnings,
                "retry_log": inc_run.retry_log,
                "requests_made": inc_run.requests_made,
                "missing_article_count": inc_run.missing_article_count,
                "fetch_failure_count": inc_run.fetch_failure_count,
                "reordered_article_count": inc_run.reordered_article_count,
                "boundary_status": inc_run.boundary_status,
                "artifact_paths": inc_run.artifact_paths,
                "mode": args.mode.upper(),
            },
        )
        exported_catalog = migration_tool.export_catalog_json(args.portal, Path(catalog_path))
        logger.info(
            f"Catalog updated for non-converged run "
            f"(latest_successful_run_id remains {exported_catalog.latest_successful_run_id})"
        )

        if inc_run.convergence_status == "BUDGET_EXHAUSTED":
            logger.warning(f"Run exited with BUDGET_EXHAUSTED. Checkpoint preserved in {run_dir}. Exiting 6.")
            flush_logger(logger)
            sys.exit(6)
        elif inc_run.convergence_status == "MANUAL_REVIEW_REQUIRED":
            logger.warning(f"Run exited with MANUAL_REVIEW_REQUIRED due to anomalies. Exiting 7.")
            flush_logger(logger)
            sys.exit(7)
        else:
            logger.error(f"Run exited with failure status '{inc_run.convergence_status}'. Exiting 1.")
            flush_logger(logger)
            sys.exit(1)


    finally:
        portal_lock.release()
        if heartbeat is not None:
            heartbeat.stop()
            remote_control_etag = heartbeat.get_latest_etag()
        if remote_backend is not None and remote_control_record is not None and remote_control_etag is not None:
            try:
                released = remote_backend.release_portal_lease(
                    portal_id=args.portal,
                    runner_id=runner_id,
                    fencing_generation=remote_control_record.fencing_generation,
                    expected_control_etag=remote_control_etag,
                )
                if not released:
                    logging.getLogger(f"corpusai.{args.portal}").warning(
                        "Remote portal lease release failed or was already superseded "
                        "(stale ETag). This is safe to ignore."
                    )
            except Exception as e:
                logging.getLogger(f"corpusai.{args.portal}").warning(
                    f"Remote portal lease release raised exception: {e}. Lease will expire by TTL."
                )
if __name__ == "__main__":
    main()

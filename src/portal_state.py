"""
Portal State model for CorpusAI Phase 6B Incremental Crawling.

Defines deterministic, typed dataclasses for tracking the state of an archive
portal across runs, including article states, archive page states, head/boundary
fingerprints, and JSON serialization.
"""

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
from typing import Any, Dict, List, Optional, Set


@dataclass
class ArticleState:
    """Represents the known state of an individual article in the portal."""
    canonical_url: str
    first_seen_run_id: str
    last_seen_run_id: str
    title: Optional[str] = None
    published_at: Optional[str] = None
    author: Optional[str] = None
    content_hash: Optional[str] = None  # SHA-256 of normalized text/body
    raw_hash: Optional[str] = None      # SHA-256 of raw HTTP response / HTML
    status: str = "UNCHANGED"           # NEW, UNCHANGED, CONTENT_CHANGED, METADATA_CHANGED, MISSING, FETCH_FAILED, UNKNOWN
    content_capture_status: str = "CAPTURED"  # CAPTURED, NOT_CAPTURED_IN_PILOT, REUSED, FAILED
    seen_on_pages: List[str] = field(default_factory=list)
    representation_type: str = "HTML"   # HTML, CONTENT_JSON, BROWSER_WACZ, EXTRACTED_TEXT
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_captured_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArticleState":
        import dataclasses
        valid_keys = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ArchivePageState:
    """Represents the known state of an individual paginated archive page."""
    url: str
    ordered_fingerprint: str
    unordered_fingerprint: str
    article_urls: List[str]
    next_page_url: Optional[str] = None
    previous_page_url: Optional[str] = None
    page_number: Optional[int] = None
    crawl_status: str = "COMPLETE"       # COMPLETE, POTENTIALLY_INCOMPLETE, FETCH_FAILED
    observed_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArchivePageState":
        import dataclasses
        valid_keys = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class PortalState:
    """
    Snapshot of a web portal's known archive state after a completed or checkpointed crawl run.
    """
    portal_id: str
    run_id: str
    schema_version: str = "1"
    captured_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    archive_pages: Dict[str, ArchivePageState] = field(default_factory=dict)  # url -> ArchivePageState
    articles: Dict[str, ArticleState] = field(default_factory=dict)            # canonical_url -> ArticleState
    archive_order: List[str] = field(default_factory=list)                    # Ordered list of article URLs as discovered
    head_fingerprint: Optional[str] = None                                    # Set/Ordered hash of head page
    boundary_fingerprints: Dict[str, str] = field(default_factory=dict)       # boundary page_url -> set_hash
    last_complete_archive_page: Optional[str] = None
    last_potentially_incomplete_page: Optional[str] = None
    total_articles_count: int = 0
    total_archive_pages_count: int = 0
    source_manifest: Optional[str] = None

    def __post_init__(self):
        if not self.total_articles_count:
            self.total_articles_count = len(self.articles)
        if not self.total_archive_pages_count:
            self.total_archive_pages_count = len(self.archive_pages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portal_id": self.portal_id,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "archive_pages": {k: v.to_dict() for k, v in self.archive_pages.items()},
            "articles": {k: v.to_dict() for k, v in self.articles.items()},
            "archive_order": self.archive_order,
            "head_fingerprint": self.head_fingerprint,
            "boundary_fingerprints": self.boundary_fingerprints,
            "last_complete_archive_page": self.last_complete_archive_page,
            "last_potentially_incomplete_page": self.last_potentially_incomplete_page,
            "total_articles_count": len(self.articles),
            "total_archive_pages_count": len(self.archive_pages),
            "source_manifest": self.source_manifest,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortalState":
        pages = {
            k: ArchivePageState.from_dict(v) if isinstance(v, dict) else v
            for k, v in data.get("archive_pages", {}).items()
        }
        articles = {
            k: ArticleState.from_dict(v) if isinstance(v, dict) else v
            for k, v in data.get("articles", {}).items()
        }
        return cls(
            portal_id=data["portal_id"],
            run_id=data["run_id"],
            schema_version=data.get("schema_version", "1"),
            captured_at=data.get("captured_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            archive_pages=pages,
            articles=articles,
            archive_order=data.get("archive_order", []),
            head_fingerprint=data.get("head_fingerprint"),
            boundary_fingerprints=data.get("boundary_fingerprints", {}),
            last_complete_archive_page=data.get("last_complete_archive_page"),
            last_potentially_incomplete_page=data.get("last_potentially_incomplete_page"),
            total_articles_count=data.get("total_articles_count", len(articles)),
            total_archive_pages_count=data.get("total_archive_pages_count", len(pages)),
            source_manifest=data.get("source_manifest"),
        )

    def save_to_json(self, file_path: str) -> None:
        """Persist PortalState to a JSON file atomically using a temporary file."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        tmp_path = f"{file_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)

    @classmethod
    def load_from_json(cls, file_path: str) -> "PortalState":
        """Load PortalState from a JSON file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"PortalState file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

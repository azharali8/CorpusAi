"""
Content URL Registry module for CorpusAI.

Tracks URLs explicitly known/discovered to represent article content,
their discovery provenance, capture status, HTTP metadata, and WARC record associations.
Maintains a dedicated content URL registry separate from generic web crawl frontiers.
"""

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
from typing import Any, Dict, List, Optional


@dataclass
class ContentURLEntry:
    """Represents an entry in the Content URL Registry."""
    url: str
    source_archive: str
    discovered_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    content_role: str = "article"
    capture_status: str = "pending"  # pending, captured, failed, skipped
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    final_url: Optional[str] = None
    payload_sha256: Optional[str] = None
    payload_size_bytes: Optional[int] = None
    warc_record_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContentURLEntry":
        return cls(**data)


class ContentURLRegistry:
    """Manages tracking, queries, and updates of content URLs."""

    def __init__(self, entries: Optional[List[ContentURLEntry]] = None):
        self._entries: Dict[str, ContentURLEntry] = {}
        if entries:
            for entry in entries:
                self.register(entry)

    def register(self, entry: ContentURLEntry) -> ContentURLEntry:
        """Register or update a content URL entry."""
        self._entries[entry.url] = entry
        return entry

    def register_url(
        self,
        url: str,
        source_archive: str,
        content_role: str = "article",
        discovered_at: Optional[str] = None,
    ) -> ContentURLEntry:
        """Register a new URL by url and source archive."""
        if url in self._entries:
            return self._entries[url]

        entry = ContentURLEntry(
            url=url,
            source_archive=source_archive,
            discovered_at=discovered_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            content_role=content_role,
        )
        self._entries[url] = entry
        return entry

    def get(self, url: str) -> Optional[ContentURLEntry]:
        """Retrieve entry by URL."""
        return self._entries.get(url)

    def update_capture_success(
        self,
        url: str,
        http_status: int,
        content_type: str,
        final_url: str,
        payload_sha256: str,
        payload_size_bytes: int,
        warc_record_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ContentURLEntry]:
        """Update entry with successful capture details."""
        entry = self._entries.get(url)
        if not entry:
            return None

        entry.capture_status = "captured"
        entry.http_status = http_status
        entry.content_type = content_type
        entry.final_url = final_url
        entry.payload_sha256 = payload_sha256
        entry.payload_size_bytes = payload_size_bytes
        entry.warc_record_id = warc_record_id
        if metadata:
            entry.metadata = metadata
        entry.error = None
        return entry

    def update_capture_failure(
        self,
        url: str,
        error: str,
        http_status: Optional[int] = None,
    ) -> Optional[ContentURLEntry]:
        """Update entry with capture failure details."""
        entry = self._entries.get(url)
        if not entry:
            return None

        entry.capture_status = "failed"
        entry.error = error
        if http_status is not None:
            entry.http_status = http_status
        return entry

    def list_entries(self) -> List[ContentURLEntry]:
        """Return all registered entries in registration order."""
        return list(self._entries.values())

    def list_by_status(self, status: str) -> List[ContentURLEntry]:
        """Filter entries by capture_status."""
        return [e for e in self._entries.values() if e.capture_status == status]

    def count(self) -> int:
        """Total number of tracked entries."""
        return len(self._entries)

    def save_to_json(self, file_path: str) -> None:
        """Persist registry to a JSON file."""
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        data = [e.to_dict() for e in self._entries.values()]
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def load_from_json(cls, file_path: str) -> "ContentURLRegistry":
        """Load registry from a JSON file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Registry file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = [ContentURLEntry.from_dict(item) for item in data]
        return cls(entries)

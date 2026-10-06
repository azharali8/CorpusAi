"""
Deposit Metadata module for CorpusAI.

Defines repository deposit metadata schemas (Zenodo/Dataverse compatible)
for publishing preserved web corpora with tamper-evident checksums,
provenance lineages, rights declarations, and archive manifests.
"""

from dataclasses import asdict, dataclass, field
import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional


@dataclass
class Creator:
    name: str
    affiliation: Optional[str] = None
    orcid: Optional[str] = None


@dataclass
class DepositMetadata:
    """Deposit metadata for archival corpus releases."""
    title: str
    creators: List[Creator]
    description: str
    capture_period_start: str
    capture_period_end: str
    source_domains: List[str]
    license_status: str  # e.g. "CC-BY-4.0", "InC", "Restricted-Access", "Research-Only"
    copyright_notice: str
    software_version: str
    archive_formats: List[str]  # e.g. ["WARC", "WACZ", "JSON"]
    checksum_manifest_path: str
    related_run_ids: List[str] = field(default_factory=list)
    access_status: str = "open"  # open, restricted, embargoed
    embargo_date: Optional[str] = None
    custom_properties: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DepositMetadata":
        creators_raw = data.get("creators", [])
        creators = [Creator(**c) if isinstance(c, dict) else c for c in creators_raw]
        cleaned_data = {**data, "creators": creators}
        return cls(**cleaned_data)

    def validate(self) -> List[str]:
        """Validate required deposit fields and integrity constraints."""
        errors = []
        if not self.title or len(self.title.strip()) < 5:
            errors.append("Deposit title must be at least 5 characters long.")
        if not self.creators:
            errors.append("At least one creator is required for deposit.")
        if not self.description or len(self.description.strip()) < 20:
            errors.append("Deposit description must be at least 20 characters long.")
        if not self.source_domains:
            errors.append("At least one source domain must be declared.")
        if not self.archive_formats:
            errors.append("At least one archive format (e.g. WARC, WACZ) must be declared.")
        if not self.checksum_manifest_path:
            errors.append("Checksum manifest path is required.")
        if self.access_status == "embargoed" and not self.embargo_date:
            errors.append("Embargo date is required when access status is embargoed.")
        return errors

    def save_to_json(self, file_path: str) -> None:
        """Persist deposit metadata to JSON."""
        errors = self.validate()
        if errors:
            raise ValueError(f"Invalid deposit metadata: {', '.join(errors)}")
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load_from_json(cls, file_path: str) -> "DepositMetadata":
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Deposit metadata file not found: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

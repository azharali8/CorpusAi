"""
Local deposit registry tracking receipts for archival runs.
"""

import datetime
import json
import os
from typing import Dict, Optional

from src.utils.serialization import atomic_write_json
from src.zenodo_stuff.models import DepositReceipt


class DepositRegistry:
    """
    Durable on-disk registry mapping (portal_id, run_id) to DepositReceipt.
    Stored atomically alongside or inside portal results (e.g. deposit_registry.json).
    """

    def __init__(self, registry_path: str):
        self.registry_path = os.path.abspath(registry_path)
        self.receipts: Dict[str, DepositReceipt] = {}  # key: f"{portal_id}:{run_id}"

    @classmethod
    def load_or_create(cls, registry_path: str) -> "DepositRegistry":
        reg = cls(registry_path)
        if os.path.exists(registry_path):
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.get("receipts", {}).items():
                    reg.receipts[k] = DepositReceipt.from_dict(v)
        return reg

    def save(self) -> None:
        """Atomic write to disk to prevent corrupted state on interruption."""
        data = {
            "schema_version": "1",
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "receipts": {k: v.to_dict() for k, v in self.receipts.items()},
        }
        atomic_write_json(self.registry_path, data)

    def get_receipt(self, portal_id: str, run_id: str) -> Optional[DepositReceipt]:
        return self.receipts.get(f"{portal_id}:{run_id}")

    def register_receipt(self, receipt: DepositReceipt) -> None:
        self.receipts[f"{receipt.portal_id}:{receipt.run_id}"] = receipt
        self.save()

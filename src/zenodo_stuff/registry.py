"""
Local deposit registry tracking receipts for archival runs.

Refactored in Stage 9 to delegate all runtime persistence to the authoritative
SQLAlchemy 2.0 SQLite persistence adapter (SADepositPersistenceAdapter).

Operational reads and writes interact directly with SQLite.
deposit_registry.json is retired from being a writable runtime authority,
retained only for explicit offline import, export, or archival interchange.
"""

import datetime
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Union

from src.storage.sa_deposit_adapter import SADepositPersistenceAdapter
from src.utils.serialization import atomic_write_json
from src.zenodo_stuff.models import DepositReceipt

logger = logging.getLogger("corpusai.deposit.registry")


class DepositRegistry:
    """
    Durable registry mapping (portal_id, run_id) to DepositReceipt.
    Delegates persistence to the authoritative CorpusAI SQLite database.
    """

    def __init__(
        self,
        registry_path: Optional[Union[str, Path]] = None,
        db_path: Optional[Union[str, Path]] = None,
        adapter: Optional[SADepositPersistenceAdapter] = None,
    ):
        """
        Initialize the DepositRegistry.

        Database resolution precedence:
        1. Explicitly provided ``adapter`` — used directly; no engine created.
        2. Explicitly provided ``db_path`` — resolved to an absolute path.
        3. If ``registry_path`` is a ``.db`` file or the special string ``:memory:``,
           treat it as the database path.
        4. No database configuration available → raise ``ValueError`` so that callers
           must supply an explicit path.  The previous CWD-relative fallback
           ``Path("storage/corpusai_state.db").resolve()`` is removed because it
           created a silently wrong database when the process working directory
           changed between construction and first use.

        Note: A ``.json`` ``registry_path`` is kept purely as an import/export
        interchange target; it **never** determines the location of the operational
        SQLite database.
        """
        # Store optional interchange path
        if registry_path is not None:
            path_str = str(registry_path)
            if path_str == ":memory:":
                self.registry_path: Union[str, Path] = ":memory:"
            else:
                self.registry_path = Path(registry_path).resolve()
        else:
            self.registry_path = None

        self.receipts: Dict[str, DepositReceipt] = {}  # In-memory cache

        if adapter is not None:
            self._adapter = adapter
            self._owns_adapter = False
        else:
            # Determine authoritative database target
            if db_path is not None:
                if str(db_path) == ":memory:":
                    db_target: Union[str, Path] = ":memory:"
                else:
                    db_target = Path(db_path).resolve()
            elif self.registry_path == ":memory:":
                db_target = ":memory:"
            elif (
                self.registry_path is not None
                and isinstance(self.registry_path, Path)
                and self.registry_path.suffix == ".db"
            ):
                db_target = self.registry_path
            else:
                # No explicit database configuration was provided.
                # Raise instead of silently using a CWD-relative path that could
                # create a second operational database when the working directory
                # differs from the application root.
                raise ValueError(
                    "DepositRegistry requires an explicit db_path, an .db registry_path, "
                    "':memory:', or an already-constructed SADepositPersistenceAdapter. "
                    "Cannot infer the authoritative database location from a JSON path "
                    "or an absent configuration."
                )

            from src.storage.sqlite_backend import CorpusAIStateDatabase
            CorpusAIStateDatabase(db_path=db_target, auto_initialize=True)
            self._adapter = SADepositPersistenceAdapter(db_target)
            self._owns_adapter = True

    @classmethod
    def load_or_create(
        cls,
        registry_path: Union[str, Path],
        db_path: Optional[Union[str, Path]] = None,
        adapter: Optional[SADepositPersistenceAdapter] = None,
    ) -> "DepositRegistry":
        """
        Factory method preserved for backward-compatibility.
        If a legacy JSON file exists, reads it into cache without mutating it.
        """
        reg = cls(registry_path=registry_path, db_path=db_path, adapter=adapter)
        path_obj = Path(registry_path)
        if path_obj.is_file() and str(path_obj).endswith(".json"):
            with open(path_obj, encoding="UTF-8") as fh:
                try:
                    data = json.load(fh)
                    for k, v in data.get("receipts", {}).items():
                        reg.receipts[k] = DepositReceipt.from_dict(v)
                except Exception as e:
                    logger.warning(f"Could not load legacy JSON registry: {e}")
        return reg

    def save(self) -> None:
        """
        Save operation: Writes all in-memory receipts to authoritative database.
        Pure SQLite write path — no automatic JSON mirroring.
        """
        for receipt in list(self.receipts.values()):
            self._adapter.save_receipt(receipt)

    def export_json(self, destination_path: Optional[Union[str, Path]] = None) -> None:
        """
        Explicit offline export of receipts to a JSON file for human inspection or interchange.
        """
        target = Path(destination_path or self.registry_path)
        data = {
            "schema_version": "1",
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "receipts": {k: v.to_dict() for k, v in self.receipts.items()},
        }
        atomic_write_json(str(target), data)

    def get_receipt(self, portal_id: str, run_id: str) -> Optional[DepositReceipt]:
        """
        Query receipt for (portal_id, run_id). Checks in-memory cache then authoritative database.
        """
        cache_key = f"{portal_id}:{run_id}"
        if cache_key in self.receipts:
            return self.receipts[cache_key]

        receipt = self._adapter.get_receipt(portal_id, run_id)
        if receipt is not None:
            self.receipts[cache_key] = receipt
        return receipt

    def register_receipt(self, receipt: DepositReceipt) -> bool:
        """
        Register a new or updated receipt.
        Atomically persists the changed receipt only. If persistence fails with an exception,
        the in-memory cache is reverted and not left in an inconsistent state.
        Returns True if persisted, False if skipped (e.g. unregistered package).
        """
        cache_key = f"{receipt.portal_id}:{receipt.run_id}"
        prev_receipt = self.receipts.get(cache_key)

        try:
            persisted = self._adapter.save_receipt(receipt)
            self.receipts[cache_key] = receipt
            return persisted
        except Exception:
            # Revert cache on persistence failure
            if prev_receipt is not None:
                self.receipts[cache_key] = prev_receipt
            else:
                self.receipts.pop(cache_key, None)
            raise

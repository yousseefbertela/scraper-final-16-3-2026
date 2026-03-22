"""
Checkpoint manager - persists scraping progress to PostgreSQL only.
DB is the sole source of truth. No local files are read or written.

Checkpoint structure (stored as JSON text in scraped_files table):
{
  "last_updated": "2026-03-16T10:30:00",
  "cars": {
    "VA99-EGY-05-2005-E90-BMW-320i": {
      "completed": true,
      "completed_groups": ["01", "02", ...],
      "completed_subgroups": {"03": ["03_2479", ...]},
      "in_progress_group": null
    }
  }
}
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class CheckpointManager:
    def __init__(self, filepath: str):
        # filepath kept for filename derivation only — not written to
        self._filename = os.path.basename(filepath)
        self.data = self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def is_car_done(self, type_code_full: str) -> bool:
        entry = self.data["cars"].get(type_code_full, {})
        return entry.get("completed", False)

    def is_group_done(self, type_code_full: str, mg: str) -> bool:
        entry = self.data["cars"].get(type_code_full, {})
        return mg in entry.get("completed_groups", [])

    def is_subgroup_done(self, type_code_full: str, mg: str, diag_id: str) -> bool:
        entry = self.data["cars"].get(type_code_full, {})
        return diag_id in entry.get("completed_subgroups", {}).get(mg, [])

    def mark_subgroup_done(self, car: dict, mg: str, diag_id: str):
        key = car["type_code_full"]
        self._ensure(key)
        subs = self.data["cars"][key].setdefault("completed_subgroups", {})
        if mg not in subs:
            subs[mg] = []
        if diag_id not in subs[mg]:
            subs[mg].append(diag_id)
        self._save()

    def set_in_progress(self, car: dict, mg: str):
        key = car["type_code_full"]
        self._ensure(key)
        self.data["cars"][key]["in_progress_group"] = mg
        self._save()

    def mark_group_done(self, car: dict, mg: str):
        key = car["type_code_full"]
        self._ensure(key)
        entry = self.data["cars"][key]
        if mg not in entry["completed_groups"]:
            entry["completed_groups"].append(mg)
        entry["in_progress_group"] = None
        entry.get("completed_subgroups", {}).pop(mg, None)
        self._save()
        logger.debug(f"Checkpoint: group {mg} done for {key}")

    def mark_car_done(self, car: dict):
        key = car["type_code_full"]
        self._ensure(key)
        self.data["cars"][key]["completed"] = True
        self.data["cars"][key]["in_progress_group"] = None
        self.data["cars"][key]["completed_subgroups"] = {}
        self._save()
        logger.info(f"Checkpoint: car done - {key}")

    def get_done_prefixes(self) -> set:
        """Return 4-char type-code prefixes for all completed cars."""
        return {
            tc[:4]
            for tc, entry in self.data["cars"].items()
            if entry.get("completed") and len(tc) >= 4
        }

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _ensure(self, key: str):
        if key not in self.data["cars"]:
            self.data["cars"][key] = {
                "completed": False,
                "completed_groups": [],
                "completed_subgroups": {},
                "in_progress_group": None,
            }
        else:
            if "completed_subgroups" not in self.data["cars"][key]:
                self.data["cars"][key]["completed_subgroups"] = {}

    def _load(self) -> dict:
        """Load from PostgreSQL only. Start fresh if not found."""
        try:
            from storage.db import get_file_content
            content = get_file_content(self._filename)
            if content:
                data = json.loads(content)
                logger.info("Loaded checkpoint from PostgreSQL")
                return data
        except Exception as e:
            logger.warning(f"Could not load checkpoint from DB: {e}")
        logger.info("Starting fresh checkpoint (nothing in DB)")
        return {"last_updated": None, "cars": {}}

    def _save(self):
        self.data["last_updated"] = datetime.utcnow().isoformat()
        try:
            from storage.db import sync_file
            content = json.dumps(self.data, indent=2, ensure_ascii=False)
            sync_file(self._filename, content)
        except Exception as e:
            logger.error(f"Failed to save checkpoint to DB: {e}")

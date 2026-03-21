"""
Checkpoint manager - persists scraping progress to disk.
Allows the scraper to resume after an interruption without re-scraping
already-completed groups OR subgroups.

Checkpoint file structure (JSON):
{
  "last_updated": "2026-03-16T10:30:00",
  "cars": {
    "VA99-EGY-05-2005-E90-BMW-320i": {
      "completed": true,
      "completed_groups": ["01", "02", ...],
      "completed_subgroups": {"03": ["03_2479", "03_0059", ...]},
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
        self.filepath = filepath
        self._tmp = filepath + ".tmp"
        self.data = self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def is_car_done(self, type_code_full: str) -> bool:
        """Return True if the car is fully scraped."""
        entry = self.data["cars"].get(type_code_full, {})
        return entry.get("completed", False)

    def is_group_done(self, type_code_full: str, mg: str) -> bool:
        """Return True if this main-group has already been scraped."""
        entry = self.data["cars"].get(type_code_full, {})
        return mg in entry.get("completed_groups", [])

    def is_subgroup_done(self, type_code_full: str, mg: str, diag_id: str) -> bool:
        """Return True if this subgroup has already been scraped."""
        entry = self.data["cars"].get(type_code_full, {})
        return diag_id in entry.get("completed_subgroups", {}).get(mg, [])

    def mark_subgroup_done(self, car: dict, mg: str, diag_id: str):
        """Mark a single subgroup as completed and persist immediately."""
        key = car["type_code_full"]
        self._ensure(key)
        subs = self.data["cars"][key].setdefault("completed_subgroups", {})
        if mg not in subs:
            subs[mg] = []
        if diag_id not in subs[mg]:
            subs[mg].append(diag_id)
        self._save()

    def set_in_progress(self, car: dict, mg: str):
        """Mark a group as currently being scraped."""
        key = car["type_code_full"]
        self._ensure(key)
        self.data["cars"][key]["in_progress_group"] = mg
        self._save()

    def mark_group_done(self, car: dict, mg: str):
        """Move a group from in-progress to completed."""
        key = car["type_code_full"]
        self._ensure(key)
        entry = self.data["cars"][key]
        if mg not in entry["completed_groups"]:
            entry["completed_groups"].append(mg)
        entry["in_progress_group"] = None
        # Clear subgroup tracking for this group (no longer needed, saves space)
        entry.get("completed_subgroups", {}).pop(mg, None)
        self._save()
        logger.debug(f"Checkpoint: group {mg} done for {key}")

    def mark_car_done(self, car: dict):
        """Mark the entire car as fully scraped."""
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
            # Migrate old entries that don't have completed_subgroups
            if "completed_subgroups" not in self.data["cars"][key]:
                self.data["cars"][key]["completed_subgroups"] = {}

    def _load(self) -> dict:
        # PostgreSQL is the primary source of truth -- read directly, no local file needed
        try:
            from storage.db import get_file_content
            content = get_file_content(os.path.basename(self.filepath))
            if content:
                data = json.loads(content)
                logger.info("Loaded checkpoint from PostgreSQL")
                return data
        except Exception as e:
            logger.warning(f"Could not load checkpoint from DB ({e}), trying local file...")
        # Fall back to local file (DB unavailable or very first run)
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"Loaded checkpoint from {self.filepath}")
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load checkpoint ({e}), starting fresh.")
        return {"last_updated": None, "cars": {}}

    def _save(self):
        self.data["last_updated"] = datetime.utcnow().isoformat()
        try:
            with open(self._tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(self._tmp, self.filepath)
        except OSError as e:
            logger.error(f"Failed to save checkpoint: {e}")
            return
        # Sync to DB (non-blocking)
        try:
            from storage.db import sync_file_from_path
            sync_file_from_path(self.filepath)
        except Exception:
            pass

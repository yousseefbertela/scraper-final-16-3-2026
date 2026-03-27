"""
storage/notes.py
Persists scraped parts data to PostgreSQL.

Each car's data is stored under its 4-char prefix key in scraped_files:
  filename = "VA99"
  content  = {"VA99-EGY-05-2005-E90-BMW-320i": { ...car_data_with_groups... }}

A per-scraper resume file is also maintained so in-progress cars survive restarts:
  filename = "scraper_1_notes"  (where 1 = SCRAPER_ID)

No local files are written.
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotesWriter:
    def __init__(self, filepath: str):
        # filepath param kept for API compatibility; not actually written to
        from config import SCRAPER_ID
        self._scraper_id = SCRAPER_ID
        self._resume_key = f"scraper_{SCRAPER_ID}_notes"
        self.data = self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def save_subgroup(self, car, group, subgroup, diagram_url, parts, error=None):
        """Merge one subgroup's data into the in-memory tree. Call flush() to persist."""
        series_key = car["series_value"]
        type_key   = car["type_code_full"]
        mg_key     = group["mg"]
        diag_key   = subgroup["diagId"]

        if series_key not in self.data["data"]:
            self.data["data"][series_key] = {"series_label": car["series_label"], "models": {}}

        series_node = self.data["data"][series_key]

        if type_key not in series_node["models"]:
            series_node["models"][type_key] = {
                "series_value":   car["series_value"],
                "series_label":   car["series_label"],
                "body":           car["body"],
                "model":          car["model"],
                "market":         car["market"],
                "prod_month":     car["prod_month"],
                "engine":         car["engine"],
                "steering":       car["steering"],
                "type_code_full": car["type_code_full"],
                "groups": {},
            }

        model_node = series_node["models"][type_key]

        if mg_key not in model_node["groups"]:
            model_node["groups"][mg_key] = {"group_name": group["name"], "subgroups": {}}

        entry = {
            "subgroup_name":     subgroup["name"],
            "diagram_image_url": diagram_url,
            "scraped_at":        datetime.utcnow().isoformat(),
            "parts":             parts,
        }
        if error:
            entry["scrape_error"] = str(error)
        model_node["groups"][mg_key]["subgroups"][diag_key] = entry

        logger.debug(f"Buffered subgroup {diag_key} ({len(parts)} parts) for {type_key}/{mg_key}")

    def flush(self):
        """Persist all in-memory data to PostgreSQL. Called after every group."""
        self._write_to_db()
        logger.info("Saved group to DB")

    def get_car_dict(self, type_code_full: str):
        """Return a car metadata dict from already-saved notes (used to resume in-progress cars)."""
        for series_data in self.data["data"].values():
            model = series_data.get("models", {}).get(type_code_full)
            if model:
                return {
                    "series_value":   model.get("series_value", ""),
                    "series_label":   model.get("series_label", ""),
                    "body":           model.get("body", ""),
                    "model":          model.get("model", ""),
                    "market":         model.get("market", "EGY"),
                    "prod_month":     model.get("prod_month", ""),
                    "engine":         model.get("engine", ""),
                    "steering":       model.get("steering", ""),
                    "type_code_full": type_code_full,
                }
        return None

    # ── Internal helpers ──────────────────────────────────────────────────

    def _load(self) -> dict:
        """Load per-scraper resume data from DB. Start fresh if not found."""
        try:
            from storage.db import get_file_content
            content = get_file_content(self._resume_key)
            if content:
                data = json.loads(content)
                logger.info(f"Loaded notes from DB ({self._resume_key})")
                return data
        except Exception as e:
            logger.warning(f"Could not load notes from DB: {e}")
        logger.info("Starting fresh notes")
        return {
            "meta": {
                "created_at":   datetime.utcnow().isoformat(),
                "last_updated": None,
                "version":      "1.0",
            },
            "data": {},
        }

    def _write_to_db(self):
        """
        Save each type_code's data under its 4-char prefix key (e.g. 'NA36').
        Also save the full scraper notes for resume capability.
        """
        self.data["meta"]["last_updated"] = datetime.utcnow().isoformat()
        from storage.db import sync_file

        # 1. Per-prefix saves (each scraper writes to its own unique prefix keys)
        for series_data in self.data["data"].values():
            for tc, car_data in series_data.get("models", {}).items():
                prefix = tc[:4]
                sync_file(prefix, json.dumps({tc: car_data}, ensure_ascii=False))

        # 2. Scraper-specific resume file
        content = json.dumps(self.data, ensure_ascii=False)
        sync_file(self._resume_key, content)

"""
Notes writer - persists scraped parts data to a structured JSON file.

File is written atomically (write to .tmp, then os.replace) after every
group so data is never lost even if the process is killed mid-run.

After every atomic write the file is also mirrored to PostgreSQL via
storage.db.sync_file_from_path (non-blocking; silently ignored if DB is
unavailable).

JSON structure:
{
  "meta": { "created_at": "...", "last_updated": "...", "version": "1.0" },
  "data": {
    "<series_value>": {
      "series_label": "3' E90 (2004 - 2023)",
      "models": {
        "<type_code_full>": {
          "series_value": "E90", "series_label": "...", "body": "Lim",
          "model": "320i", "market": "EGY", "prod_month": "200805",
          "engine": "N46", "steering": "Left hand drive",
          "type_code_full": "VA99-EGY-05-2005-E90-BMW-320i",
          "groups": {
            "<mg>": {
              "group_name": "ENGINE",
              "subgroups": {
                "<diagId>": {
                  "subgroup_name": "SHORT ENGINE",
                  "diagram_image_url": "https://...",
                  "scraped_at": "...",
                  "parts": [ { ... } ]
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class NotesWriter:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._tmp = filepath + ".tmp"
        self.data = self._load()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def save_subgroup(self, car, group, subgroup, diagram_url, parts):
        """
        Merge one subgroup's data into the in-memory tree only (no disk write).
        Call flush() after all subgroups in a group are done to persist.
        """
        series_key = car["series_value"]
        type_key   = car["type_code_full"]
        mg_key     = group["mg"]
        diag_key   = subgroup["diagId"]

        if series_key not in self.data["data"]:
            self.data["data"][series_key] = {
                "series_label": car["series_label"],
                "models": {},
            }

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
            model_node["groups"][mg_key] = {
                "group_name": group["name"],
                "subgroups": {},
            }

        group_node = model_node["groups"][mg_key]

        group_node["subgroups"][diag_key] = {
            "subgroup_name":     subgroup["name"],
            "diagram_image_url": diagram_url,
            "scraped_at":        datetime.utcnow().isoformat(),
            "parts":             parts,
        }

        logger.debug(
            f"Buffered subgroup {diag_key} ({len(parts)} parts) "
            f"for {type_key} / group {mg_key}"
        )

    def flush(self):
        """Flush the in-memory tree to disk and PostgreSQL. Call after every group."""
        self._atomic_write()
        logger.debug("Flushed notes to disk and DB")

    def get_car_dict(self, type_code_full: str):
        """
        Extract a car metadata dict from already-saved notes data.
        Used on startup to resume an in-progress car without re-enumerating
        the RealOEM dropdowns.
        Returns None if the car has not been saved to notes yet.
        """
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

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        # PostgreSQL is the primary source of truth -- read directly, no local file needed
        try:
            from storage.db import get_file_content
            content = get_file_content(os.path.basename(self.filepath))
            if content:
                data = json.loads(content)
                logger.info("Loaded notes from PostgreSQL")
                return data
        except Exception as e:
            logger.warning(f"Could not load notes from DB ({e}), trying local file...")
        # Fall back to local file (DB unavailable or very first run)
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"Loaded existing notes from {self.filepath}")
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load notes ({e}), starting fresh.")
        return {
            "meta": {
                "created_at": datetime.utcnow().isoformat(),
                "last_updated": None,
                "version": "1.0",
            },
            "data": {},
        }

    def _atomic_write(self):
        self.data["meta"]["last_updated"] = datetime.utcnow().isoformat()
        try:
            with open(self._tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            os.replace(self._tmp, self.filepath)
        except OSError as e:
            logger.error(f"Failed to write notes: {e}")
            return
        try:
            from storage.db import sync_file_from_path
            sync_file_from_path(self.filepath)
        except Exception:
            pass

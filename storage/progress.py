"""
storage/progress.py
Tracks which type-code prefixes (first 4 chars) have been scraped.

Output file: data/scraped_progress.csv
Columns: type_code, prefix, status, timestamp, parts_count

"prefix" is the first 4 characters of the type_code (e.g. "VA99").
If a prefix is marked "completed", any car with that same prefix is skipped.
"""

import csv
import io
import logging
import os
from datetime import datetime

from config import PROGRESS_FILE

logger = logging.getLogger(__name__)

_HEADERS = ["type_code", "prefix", "status", "timestamp", "parts_count"]


class ProgressWriter:
    def __init__(self, filepath: str = PROGRESS_FILE):
        self.filepath = filepath
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        self._ensure_header()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def mark_started(self, type_code: str):
        """Record that scraping of this type_code has begun."""
        self._append(type_code, "started", 0)

    def mark_completed(self, type_code: str, parts_count: int):
        """Record that scraping of this type_code finished successfully."""
        self._append(type_code, "completed", parts_count)

    def get_scraped_prefixes(self) -> set:
        """
        Return the set of 4-char prefixes where status == 'completed'.
        NOTE: audit log only -- not used for skip decisions (checkpoint is the source of truth).
        Reads from PostgreSQL first, falls back to local file.
        """
        prefixes = set()
        # Try DB first
        try:
            from storage.db import get_file_content
            content = get_file_content(os.path.basename(self.filepath))
            if content:
                for row in csv.DictReader(io.StringIO(content)):
                    if row.get("status") == "completed":
                        p = (row.get("prefix") or "").strip()
                        if p:
                            prefixes.add(p)
                return prefixes
        except Exception as e:
            logger.warning(f"Could not read progress from DB ({e}), trying local file...")
        # Fall back to local file
        if not os.path.exists(self.filepath):
            return prefixes
        try:
            with open(self.filepath, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("status") == "completed":
                        p = (row.get("prefix") or "").strip()
                        if p:
                            prefixes.add(p)
        except Exception as e:
            logger.warning(f"Could not read progress file: {e}")
        return prefixes

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _ensure_header(self):
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(_HEADERS)

    def _append(self, type_code: str, status: str, parts_count: int):
        prefix = type_code[:4] if len(type_code) >= 4 else type_code
        row = [
            type_code,
            prefix,
            status,
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            parts_count,
        ]
        try:
            with open(self.filepath, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
            logger.debug(f"Progress: {type_code} -> {status} ({parts_count} parts)")
        except OSError as e:
            logger.warning(f"Progress write failed: {e}")
        # Sync to DB
        try:
            from storage.db import sync_file_from_path
            sync_file_from_path(self.filepath)
        except Exception as e:
            logger.debug(f"Progress DB sync skipped: {e}")

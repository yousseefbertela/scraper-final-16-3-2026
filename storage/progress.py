"""
storage/progress.py
Audit log for scraping progress. Stored in PostgreSQL only — no local file.

Columns: type_code, prefix, status, timestamp, parts_count
NOTE: audit log only — not used for skip decisions (checkpoint is the source of truth).
"""

import csv
import io
import logging
import os
from datetime import datetime

from config import PROGRESS_FILE

logger = logging.getLogger(__name__)

_HEADERS = ["type_code", "prefix", "status", "timestamp", "parts_count"]
_FILENAME = "scraped_progress.csv"


class ProgressWriter:
    def __init__(self, filepath: str = PROGRESS_FILE):
        self._filename = _FILENAME
        self._rows = self._load_rows()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def mark_started(self, type_code: str):
        self._append(type_code, "started", 0)

    def mark_completed(self, type_code: str, parts_count: int):
        self._append(type_code, "completed", parts_count)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _load_rows(self) -> list:
        """Load existing rows from DB."""
        try:
            from storage.db import get_file_content
            content = get_file_content(self._filename)
            if content:
                rows = list(csv.DictReader(io.StringIO(content)))
                logger.info(f"Loaded {len(rows)} progress rows from DB")
                return rows
        except Exception as e:
            logger.warning(f"Could not load progress from DB: {e}")
        return []

    def _append(self, type_code: str, status: str, parts_count: int):
        prefix = type_code[:4] if len(type_code) >= 4 else type_code
        self._rows.append({
            "type_code":   type_code,
            "prefix":      prefix,
            "status":      status,
            "timestamp":   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "parts_count": parts_count,
        })
        self._sync_to_db()
        logger.debug(f"Progress: {type_code} -> {status} ({parts_count} parts)")

    def _sync_to_db(self):
        try:
            from storage.db import sync_file
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=_HEADERS)
            writer.writeheader()
            writer.writerows(self._rows)
            sync_file(self._filename, buf.getvalue())
        except Exception as e:
            logger.warning(f"Progress DB sync failed: {e}")

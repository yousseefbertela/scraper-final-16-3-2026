"""
storage/db.py
DigitalOcean PostgreSQL storage for 5-scraper parallel architecture.

Tables:
  scraped_files       – per-prefix parts data  (filename=4-char-prefix, content=JSON)
  scraper_car_lists   – per-scraper target car list  (scraper_id INT PK, car_data JSONB)
  scraper_checkpoints – per-scraper checkpoint        (scraper_id INT PK, checkpoint_data JSONB)

Advisory lock key 42 is shared across all 5 instances to serialise scraped_files writes.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_DB_AVAILABLE = False


# ── Connection ────────────────────────────────────────────────────────────────

def get_conn():
    """Return a new psycopg2 connection to the DO PostgreSQL database."""
    from config import (
        DO_DB_HOST, DO_DB_PORT, DO_DB_NAME,
        DO_DB_USER, DO_DB_PASSWORD, DO_DB_SSLMODE,
    )
    import psycopg2
    return psycopg2.connect(
        host=DO_DB_HOST,
        port=DO_DB_PORT,
        dbname=DO_DB_NAME,
        user=DO_DB_USER,
        password=DO_DB_PASSWORD,
        sslmode=DO_DB_SSLMODE,
        connect_timeout=15,
    )


# Keep the old alias used elsewhere
def _get_conn():
    return get_conn()


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def ensure_table():
    """Create all required tables if they don't exist. Call at startup."""
    global _DB_AVAILABLE
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scraped_files (
                        filename   TEXT PRIMARY KEY,
                        content    TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scraper_car_lists (
                        scraper_id INT PRIMARY KEY,
                        car_data   JSONB NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scraper_checkpoints (
                        scraper_id      INT PRIMARY KEY,
                        checkpoint_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at      TIMESTAMP DEFAULT NOW()
                    )
                """)
            conn.commit()
        _DB_AVAILABLE = True
        logger.info("DB ready: all tables exist")
    except Exception as e:
        logger.warning(f"DB not available ({e}), continuing without DB sync")
        _DB_AVAILABLE = False


# ── Locked write for scraped parts ────────────────────────────────────────────

def save_with_lock(filename: str, content: str):
    """
    Acquire pg_advisory_lock(42), upsert filename→content in scraped_files,
    release lock, commit.  Safe to call from 5 concurrent scrapers.
    """
    from config import DO_ADVISORY_LOCK_KEY
    if not _DB_AVAILABLE:
        logger.warning(f"DB not available — skipping save_with_lock for {filename}")
        return
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (DO_ADVISORY_LOCK_KEY,))
        try:
            cur.execute("""
                INSERT INTO scraped_files (filename, content, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (filename) DO UPDATE
                    SET content    = EXCLUDED.content,
                        updated_at = NOW()
            """, (filename, content))
            conn.commit()
            logger.debug(f"DB save_with_lock: {filename}")
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DO_ADVISORY_LOCK_KEY,))
            conn.commit()
            cur.close()
            conn.close()
    except Exception as e:
        logger.warning(f"save_with_lock failed for {filename}: {e}")


# ── Per-scraper car list ──────────────────────────────────────────────────────

def get_car_list(scraper_id: int) -> list:
    """
    Return the car list for scraper_id from scraper_car_lists.
    Each item is a dict: {num, code, brand, model, series, body, engine, market, prod_month}.
    Returns [] if not found or on error.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT car_data FROM scraper_car_lists WHERE scraper_id = %s",
                    (scraper_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            return data
    except Exception as e:
        logger.warning(f"get_car_list({scraper_id}) failed: {e}")
    return []


# ── Per-scraper checkpoint ────────────────────────────────────────────────────

def save_checkpoint(scraper_id: int, data: dict):
    """
    Upsert checkpoint data for scraper_id into scraper_checkpoints.
    data should be the full checkpoint dict (same structure as old checkpoint.json).
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scraper_checkpoints (scraper_id, checkpoint_data, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (scraper_id) DO UPDATE
                        SET checkpoint_data = EXCLUDED.checkpoint_data,
                            updated_at      = NOW()
                """, (scraper_id, json.dumps(data)))
            conn.commit()
        logger.debug(f"save_checkpoint({scraper_id}) OK")
    except Exception as e:
        logger.warning(f"save_checkpoint({scraper_id}) failed: {e}")


def load_checkpoint(scraper_id: int) -> dict:
    """
    Load checkpoint data for scraper_id from scraper_checkpoints.
    Returns {} if not found.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checkpoint_data FROM scraper_checkpoints WHERE scraper_id = %s",
                    (scraper_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            data = row[0]
            if isinstance(data, str):
                data = json.loads(data)
            return data
    except Exception as e:
        logger.warning(f"load_checkpoint({scraper_id}) failed: {e}")
    return {}


# ── Legacy helpers (kept for backward compatibility) ──────────────────────────

def sync_file(filename: str, content: str):
    """Upsert file content into scraped_files. Silent on failure."""
    if not _DB_AVAILABLE:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO scraped_files (filename, content, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (filename) DO UPDATE
                        SET content    = EXCLUDED.content,
                            updated_at = NOW()
                """, (filename, content))
            conn.commit()
        logger.debug(f"DB synced: {filename}")
    except Exception as e:
        logger.warning(f"DB sync failed for {filename}: {e}")


def sync_file_from_path(filepath: str):
    """Read a local file and sync its content to DB. Silent on failure."""
    if not _DB_AVAILABLE:
        return
    try:
        filename = os.path.basename(filepath)
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
        sync_file(filename, content)
    except Exception as e:
        logger.warning(f"DB sync from path failed ({filepath}): {e}")


def restore_file_to_path(filename: str, filepath: str) -> bool:
    """
    Restore a file from DB to a local path.
    Returns True if content was found and written, False otherwise.
    """
    if not _DB_AVAILABLE:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM scraped_files WHERE filename = %s",
                    (filename,),
                )
                row = cur.fetchone()
        if row and row[0]:
            os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(row[0])
            logger.info(f"Restored {filename} from DB -> {filepath}")
            return True
    except Exception as e:
        logger.warning(f"DB restore failed for {filename}: {e}")
    return False


def get_file_content(filename: str):
    """Return file content from DB as a string, or None if not found."""
    if not _DB_AVAILABLE:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM scraped_files WHERE filename = %s",
                    (filename,),
                )
                row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.warning(f"DB get_file_content failed for {filename}: {e}")
        return None

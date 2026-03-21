"""
storage/db.py
PostgreSQL file-mirror sync.

Stores file contents (notes JSON, progress CSV, checkpoint JSON, logs)
in a single table so they can be inspected from any Postgres client
even when the Railway filesystem is ephemeral.

Table schema:
    scraped_files(filename TEXT PK, content TEXT, updated_at TIMESTAMP)
"""

import logging
import os

logger = logging.getLogger(__name__)

_DB_AVAILABLE = False


def _get_conn():
    from config import DATABASE_URL
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def ensure_table():
    """Create the scraped_files table if it doesn't exist. Must be called at startup."""
    global _DB_AVAILABLE
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scraped_files (
                        filename   TEXT PRIMARY KEY,
                        content    TEXT,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
            conn.commit()
        _DB_AVAILABLE = True
        logger.info("DB ready: scraped_files table exists")
    except Exception as e:
        logger.warning(f"DB not available ({e}), continuing without DB sync")
        _DB_AVAILABLE = False


def sync_file(filename: str, content: str):
    """Upsert file content into DB. Silent on failure."""
    if not _DB_AVAILABLE:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO scraped_files (filename, content, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (filename) DO UPDATE
                        SET content = EXCLUDED.content,
                            updated_at = NOW()
                    """,
                    (filename, content),
                )
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
    Useful on Railway restart when the filesystem is empty.
    """
    if not _DB_AVAILABLE:
        return False
    try:
        with _get_conn() as conn:
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
    """
    Return file content from DB as a string, or None if not found.
    Used by storage classes to read directly from DB without writing to disk.
    """
    if not _DB_AVAILABLE:
        return None
    try:
        with _get_conn() as conn:
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

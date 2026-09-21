"""
storage/jobs.py
Shared, per-group work queue so several worker instances can scrape ONE car at
the same time — each instance on its own browser and proxy IP.

Table catalog_group_jobs: one row per (car, main group).
  pending  → nobody has it
  claimed  → an instance is scraping it (claimed_at doubles as a heartbeat and
             is refreshed after every subgroup; a claim with a stale heartbeat
             is handed to another instance, which covers crashed/redeployed pods)
  done     → merged into scraped_files

Everything here is written straight to Postgres — there is no in-memory state
that another instance could clobber. The legacy per-scraper checkpoint row is
still kept up to date (derived from this table) because PartPilot's backend
reads it for the customer-facing progress bar.
"""

import json
import logging
import zlib
from datetime import datetime

from storage.db import get_conn

logger = logging.getLogger(__name__)

STALE_CLAIM_SECONDS = 180  # no heartbeat for this long ⇒ instance is gone


def ensure_table():
    # Several instances boot at once; CREATE IF NOT EXISTS still races on the
    # catalog insert, so serialise it and shrug off the duplicate-key loser.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(4243)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS catalog_group_jobs (
                    type_code_full TEXT NOT NULL,
                    mg             TEXT NOT NULL,
                    group_name     TEXT NOT NULL DEFAULT '',
                    status         TEXT NOT NULL DEFAULT 'pending',
                    claimed_by     TEXT,
                    claimed_at     TIMESTAMPTZ,
                    done_at        TIMESTAMPTZ,
                    parts_count    INT NOT NULL DEFAULT 0,
                    PRIMARY KEY (type_code_full, mg)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS catalog_group_jobs_status_idx
                ON catalog_group_jobs (type_code_full, status)
            """)
        conn.commit()


def _car_lock_key(type_code_full: str) -> int:
    # Advisory locks take a bigint; a crc32 of the car id is plenty for our few cars.
    return zlib.crc32(type_code_full.encode("utf-8"))


# ── Seeding ─────────────────────────────────────────────────────────────────

def has_jobs(type_code_full: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM catalog_group_jobs WHERE type_code_full = %s LIMIT 1",
                        (type_code_full,))
            return cur.fetchone() is not None


def seed_jobs(type_code_full: str, groups: list) -> int:
    """Insert one pending row per group. Idempotent (ON CONFLICT DO NOTHING)."""
    inserted = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for g in groups:
                cur.execute("""
                    INSERT INTO catalog_group_jobs (type_code_full, mg, group_name)
                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """, (type_code_full, g["mg"], g["name"]))
                inserted += cur.rowcount
        conn.commit()
    logger.info(f"{type_code_full}: seeded {inserted} group jobs ({len(groups)} groups)")
    return inserted


def with_car_lock(type_code_full: str, fn):
    """Run fn() while holding a session advisory lock for this car (serialises discovery)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (_car_lock_key(type_code_full),))
        conn.commit()
        try:
            return fn()
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_car_lock_key(type_code_full),))
            conn.commit()
            cur.close()
    finally:
        conn.close()


# ── Claiming / progress ─────────────────────────────────────────────────────

def claim_next(type_code_full: str, instance_id: str, stale_seconds: int = STALE_CLAIM_SECONDS):
    """
    Atomically take the next pending group (or one whose owner stopped
    heartbeating). Returns {"mg", "name"} or None when nothing is claimable.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE catalog_group_jobs j
                   SET status = 'claimed', claimed_by = %s, claimed_at = NOW()
                 WHERE (j.type_code_full, j.mg) = (
                       SELECT type_code_full, mg
                         FROM catalog_group_jobs
                        WHERE type_code_full = %s
                          AND (status = 'pending'
                               OR (status = 'claimed'
                                   AND claimed_at < NOW() - make_interval(secs => %s)))
                        ORDER BY mg
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED)
             RETURNING j.mg, j.group_name
            """, (instance_id, type_code_full, stale_seconds))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    return {"mg": row[0], "name": row[1]}


def heartbeat(type_code_full: str, mg: str, instance_id: str) -> bool:
    """Refresh the claim. Returns False if the claim was taken over (stop scraping it)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE catalog_group_jobs SET claimed_at = NOW()
                 WHERE type_code_full = %s AND mg = %s AND claimed_by = %s AND status = 'claimed'
            """, (type_code_full, mg, instance_id))
            ok = cur.rowcount == 1
        conn.commit()
    return ok


def mark_done(type_code_full: str, mg: str, instance_id: str, parts_count: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE catalog_group_jobs
                   SET status = 'done', done_at = NOW(), parts_count = %s
                 WHERE type_code_full = %s AND mg = %s
            """, (parts_count, type_code_full, mg))
        conn.commit()


def counts(type_code_full: str) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, COUNT(*), COALESCE(SUM(parts_count), 0)
                  FROM catalog_group_jobs WHERE type_code_full = %s GROUP BY status
            """, (type_code_full,))
            rows = cur.fetchall()
            cur.execute("""
                SELECT mg FROM catalog_group_jobs
                 WHERE type_code_full = %s AND status = 'claimed'
                 ORDER BY claimed_at DESC LIMIT 1
            """, (type_code_full,))
            cur_row = cur.fetchone()
            cur.execute("""
                SELECT mg FROM catalog_group_jobs
                 WHERE type_code_full = %s AND status = 'done' ORDER BY mg
            """, (type_code_full,))
            done_mgs = [r[0] for r in cur.fetchall()]
    by = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    total = sum(c for c, _ in by.values())
    return {
        "total": total,
        "done": by.get("done", (0, 0))[0],
        "claimed": by.get("claimed", (0, 0))[0],
        "pending": by.get("pending", (0, 0))[0],
        "parts": sum(p for _, p in by.values()),
        "done_mgs": done_mgs,
        "current_mg": cur_row[0] if cur_row else None,
    }


# ── Catalog merge (scraped_files) ───────────────────────────────────────────

def merge_group_into_catalog(car: dict, group: dict, group_node: dict):
    """
    Add one finished group to scraped_files[<4-char prefix>] under the shared
    advisory lock, so eight instances writing the same car never lose a group.
    Layout is identical to what NotesWriter produced, so PartPilot reads it as before.
    """
    from config import DO_ADVISORY_LOCK_KEY
    tc = car["type_code_full"]
    prefix = tc[:4]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (DO_ADVISORY_LOCK_KEY,))
        try:
            cur.execute("SELECT content FROM scraped_files WHERE filename = %s", (prefix,))
            row = cur.fetchone()
            data = json.loads(row[0]) if row and row[0] else {}
            node = data.get(tc)
            if not node:
                node = {
                    "series_value":   car.get("series_value", ""),
                    "series_label":   car.get("series_label", ""),
                    "body":           car.get("body", ""),
                    "model":          car.get("model", ""),
                    "market":         car.get("market", ""),
                    "prod_month":     car.get("prod_month", ""),
                    "engine":         car.get("engine", ""),
                    "steering":       car.get("steering", ""),
                    "type_code_full": tc,
                    "groups": {},
                }
                data[tc] = node
            # Merge subgroup by subgroup: a long group is saved in slices, and a
            # takeover resumes from what is already stored (2026-09-21: a
            # 130-subgroup group restarted from zero every time its pod died).
            groups = node.setdefault("groups", {})
            existing = groups.get(group["mg"]) or {"group_name": group_node.get("group_name", group.get("name", "")), "subgroups": {}}
            existing["group_name"] = group_node.get("group_name") or existing.get("group_name", "")
            existing.setdefault("subgroups", {}).update(group_node.get("subgroups", {}))
            groups[group["mg"]] = existing
            cur.execute("""
                INSERT INTO scraped_files (filename, content, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (filename) DO UPDATE
                    SET content = EXCLUDED.content, updated_at = NOW()
            """, (prefix, json.dumps(data, ensure_ascii=False)))
            conn.commit()
            return set(existing["subgroups"].keys())
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DO_ADVISORY_LOCK_KEY,))
            conn.commit()
            cur.close()
    finally:
        conn.close()


def stored_subgroups(car: dict, mg: str) -> set:
    """Subgroup ids already saved for this group (from earlier slices or a dead owner)."""
    tc = car["type_code_full"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM scraped_files WHERE filename = %s", (tc[:4],))
            row = cur.fetchone()
    if not row or not row[0]:
        return set()
    try:
        node = json.loads(row[0]).get(tc) or {}
        return set((node.get("groups", {}).get(mg) or {}).get("subgroups", {}).keys())
    except Exception:
        return set()


def has_claimable(type_code_full: str, stale_seconds: int = STALE_CLAIM_SECONDS) -> bool:
    """Cheap pre-check so idle instances don't launch a browser for nothing."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM catalog_group_jobs
                 WHERE type_code_full = %s
                   AND (status = 'pending'
                        OR (status = 'claimed' AND claimed_at < NOW() - make_interval(secs => %s)))
                 LIMIT 1
            """, (type_code_full, stale_seconds))
            return cur.fetchone() is not None


def drop_skipped(car: dict, skip_mgs) -> int:
    """Remove job rows (and any stored data) for groups we no longer scrape."""
    skip = sorted({m for m in (skip_mgs or ()) if m})
    if not skip:
        return 0
    tc = car["type_code_full"]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM catalog_group_jobs WHERE type_code_full = %s AND mg = ANY(%s)", (tc, skip))
            removed = cur.rowcount
        conn.commit()
    if removed:
        logger.info(f"{tc}: dropped {removed} job(s) for skipped group(s) {', '.join(skip)}")
        # Also strip the group from the catalog blob if a partial slice landed there.
        from config import DO_ADVISORY_LOCK_KEY
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_lock(%s)", (DO_ADVISORY_LOCK_KEY,))
            try:
                cur.execute("SELECT content FROM scraped_files WHERE filename = %s", (tc[:4],))
                row = cur.fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
                    node = data.get(tc) or {}
                    changed = False
                    for m in skip:
                        if m in node.get("groups", {}):
                            del node["groups"][m]
                            changed = True
                    if changed:
                        cur.execute("UPDATE scraped_files SET content = %s, updated_at = NOW() WHERE filename = %s",
                                    (json.dumps(data, ensure_ascii=False), tc[:4]))
                conn.commit()
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (DO_ADVISORY_LOCK_KEY,))
                conn.commit()
                cur.close()
        finally:
            conn.close()
    return removed


def update_summary(car: dict):
    """Recompute this car's line in the shared _summary blob from scraped_files."""
    from config import DO_ADVISORY_LOCK_KEY
    tc = car["type_code_full"]
    prefix = tc[:4]
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_lock(%s)", (DO_ADVISORY_LOCK_KEY,))
        try:
            cur.execute("SELECT content FROM scraped_files WHERE filename = %s", (prefix,))
            row = cur.fetchone()
            car_data = (json.loads(row[0]) if row and row[0] else {}).get(tc) or {}
            parts = groups_c = subgroups_c = 0
            for g in car_data.get("groups", {}).values():
                groups_c += 1
                for sg in g.get("subgroups", {}).values():
                    subgroups_c += 1
                    parts += len(sg.get("parts", []))
            cur.execute("SELECT content FROM scraped_files WHERE filename = '_summary'")
            row = cur.fetchone()
            summary = json.loads(row[0]) if row and row[0] else {}
            summary[tc] = {
                "series_label":    car_data.get("series_label") or car_data.get("series_value", ""),
                "model":           car_data.get("model", ""),
                "market":          car_data.get("market", ""),
                "body":            car_data.get("body", ""),
                "engine":          car_data.get("engine", ""),
                "prod_month":      car_data.get("prod_month", ""),
                "parts_count":     parts,
                "groups_count":    groups_c,
                "subgroups_count": subgroups_c,
            }
            cur.execute("""
                INSERT INTO scraped_files (filename, content, updated_at)
                VALUES ('_summary', %s, NOW())
                ON CONFLICT (filename) DO UPDATE
                    SET content = EXCLUDED.content, updated_at = NOW()
            """, (json.dumps(summary, ensure_ascii=False),))
            conn.commit()
            return parts
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (DO_ADVISORY_LOCK_KEY,))
            conn.commit()
            cur.close()
    finally:
        conn.close()


# ── Legacy checkpoint row (read by PartPilot's progress bar) ────────────────

def sync_checkpoint_entry(scraper_id: int, type_code_full: str, completed: bool = None):
    """
    Rewrite cars[type_code_full] in scraper_checkpoints from the jobs table.
    Read-modify-write under a row lock so concurrent instances don't drop each
    other's updates. Also serves as the worker heartbeat the backend relies on.
    """
    c = counts(type_code_full)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scraper_checkpoints (scraper_id, checkpoint_data)
                VALUES (%s, '{"cars": {}}'::jsonb) ON CONFLICT (scraper_id) DO NOTHING
            """, (scraper_id,))
            cur.execute("SELECT checkpoint_data FROM scraper_checkpoints WHERE scraper_id = %s FOR UPDATE",
                        (scraper_id,))
            row = cur.fetchone()
            data = row[0] if row and row[0] else {}
            if isinstance(data, str):
                data = json.loads(data)
            cars = data.setdefault("cars", {})
            entry = cars.get(type_code_full) or {}
            done_flag = completed if completed is not None else (
                c["total"] > 0 and c["done"] == c["total"])
            c["newly_completed"] = bool(done_flag) and not entry.get("completed")
            entry.update({
                "completed":        bool(done_flag),
                "completed_groups": c["done_mgs"],
                "completed_subgroups": {},
                "in_progress_group": None if done_flag else c["current_mg"],
                "total_groups":     c["total"],
            })
            cars[type_code_full] = entry
            data["last_updated"] = datetime.utcnow().isoformat()
            cur.execute("""
                UPDATE scraper_checkpoints
                   SET checkpoint_data = %s::jsonb, updated_at = NOW()
                 WHERE scraper_id = %s
            """, (json.dumps(data), scraper_id))
        conn.commit()
    return c


def touch_checkpoint(scraper_id: int):
    """Heartbeat only (idle instances): the backend treats a fresh updated_at as 'worker alive'."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scraper_checkpoints (scraper_id, checkpoint_data)
                VALUES (%s, '{"cars": {}}'::jsonb) ON CONFLICT (scraper_id) DO UPDATE SET updated_at = NOW()
            """, (scraper_id,))
        conn.commit()

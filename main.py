
import argparse
import gc
import logging
import os
import socket
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

from config import SCRAPER_ID
from scraper.browser import (
    launch_browser, start_virtual_display, stop_virtual_display, BrowserCrashError
)
from scraper.parts_scraper import get_main_groups, scrape_group
from storage import jobs
from storage.progress import ProgressWriter
import scaling

# Relaunch the browser every N groups to keep Chromium's memory flat on a 1 GB box.
BROWSER_RESTART_EVERY_GROUPS = int(os.environ.get("BROWSER_RESTART_EVERY_GROUPS", "6"))

# On-demand worker mode: when the queue is empty, sleep this long and re-check
# instead of exiting. 0 (default) keeps the one-shot behaviour for local runs.
IDLE_POLL_SECONDS = int(os.environ.get("IDLE_POLL_SECONDS", "0"))

# Several instances of this worker run the same code against the same queue.
# Each one claims groups from storage.jobs, so an id is all they need to share a car.
INSTANCE_ID = os.environ.get("HOSTNAME") or socket.gethostname() or uuid.uuid4().hex[:8]

# Sample car for --sample mode (navigates directly, no dropdown enumeration)
_SAMPLE_CAR = {
    "type_code_full": "VA99-EGY-05-2005-E90-BMW-320i",
    "series_value":   "E90",
    "series_label":   "3' E90",
    "body":           "Lim",
    "model":          "320i",
    "market":         "EGY",
    "prod_month":     "200805",
    "engine":         "N46",
    "steering":       "",
}


def setup_logging():
    log_format = f"%(asctime)s [%(levelname)s] [{INSTANCE_ID[-6:]}] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ── Car list helpers ──────────────────────────────────────────────────────────

def _car_from_info(car_info: dict, type_code_full: str) -> dict:
    model = (car_info.get("model") or "").strip()
    for brand in ("BMW ", "MINI "):
        if model.startswith(brand):
            model = model[len(brand):]
    return {
        "type_code_full": type_code_full,
        "series_value":   car_info.get("series", ""),
        "series_label":   car_info.get("series", ""),
        "body":           car_info.get("body", ""),
        "model":          model,
        "market":         car_info.get("market", "EUR"),
        "prod_month":     (car_info.get("prod_month") or "").replace("-", ""),
        "engine":         car_info.get("engine", ""),
        "steering":       "",
    }


def _load_done_set() -> tuple:
    """(completed type_code_fulls, code→type_code_full map) from the shared checkpoint row."""
    from storage.db import load_checkpoint
    data = load_checkpoint(SCRAPER_ID) or {}
    done = {tc for tc, e in (data.get("cars") or {}).items() if e.get("completed")}
    return done, dict(data.get("type_code_map") or {})


def _get_remaining_cars(sample_mode: bool) -> list:
    """
    Cars from scraper_car_lists that are not marked completed. Re-read from the
    DB on every call because other instances finish cars too.
    """
    logger = logging.getLogger("main")
    done, type_code_map = _load_done_set()

    if sample_mode:
        return [] if _SAMPLE_CAR["type_code_full"] in done else [_SAMPLE_CAR]

    from storage.db import get_car_list
    car_list = get_car_list(SCRAPER_ID)
    if not car_list:
        return []

    remaining, seen = [], set()
    for car_info in car_list:
        code = car_info.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        tc = car_info.get("type_code_full") or type_code_map.get(code)
        if tc and tc in done:
            continue
        remaining.append(car_info)
    logger.info(f"Scraper {SCRAPER_ID}: {len(remaining)} car(s) remaining ({len(done)} done)")
    return remaining


def _remember_type_code(code: str, type_code_full: str):
    """Cache a discovered type_code_full in the checkpoint row (legacy rows without one)."""
    import json
    from storage.db import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT checkpoint_data FROM scraper_checkpoints WHERE scraper_id = %s FOR UPDATE",
                        (SCRAPER_ID,))
            row = cur.fetchone()
            data = row[0] if row and row[0] else {"cars": {}}
            if isinstance(data, str):
                data = json.loads(data)
            data.setdefault("type_code_map", {})[code] = type_code_full
            cur.execute("UPDATE scraper_checkpoints SET checkpoint_data = %s::jsonb, updated_at = NOW() WHERE scraper_id = %s",
                        (json.dumps(data), SCRAPER_ID))
        conn.commit()


# ── One car, shared between instances ────────────────────────────────────────

def _ensure_jobs(page, car) -> bool:
    """Discover the car's main groups once (first instance in wins) and seed the job table."""
    logger = logging.getLogger("main")
    tc = car["type_code_full"]
    if jobs.has_jobs(tc):
        return "exists"

    def discover():
        if jobs.has_jobs(tc):
            return "exists"
        groups = get_main_groups(page, tc)
        if not groups:
            logger.warning(f"No groups found for {tc}")
            return None
        jobs.seed_jobs(tc, groups)
        jobs.sync_checkpoint_entry(SCRAPER_ID, tc)
        return "seeded"

    return jobs.with_car_lock(tc, discover)


def _scrape_car_shared(page, car, progress, still_wanted=None) -> tuple:
    """
    Claim and scrape groups of this car until none are left to claim.
    Returns (groups_scraped_here, car_finished_by_anyone).
    Raises BrowserCrashError to let the caller relaunch (claims are reclaimed
    by other instances after the heartbeat goes stale, or by us after relaunch).
    """
    logger = logging.getLogger("main")
    tc = car["type_code_full"]
    groups_here = 0

    while True:
        if still_wanted is not None and not still_wanted():
            logger.info(f"{tc} was removed from the queue — stopping")
            return groups_here, False
        job = jobs.claim_next(tc, INSTANCE_ID)
        if job is None:
            break
        logger.info(f"=== {tc}: group {job['mg']} — {job['name']} ===")
        jobs.sync_checkpoint_entry(SCRAPER_ID, tc)

        def on_subgroup(_diag_id, _mg=job["mg"]):
            return jobs.heartbeat(tc, _mg, INSTANCE_ID)

        node, parts_count = scrape_group(page, car, job, on_subgroup=on_subgroup)
        if node is None:
            continue  # reassigned while we were slow; whoever owns it now will merge it
        jobs.merge_group_into_catalog(car, job, node)
        jobs.mark_done(tc, job["mg"], INSTANCE_ID, parts_count)
        c = jobs.sync_checkpoint_entry(SCRAPER_ID, tc)
        groups_here += 1
        logger.info(f"{tc}: group {job['mg']} saved ({parts_count} parts) — {c['done']}/{c['total']} groups done")
        if groups_here % BROWSER_RESTART_EVERY_GROUPS == 0:
            raise BrowserCrashError("scheduled browser restart")  # handled like a crash: relaunch + continue

    c = jobs.counts(tc)
    finished = c["total"] > 0 and c["done"] == c["total"]
    if finished:
        # Several instances reach this point; the work is idempotent.
        parts = jobs.update_summary(car)
        if jobs.sync_checkpoint_entry(SCRAPER_ID, tc, completed=True).get("newly_completed"):
            progress.mark_completed(tc, parts)
        logger.info(f"Completed: {tc} — {parts} parts, {c['total']} groups")
    elif c["claimed"]:
        logger.info(f"{tc}: {c['claimed']} group(s) still being scraped by other instance(s)")
    return groups_here, finished


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RealOEM BMW Parts Scraper")
    parser.add_argument("--sample", action="store_true",
                        help="Sample mode: scrape only E90 320i EGY for testing.")
    args = parser.parse_args()
    sample_mode = args.sample

    setup_logging()
    logger = logging.getLogger("main")
    mode_str = "SAMPLE (E90 320i EGY)" if sample_mode else f"FULL (scraper {SCRAPER_ID})"
    logger.info(f"=== RealOEM BMW Scraper starting — SCRAPER_ID={SCRAPER_ID}, instance={INSTANCE_ID}, mode: {mode_str} ===")

    from storage import db as _db
    for attempt in range(1, 6):
        try:
            _db.ensure_table()
            jobs.ensure_table()
            break
        except Exception as e:
            # e.g. a sibling instance created the table a millisecond earlier
            logger.warning(f"DB init attempt {attempt} failed: {e}")
            if attempt == 5:
                raise
            time.sleep(2 * attempt)

    progress = ProgressWriter()
    start_virtual_display()
    session = 0

    try:
        while True:
            session += 1
            scaling.watchdog()
            remaining = _get_remaining_cars(sample_mode)
            if not remaining:
                jobs.touch_checkpoint(SCRAPER_ID)
                scaling.on_idle()
                if IDLE_POLL_SECONDS > 0:
                    logger.info(f"Queue empty — re-checking in {IDLE_POLL_SECONDS}s")
                    time.sleep(IDLE_POLL_SECONDS)
                    continue
                logger.info("All assigned cars scraped! Scraper done.")
                break

            # Bring the rest of the fleet up before spending time in a browser.
            scaling.on_work_found()

            need_restart = False
            interrupted = False
            worked = False
            with sync_playwright() as p:
                browser = None
                try:
                    browser, context, page = launch_browser(p)
                    for car_info in remaining:
                        if sample_mode:
                            car = car_info
                        else:
                            code = car_info["code"]
                            tc = car_info.get("type_code_full") or _load_done_set()[1].get(code)
                            if not tc:
                                logger.info(f"Navigating RealOEM to find type_code for {code}")
                                from scraper.car_selector import find_car_type_code
                                found = find_car_type_code(page, car_info)
                                if found is None:
                                    logger.warning(f"Could not find type_code for {code}, skipping")
                                    continue
                                tc = found["type_code_full"]
                                _remember_type_code(code, tc)
                                logger.info(f"Found type_code: {tc}")
                            car = _car_from_info(car_info, tc)

                        state = _ensure_jobs(page, car)
                        if not state:
                            continue
                        if state == "seeded":
                            progress.mark_started(car["type_code_full"])

                        def still_wanted(_code=car_info.get("code")):
                            if sample_mode:
                                return True
                            from storage.db import get_car_list
                            return any(c.get("code") == _code for c in get_car_list(SCRAPER_ID))

                        n, _ = _scrape_car_shared(page, car, progress, still_wanted=still_wanted)
                        worked = worked or n > 0

                except BrowserCrashError as e:
                    logger.warning(f"Browser restart: {e}")
                    need_restart = True
                except KeyboardInterrupt:
                    interrupted = True
                    logger.info("Interrupted by user. Progress saved.")
                except Exception as e:
                    logger.error(f"Unexpected session error: {e} — restarting browser", exc_info=True)
                    need_restart = True
                finally:
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass

            gc.collect()
            if interrupted:
                break
            if need_restart:
                time.sleep(2)
            elif not worked:
                # Every group of every queued car is claimed by other instances:
                # wait for them instead of spinning on the queue.
                logger.info("Nothing left to claim — waiting for other instances (30s)")
                time.sleep(30)
            # Loop: re-read the queue; finished cars drop out, unfinished ones get more claims.
    finally:
        stop_virtual_display()
        logger.info("=== Scraper done ===")


if __name__ == "__main__":
    main()

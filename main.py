
import argparse
import gc
import json
import logging
import sys

from playwright.sync_api import sync_playwright

from config import (
    VFINAL_NOTES_FILE, CHECKPOINT_FILE, LOG_FILE, PROGRESS_FILE,
    SCRAPER_ID,
)
from scraper.browser import (
    launch_browser, start_virtual_display, stop_virtual_display, BrowserCrashError
)
from scraper.parts_scraper import scrape_car_parts
from storage.notes import NotesWriter
from storage.checkpoint import CheckpointManager
from storage.progress import ProgressWriter

# Restart browser every N cars to reset memory
BROWSER_RESTART_EVERY = 4

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
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ── Checkpoint helpers (DB-backed) ────────────────────────────────────────────

def _load_checkpoint_data() -> dict:
    """Load checkpoint from DO DB for this SCRAPER_ID; fall back to local file."""
    from storage import db
    data = db.load_checkpoint(SCRAPER_ID)
    if data:
        logger_ = logging.getLogger("main")
        logger_.info(f"Checkpoint loaded from DB (scraper {SCRAPER_ID})")
        return data
    # Fallback: local file
    try:
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.getLogger("main").warning(f"Could not read local checkpoint: {e}")
        return {}


def _save_checkpoint_data(data: dict):
    """Persist checkpoint to DO DB for this SCRAPER_ID."""
    from storage import db
    db.save_checkpoint(SCRAPER_ID, data)


# ── Car list helpers ──────────────────────────────────────────────────────────

def _get_target_codes() -> set:
    """
    Return the set of 4-char target codes for this SCRAPER_ID from DB.
    Returns None if the car list could not be loaded (meaning: scrape all).
    """
    logger_ = logging.getLogger("main")
    from storage import db
    car_list = db.get_car_list(SCRAPER_ID)
    if not car_list:
        logger_.warning(
            f"No car list found in DB for SCRAPER_ID={SCRAPER_ID}. "
            f"Will scrape all discovered cars."
        )
        return None
    codes = {c["code"] for c in car_list}
    logger_.info(
        f"Scraper {SCRAPER_ID}: targeting {len(codes)} cars — "
        f"{sorted(codes)[:5]}{'…' if len(codes) > 5 else ''}"
    )
    return codes


# ── Main car discovery ────────────────────────────────────────────────────────

def _get_remaining_cars(sample_mode: bool, scraped_prefixes: set,
                        notes: NotesWriter, checkpoint: CheckpointManager,
                        target_codes) -> list:
    """
    Return the list of cars to scrape this session.

    - sample_mode: returns the single sample car if not already done.
    - full mode:
        1. Resume any in-progress car from checkpoint (metadata pulled from notes).
        2. Load car_list.json from DB (legacy discovery cache), filter by
           done prefixes AND target_codes for this SCRAPER_ID.
    Returns [] when everything is done.
    """
    logger = logging.getLogger("main")

    if sample_mode:
        tc = _SAMPLE_CAR["type_code_full"]
        if checkpoint.is_car_done(tc):
            logger.info("Sample car already done.")
            return []
        return [_SAMPLE_CAR]

    # --- Resume in-progress cars (checkpoint started but not completed) ---
    resume_cars = []
    for tc, entry in checkpoint.data.get("cars", {}).items():
        if entry.get("completed", False):
            continue
        prefix = tc[:4]
        if prefix in scraped_prefixes:
            continue
        # Only resume cars belonging to this scraper's target list
        if target_codes is not None and prefix not in target_codes:
            continue
        car_dict = notes.get_car_dict(tc)
        if car_dict:
            resume_cars.append(car_dict)
            scraped_prefixes.add(prefix)
            logger.info(f"Resume in-progress: {tc}")
        else:
            logger.warning(
                f"Checkpoint has {tc} as in-progress but no notes data — "
                f"will pick it up from car_list.json."
            )

    if resume_cars:
        return resume_cars

    # --- Load full car list from DB (legacy discovery cache), filter done & target ---
    try:
        from storage.db import get_file_content
        content = get_file_content("car_list.json")
        if not content:
            logger.warning(
                "car_list.json not found in DB. "
                "Scraper will rely on live discovery for new cars."
            )
            return []
        all_cars = json.loads(content)
    except Exception as e:
        logger.error(f"Failed to load car_list.json from DB: {e}")
        return []

    # Flatten: {"typecode#N [XXXX]": {"1. BMW...": car_dict, ...}}
    flat_cars = []
    if isinstance(all_cars, dict):
        for group_variants in all_cars.values():
            if isinstance(group_variants, dict):
                variants = list(group_variants.values())
                if variants:
                    flat_cars.append(variants[0])
    else:
        flat_cars = list(all_cars)

    remaining = []
    seen = set(scraped_prefixes)
    for car in flat_cars:
        prefix = car["type_code_full"][:4]
        if prefix in seen:
            continue
        # Filter by this scraper's target list
        if target_codes is not None and prefix not in target_codes:
            continue
        seen.add(prefix)
        remaining.append(car)

    logger.info(
        f"Scraper {SCRAPER_ID}: {len(remaining)} cars remaining "
        f"(target={len(target_codes) if target_codes else 'all'}, "
        f"done={len(scraped_prefixes)})"
    )
    return remaining


# ── Notes save wrapper (uses advisory lock) ───────────────────────────────────

def _save_notes_to_db(notes: NotesWriter):
    """Persist the current notes data to DO DB under this scraper's prefix key."""
    from storage.db import save_with_lock
    try:
        content = json.dumps(notes.data, ensure_ascii=False)
        # Store as "vFinal_notes.json" for backward compat with frontend
        save_with_lock("vFinal_notes.json", content)
    except Exception as e:
        logging.getLogger("main").warning(f"_save_notes_to_db failed: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RealOEM BMW Parts Scraper")
    parser.add_argument(
        "--sample", action="store_true",
        help="Sample mode: scrape only E90 320i EGY for testing.",
    )
    args = parser.parse_args()
    sample_mode = args.sample

    setup_logging()
    logger = logging.getLogger("main")

    mode_str = "SAMPLE (E90 320i EGY)" if sample_mode else f"FULL (scraper {SCRAPER_ID})"
    logger.info(f"=== RealOEM BMW Scraper starting — SCRAPER_ID={SCRAPER_ID}, mode: {mode_str} ===")

    # --- Init DB ---
    from storage import db as _db
    try:
        _db.ensure_table()
    except Exception as e:
        logger.warning(f"DB init skipped: {e}")

    # --- Load target codes for this scraper ---
    target_codes = None if sample_mode else _get_target_codes()

    # --- Storage ---
    notes      = NotesWriter(VFINAL_NOTES_FILE)
    progress   = ProgressWriter(PROGRESS_FILE)

    # --- Load checkpoint from DB ---
    cp_data    = _load_checkpoint_data()
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    if cp_data:
        checkpoint.data = cp_data  # inject DB data into in-memory checkpoint

    start_virtual_display()

    session = 0

    try:
        while True:
            session += 1
            scraped_prefixes = checkpoint.get_done_prefixes()
            logger.info(
                f"Browser session {session} — "
                f"{len(scraped_prefixes)} prefixes done so far"
            )

            need_restart = False
            interrupted  = False

            with sync_playwright() as p:
                browser = None
                cars_this_session = 0

                try:
                    browser, context, page = launch_browser(p)

                    cars = _get_remaining_cars(
                        sample_mode, scraped_prefixes, notes, checkpoint, target_codes
                    )

                    if not cars:
                        logger.info("All assigned cars scraped! Scraper done.")
                        break

                    for car in cars:
                        type_code = car["type_code_full"]

                        if checkpoint.is_car_done(type_code):
                            continue

                        logger.info(f"=== Scraping car: {type_code} ===")
                        progress.mark_started(type_code)

                        try:
                            parts_count = scrape_car_parts(
                                page, car, notes, checkpoint
                            )
                            progress.mark_completed(type_code, parts_count)
                            logger.info(f"Finished {type_code}: {parts_count} parts")

                            # Persist checkpoint to DB after each completed car
                            _save_checkpoint_data(checkpoint.data)

                            # Save notes to DB with advisory lock
                            _save_notes_to_db(notes)

                        except BrowserCrashError as e:
                            logger.error(
                                f"Browser crashed scraping {type_code}: {e} "
                                f"— restarting browser"
                            )
                            need_restart = True
                            break
                        except Exception as e:
                            logger.error(
                                f"Failed to scrape {type_code}: {e}", exc_info=True
                            )

                        cars_this_session += 1
                        if cars_this_session >= BROWSER_RESTART_EVERY:
                            need_restart = True
                            logger.info(
                                f"Scraped {BROWSER_RESTART_EVERY} cars — "
                                f"restarting browser to clear memory"
                            )
                            break

                except KeyboardInterrupt:
                    interrupted = True
                    logger.info("Interrupted by user. Progress saved.")

                except Exception as e:
                    logger.error(
                        f"Unexpected session error: {e} — restarting browser",
                        exc_info=True,
                    )
                    need_restart = True

                finally:
                    if browser is not None:
                        try:
                            browser.close()
                            logger.info("Browser closed.")
                        except Exception:
                            pass

            gc.collect()
            logger.info("Memory cleared.")

            if interrupted:
                break

            if not need_restart:
                need_restart = True
                logger.info(
                    "Batch complete — restarting to verify no cars remain."
                )

    finally:
        stop_virtual_display()
        logger.info("=== Scraper done ===")


if __name__ == "__main__":
    main()

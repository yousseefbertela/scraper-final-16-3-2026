
import argparse
import gc
import json
import logging
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import (
    VFINAL_NOTES_FILE, CHECKPOINT_FILE, LOG_FILE,
    PROGRESS_FILE, DATA_DIR, EGY_CARS_CACHE_FILE,
)
from scraper.browser import launch_browser, start_virtual_display, stop_virtual_display
from scraper.car_selector import build_car_list
from scraper.parts_scraper import scrape_car_parts
from storage.notes import NotesWriter
from storage.checkpoint import CheckpointManager
from storage.progress import ProgressWriter

# Restart browser every N cars to reset memory
BROWSER_RESTART_EVERY = 10


def setup_logging():
    Path(DATA_DIR).mkdir(exist_ok=True)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def restore_files_from_db():
    """On startup, ALWAYS pull latest files from PostgreSQL.
    Includes the EGY cars discovery cache so we never re-navigate
    all 243 series after a restart.
    """
    try:
        from storage.db import restore_file_to_path
        files = (
            VFINAL_NOTES_FILE,
            CHECKPOINT_FILE,
            PROGRESS_FILE,
            EGY_CARS_CACHE_FILE,
        )
        for filepath in files:
            filename = Path(filepath).name
            ok = restore_file_to_path(filename, filepath)
            if ok:
                logging.getLogger("main").info(
                    f"Restored {filename} from DB"
                )
            else:
                logging.getLogger("main").info(
                    f"No DB backup for {filename} — starting fresh"
                )
    except Exception as e:
        logging.getLogger("main").warning(f"DB restore skipped ({e})")


def load_car_cache():
    """Load the cached list of all EGY cars discovered previously.
    Returns list of car dicts, or None if no cache exists yet.
    """
    if Path(EGY_CARS_CACHE_FILE).exists():
        try:
            with open(EGY_CARS_CACHE_FILE, "r", encoding="utf-8") as f:
                cars = json.load(f)
            logging.getLogger("main").info(
                f"Car cache loaded: {len(cars)} EGY cars"
            )
            return cars
        except Exception as e:
            logging.getLogger("main").warning(f"Could not load car cache: {e}")
    return None


def save_car_cache(cars: list):
    """Save discovered EGY car list to file and sync to PostgreSQL."""
    try:
        Path(DATA_DIR).mkdir(exist_ok=True)
        tmp = EGY_CARS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cars, f, indent=2, ensure_ascii=False)
        os.replace(tmp, EGY_CARS_CACHE_FILE)
        logging.getLogger("main").info(
            f"Car cache saved: {len(cars)} EGY cars"
        )
        try:
            from storage.db import sync_file_from_path
            sync_file_from_path(EGY_CARS_CACHE_FILE)
        except Exception:
            pass
    except Exception as e:
        logging.getLogger("main").error(f"Could not save car cache: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="RealOEM BMW Parts Scraper — EGY full run"
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Sample mode: scrape only E90 320i EGY for testing.",
    )
    args = parser.parse_args()
    sample_mode = args.sample

    Path(DATA_DIR).mkdir(exist_ok=True)
    setup_logging()
    logger = logging.getLogger("main")

    mode_str = "SAMPLE (E90 320i EGY)" if sample_mode else "FULL (all EGY series)"
    logger.info(f"=== RealOEM BMW Scraper starting — mode: {mode_str} ===")

    # --- Initialise DB ---
    try:
        from storage.db import ensure_table
        ensure_table()
    except Exception as e:
        logger.warning(f"DB init skipped: {e}")

    # --- Restore ALL files from DB (including car cache) ---
    restore_files_from_db()

    # --- Storage layer ---
    notes      = NotesWriter(VFINAL_NOTES_FILE)
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    progress   = ProgressWriter(PROGRESS_FILE)

    # --- Load car list from cache (avoids re-navigating 243 series on restart) ---
    car_list = load_car_cache()

    start_virtual_display()

    session = 0

    try:
        # ── Phase 1: Discovery (only runs ONCE — result cached in DB) ──────────
        if car_list is None:
            logger.info(
                "No car cache found — running full discovery "
                "(this runs once; result will be cached in DB)"
            )
            with sync_playwright() as p:
                browser, context, page = launch_browser(p)
                try:
                    # Discover ALL EGY cars (empty prefixes = full list)
                    car_list = list(
                        build_car_list(
                            page,
                            sample_mode=sample_mode,
                            scraped_prefixes=set(),
                        )
                    )
                    save_car_cache(car_list)
                    logger.info(
                        f"Discovery complete — {len(car_list)} EGY cars found"
                    )
                except Exception as e:
                    logger.error(
                        f"Discovery failed: {e} — will retry on next start",
                        exc_info=True,
                    )
                    car_list = []
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
            gc.collect()

        if not car_list:
            logger.error("No EGY cars in cache — cannot scrape. Exiting.")
            return

        logger.info(f"Total EGY cars to scrape: {len(car_list)}")

        # ── Phase 2: Scraping — starts from checkpoint, no re-discovery ────────
        while True:
            session += 1

            # Recompute done prefixes at every browser session start
            scraped_prefixes = (
                checkpoint.get_done_prefixes() | progress.get_scraped_prefixes()
            )

            # Filter to cars not yet scraped — preserves original order
            remaining = [
                car for car in car_list
                if car["type_code_full"][:4] not in scraped_prefixes
                and not checkpoint.is_car_done(car["type_code_full"])
            ]

            if not remaining:
                logger.info("All EGY cars scraped! Scraper done.")
                break

            logger.info(
                f"Browser session {session} — "
                f"{len(remaining)} cars remaining, "
                f"{len(scraped_prefixes)} prefixes done"
            )

            need_restart = False
            interrupted  = False

            with sync_playwright() as p:
                browser, context, page = launch_browser(p)
                cars_this_session = 0

                try:
                    for car in remaining:
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
                            logger.info(
                                f"Finished {type_code}: {parts_count} parts"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to scrape {type_code}: {e}",
                                exc_info=True,
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
                    # Unexpected error (navigation crash, Cloudflare, etc.)
                    # Log it and restart browser session to continue
                    logger.error(
                        f"Unexpected session error: {e} — restarting browser",
                        exc_info=True,
                    )
                    need_restart = True

                finally:
                    try:
                        browser.close()
                        logger.info("Browser closed.")
                    except Exception:
                        pass

            gc.collect()
            logger.info("Memory cleared.")

            if interrupted:
                break

            # need_restart=True  → loop again (browser restart after 10 cars or error)
            # need_restart=False → all remaining cars done in this session
            if not need_restart:
                break

    finally:
        stop_virtual_display()
        logger.info("=== Scraper done ===")


if __name__ == "__main__":
    main()

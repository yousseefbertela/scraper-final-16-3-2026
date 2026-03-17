
import argparse
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import VFINAL_NOTES_FILE, CHECKPOINT_FILE, LOG_FILE, PROGRESS_FILE, DATA_DIR
from scraper.browser import launch_browser, start_virtual_display, stop_virtual_display
from scraper.car_selector import build_car_list
from scraper.parts_scraper import scrape_car_parts
from storage.notes import NotesWriter
from storage.checkpoint import CheckpointManager
from storage.progress import ProgressWriter


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
    """On startup, pull files from PostgreSQL if local copies are missing."""
    try:
        from storage.db import restore_file_to_path
        for filepath in (VFINAL_NOTES_FILE, CHECKPOINT_FILE, PROGRESS_FILE):
            if not Path(filepath).exists():
                filename = Path(filepath).name
                ok = restore_file_to_path(filename, filepath)
                if ok:
                    logging.getLogger("main").info(
                        f"Restored {filepath} from DB"
                    )
    except Exception as e:
        logging.getLogger("main").warning(
            f"DB restore skipped ({e})"
        )


def main():
    parser = argparse.ArgumentParser(description="RealOEM BMW Parts Scraper — EGY full run")
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

    # --- Initialise DB (create table if needed) ---
    try:
        from storage.db import ensure_table
        ensure_table()
    except Exception as e:
        logger.warning(f"DB init skipped: {e}")

    # --- Restore files from DB if local copies missing ---
    restore_files_from_db()

    # --- Storage layer ---
    notes      = NotesWriter(VFINAL_NOTES_FILE)
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    progress   = ProgressWriter(PROGRESS_FILE)

    # --- Compute already-scraped prefixes (4-char dedup) ---
    scraped_prefixes = checkpoint.get_done_prefixes() | progress.get_scraped_prefixes()
    logger.info(f"Known scraped prefixes: {len(scraped_prefixes)}")

    # --- Start virtual display for headed browser on Linux ---
    start_virtual_display()

    with sync_playwright() as p:
        browser, context, page = launch_browser(p)

        try:
            car_gen = build_car_list(
                page,
                sample_mode=sample_mode,
                scraped_prefixes=scraped_prefixes,
            )

            for car in car_gen:
                type_code = car["type_code_full"]

                if checkpoint.is_car_done(type_code):
                    logger.info(f"Skipping (checkpoint done): {type_code}")
                    continue

                logger.info(f"=== Scraping car: {type_code} ===")
                progress.mark_started(type_code)

                try:
                    parts_count = scrape_car_parts(page, car, notes, checkpoint)
                    progress.mark_completed(type_code, parts_count)
                    logger.info(
                        f"Finished {type_code}: {parts_count} parts"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to scrape {type_code}: {e}", exc_info=True
                    )

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Progress saved.")
        finally:
            browser.close()
            logger.info("Browser closed.")

    stop_virtual_display()
    logger.info("=== Scraper done ===")


if __name__ == "__main__":
    main()


import argparse
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from config import NOTES_FILE, CHECKPOINT_FILE, LOG_FILE
from scraper.browser import launch_browser
from scraper.car_selector import build_car_list
from scraper.parts_scraper import scrape_car_parts
from storage.notes import NotesWriter
from storage.checkpoint import CheckpointManager


def setup_logging():
    Path("data").mkdir(exist_ok=True)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="RealOEM BMW Parts Scraper")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape all BMW series. Default: sample mode (E90 320i EUR only).",
    )
    args = parser.parse_args()
    sample_mode = not args.all

    Path("data").mkdir(exist_ok=True)
    setup_logging()

    logger = logging.getLogger("main")
    mode_str = "SAMPLE (E90 320i)" if sample_mode else "FULL (all series)"
    logger.info(f"Starting scraper - mode: {mode_str}")

    notes      = NotesWriter(NOTES_FILE)
    checkpoint = CheckpointManager(CHECKPOINT_FILE)

    with sync_playwright() as p:
        browser, context, page = launch_browser(p)

        try:
            car_list = build_car_list(page, sample_mode=sample_mode)

            for car in car_list:
                type_code = car["type_code_full"]

                if checkpoint.is_car_done(type_code):
                    logger.info(f"Skipping (already done): {type_code}")
                    continue

                logger.info(f"=== Scraping car: {type_code} ===")
                try:
                    scrape_car_parts(page, car, notes, checkpoint)
                except Exception as e:
                    logger.error(f"Failed to scrape {type_code}: {e}", exc_info=True)

        except KeyboardInterrupt:
            logger.info("Interrupted by user. Progress saved to checkpoint.")
        finally:
            browser.close()
            logger.info("Browser closed. Scraper done.")


if __name__ == "__main__":
    main()

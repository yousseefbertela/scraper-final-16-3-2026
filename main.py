
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
        logging.getLogger("main").info(f"Checkpoint loaded from DB (scraper {SCRAPER_ID})")
        return data
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

def _get_remaining_cars(sample_mode: bool, scraped_prefixes: set,
                        checkpoint: CheckpointManager) -> list:
    """
    Return the list of cars to scrape this session.

    Each item from scraper_car_lists has: {code, series, model, body, engine, market, prod_month}.
    Already-scraped prefixes (from checkpoint) are filtered out.
    type_code_full is NOT yet set here; it will be discovered per-car in the main loop.
    """
    logger = logging.getLogger("main")

    if sample_mode:
        tc = _SAMPLE_CAR["type_code_full"]
        if checkpoint.is_car_done(tc):
            logger.info("Sample car already done.")
            return []
        return [_SAMPLE_CAR]

    from storage.db import get_car_list
    car_list = get_car_list(SCRAPER_ID)
    if not car_list:
        logger.warning(
            f"No car list found in DB for SCRAPER_ID={SCRAPER_ID}. "
            f"Nothing to scrape."
        )
        return []

    remaining = []
    seen = set(scraped_prefixes)
    for car_info in car_list:
        code = car_info["code"]
        if code in seen:
            continue
        # Also skip if we have a cached type_code_full that is marked done
        type_code_map = checkpoint.data.get("type_code_map", {})
        cached_tc = type_code_map.get(code)
        if cached_tc and checkpoint.is_car_done(cached_tc):
            seen.add(code)
            continue
        seen.add(code)
        remaining.append(car_info)

    logger.info(
        f"Scraper {SCRAPER_ID}: {len(remaining)} cars remaining "
        f"({len(scraped_prefixes)} already done)"
    )
    return remaining


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

    # --- Storage ---
    notes      = NotesWriter(VFINAL_NOTES_FILE)
    progress   = ProgressWriter(PROGRESS_FILE)

    # --- Load checkpoint from DB ---
    cp_data    = _load_checkpoint_data()
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    if cp_data:
        checkpoint.data = cp_data

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
                        sample_mode, scraped_prefixes, checkpoint
                    )

                    if not cars:
                        logger.info("All assigned cars scraped! Scraper done.")
                        break

                    for car_info in cars:
                        # ── Resolve type_code_full ────────────────────────
                        if sample_mode:
                            # Sample car already has type_code_full
                            car = car_info
                            type_code_full = car["type_code_full"]
                        else:
                            code = car_info["code"]
                            type_code_map = checkpoint.data.setdefault("type_code_map", {})
                            type_code_full = type_code_map.get(code)

                            if not type_code_full:
                                logger.info(f"Navigating RealOEM to find type_code for {code}")
                                from scraper.car_selector import find_car_type_code
                                car = find_car_type_code(page, car_info)
                                if car is None:
                                    logger.warning(
                                        f"Could not find type_code for {code}, skipping"
                                    )
                                    continue
                                type_code_full = car["type_code_full"]
                                # Cache for future sessions
                                type_code_map[code] = type_code_full
                                _save_checkpoint_data(checkpoint.data)
                                logger.info(f"Found type_code: {type_code_full}")
                            else:
                                # Reconstruct car dict from cached type_code + car_info
                                model = car_info.get("model", "").strip()
                                for brand in ("BMW ", "MINI "):
                                    if model.startswith(brand):
                                        model = model[len(brand):]
                                car = {
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

                        if checkpoint.is_car_done(type_code_full):
                            continue

                        logger.info(f"=== Scraping car: {type_code_full} ===")
                        progress.mark_started(type_code_full)

                        try:
                            parts_count = scrape_car_parts(
                                page, car, notes, checkpoint
                            )
                            progress.mark_completed(type_code_full, parts_count)
                            logger.info(f"Finished {type_code_full}: {parts_count} parts")

                            # Persist checkpoint to DB after each completed car
                            _save_checkpoint_data(checkpoint.data)

                        except BrowserCrashError as e:
                            logger.error(
                                f"Browser crashed scraping {type_code_full}: {e} "
                                f"— restarting browser"
                            )
                            need_restart = True
                            break
                        except Exception as e:
                            logger.error(
                                f"Failed to scrape {type_code_full}: {e}", exc_info=True
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

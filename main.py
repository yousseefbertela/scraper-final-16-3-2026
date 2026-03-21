
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
    PROGRESS_FILE, DATA_DIR, CAR_LIST_CACHE_FILE,
)
from scraper.browser import (
    launch_browser, start_virtual_display, stop_virtual_display, BrowserCrashError
)
from scraper.car_selector import build_car_list
from scraper.parts_scraper import scrape_car_parts
from storage.notes import NotesWriter
from storage.checkpoint import CheckpointManager
from storage.progress import ProgressWriter

# Restart browser every N cars to reset memory
BROWSER_RESTART_EVERY = 4


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
    """On startup, ALWAYS pull latest files from PostgreSQL."""
    try:
        from storage.db import restore_file_to_path
        files = (
            VFINAL_NOTES_FILE,
            CHECKPOINT_FILE,
            PROGRESS_FILE,
            CAR_LIST_CACHE_FILE,
        )
        for filepath in files:
            filename = Path(filepath).name
            ok = restore_file_to_path(filename, filepath)
            if ok:
                logging.getLogger("main").info(f"Restored {filename} from DB")
            else:
                logging.getLogger("main").info(
                    f"No DB backup for {filename} - starting fresh"
                )
    except Exception as e:
        logging.getLogger("main").warning(f"DB restore skipped ({e})")


def _get_cars_for_session(page, sample_mode: bool, scraped_prefixes: set,
                           notes, checkpoint):
    """
    Return (cars, needs_followup) for this browser session.

    needs_followup=True means Phase 1 was used — after processing these cars
    the outer loop MUST restart to discover more cars via Phase 2.

    Phase 1 - Resume incomplete cars instantly (no dropdown navigation):
        Find cars in checkpoint that are started but not completed.
        Pull their metadata straight from notes.json.
        Always sets needs_followup=True so Phase 2 runs next session.

    Phase 2 - Discover remaining cars:
        Load from car_list.json cache if available, otherwise enumerate
        RealOEM dropdowns and save the cache for future sessions.
        needs_followup=False (list is exhaustive; loop exits when empty).
    """
    logger = logging.getLogger("main")

    # ── Phase 1: resume in-progress cars from notes.json metadata ──
    resume_cars = []
    if not sample_mode:
        for tc, entry in checkpoint.data.get("cars", {}).items():
            if entry.get("completed", False):
                continue
            prefix = tc[:4]
            if prefix in scraped_prefixes:
                continue
            car_dict = notes.get_car_dict(tc)
            if car_dict:
                resume_cars.append(car_dict)
                scraped_prefixes.add(prefix)
                logger.info(f"Phase 1 resume: {tc} (metadata from notes.json)")
            else:
                logger.warning(
                    f"Checkpoint has {tc} as in-progress but no notes data found. "
                    f"Will pick it up in Phase 2 enumeration."
                )

    if resume_cars:
        logger.info(
            f"Returning {len(resume_cars)} in-progress car(s) for immediate resume. "
            f"RealOEM dropdown enumeration skipped. "
            f"Will enumerate remaining cars in next session."
        )
        # needs_followup=True so the outer loop restarts and runs Phase 2 next
        return resume_cars, True

    # ── Phase 2: discover next cars from cache or RealOEM ──
    cache_path = Path(CAR_LIST_CACHE_FILE)

    if not sample_mode and cache_path.exists():
        try:
            with open(str(cache_path), encoding="utf-8") as f:
                all_cars = json.load(f)
            remaining = []
            seen = set(scraped_prefixes)
            for car in all_cars:
                prefix = car["type_code_full"][:4]
                if prefix in seen:
                    continue
                seen.add(prefix)
                remaining.append(car)
            scraped_prefixes.update(seen)
            logger.info(
                f"Phase 2 cache: {len(remaining)} cars remaining "
                f"of {len(all_cars)} total"
            )
            if not remaining:
                # All cars in the cache are already scraped.
                # Delete the cache so the next session runs a fresh RealOEM enumeration
                # which may discover new EGY cars.
                logger.info(
                    "All cached cars scraped -- clearing car list cache to "
                    "force fresh EGY discovery in next session."
                )
                try:
                    cache_path.unlink(missing_ok=True)
                except Exception:
                    pass
                # Return needs_followup=True so outer loop restarts and Phase 2
                # runs again without a cache, triggering a full RealOEM enumeration.
                return [], True
            return remaining, False
        except Exception as e:
            logger.warning(f"Could not load car list cache ({e}), re-enumerating...")

    # No cache - enumerate from RealOEM and save for future sessions
    logger.info(
        "Phase 2: building full car list from RealOEM dropdowns "
        "(first time - will be cached for future restarts)..."
    )
    all_cars = list(build_car_list(
        page, sample_mode=sample_mode, scraped_prefixes=set()
    ))

    if not sample_mode:
        try:
            Path(DATA_DIR).mkdir(exist_ok=True)
            with open(str(cache_path), "w", encoding="utf-8") as f:
                json.dump(all_cars, f, indent=2, ensure_ascii=False)
            logger.info(
                f"Car list cached: {len(all_cars)} cars -> {CAR_LIST_CACHE_FILE}"
            )
            try:
                from storage.db import sync_file_from_path
                sync_file_from_path(str(cache_path))
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Could not save car list cache: {e}")

    remaining = []
    seen = set(scraped_prefixes)
    for car in all_cars:
        prefix = car["type_code_full"][:4]
        if prefix in seen:
            continue
        seen.add(prefix)
        remaining.append(car)
    scraped_prefixes.update(seen)
    return remaining, False


def main():
    parser = argparse.ArgumentParser(
        description="RealOEM BMW Parts Scraper - EGY full run"
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
    logger.info(f"=== RealOEM BMW Scraper starting - mode: {mode_str} ===")

    # --- Initialise DB ---
    try:
        from storage.db import ensure_table
        ensure_table()
    except Exception as e:
        logger.warning(f"DB init skipped: {e}")

    # --- Restore ALL files from DB ---
    restore_files_from_db()

    # --- Storage layer ---
    notes      = NotesWriter(VFINAL_NOTES_FILE)
    checkpoint = CheckpointManager(CHECKPOINT_FILE)
    progress   = ProgressWriter(PROGRESS_FILE)

    start_virtual_display()

    session = 0

    try:
        while True:
            session += 1

            # Use ONLY checkpoint as the source of truth for which cars are done.
            # progress.csv is an audit log only -- it must NOT be used to skip cars,
            # because it can become inconsistent with checkpoint after a Railway crash
            # (e.g. progress.mark_completed fires but checkpoint.mark_car_done does not).
            scraped_prefixes = checkpoint.get_done_prefixes()

            logger.info(
                f"Browser session {session} - "
                f"{len(scraped_prefixes)} prefixes done so far"
            )

            need_restart = False
            interrupted  = False

            with sync_playwright() as p:
                browser = None          # defined early so finally can reference it safely
                cars_this_session = 0

                try:
                    browser, context, page = launch_browser(p)
                    cars, needs_followup = _get_cars_for_session(
                        page, sample_mode, scraped_prefixes, notes, checkpoint
                    )

                    if not cars and not needs_followup:
                        logger.info("All EGY cars scraped! Scraper done.")
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
                            logger.info(
                                f"Finished {type_code}: {parts_count} parts"
                            )
                        except BrowserCrashError as e:
                            logger.error(
                                f"Browser crashed scraping {type_code}: {e} "
                                f"- restarting browser and resuming from checkpoint"
                            )
                            need_restart = True
                            break
                        except Exception as e:
                            logger.error(
                                f"Failed to scrape {type_code}: {e}",
                                exc_info=True,
                            )

                        cars_this_session += 1

                        if cars_this_session >= BROWSER_RESTART_EVERY:
                            need_restart = True
                            logger.info(
                                f"Scraped {BROWSER_RESTART_EVERY} cars - "
                                f"restarting browser to clear memory"
                            )
                            break

                    # If Phase 1 was used and no restart was triggered yet,
                    # force a restart so Phase 2 runs in the next session
                    if needs_followup and not need_restart:
                        need_restart = True
                        logger.info(
                            "Phase 1 complete - restarting browser to "
                            "discover and scrape remaining cars (Phase 2)"
                        )

                except KeyboardInterrupt:
                    interrupted = True
                    logger.info("Interrupted by user. Progress saved.")

                except Exception as e:
                    logger.error(
                        f"Unexpected session error: {e} - restarting browser",
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
                # The for-loop finished without hitting the 4-car restart limit.
                # Force another session so _get_cars_for_session can check whether
                # there are NEW EGY cars on RealOEM beyond the ones we already know.
                # The scraper stops ONLY when _get_cars_for_session returns ([], False),
                # which happens after a fresh enumeration that finds zero remaining cars.
                need_restart = True
                logger.info(
                    "All cars in this batch processed -- restarting to check "
                    "for more EGY cars on RealOEM."
                )

    finally:
        stop_virtual_display()
        logger.info("=== Scraper done ===")


if __name__ == "__main__":
    main()

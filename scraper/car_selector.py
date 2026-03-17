"""
car_selector.py

Builds the list of cars to scrape.

Full mode (default):
  - EGY market only — skips any model not available in EGY
  - ALL production dates per model (not just the first)
  - ALL engines per production date
  - 4-char prefix dedup: if type_code_full[:4] (e.g. "VA99") has already been
    scraped (or seen in this run), skip that type code silently

Sample mode (--sample flag / sample_mode=True):
  - Only scrapes SAMPLE_SERIES / SAMPLE_MODEL with the same EGY logic
  - Used for local testing
"""

import logging

from scraper.filters import is_diesel
from scraper import discovery as disc

logger = logging.getLogger(__name__)

# Hard-coded sample constants for test runs
SAMPLE_SERIES = "E90"
SAMPLE_MODEL  = "320i"


def build_car_list(page, sample_mode=False, scraped_prefixes=None):
    """
    Generator yielding car dicts ready for parts scraping.

    Parameters
    ----------
    page             : Playwright page
    sample_mode      : If True, only yield SAMPLE_SERIES / SAMPLE_MODEL cars
    scraped_prefixes : set of 4-char type-code prefixes already scraped.
                       Cars whose prefix is in this set are silently skipped.
                       The set is mutated in-place as new prefixes are yielded,
                       so within-run duplicates are also avoided automatically.

    Each yielded dict has keys:
      series_value, series_label, body, model, market,
      prod_month, engine, steering, type_code_full
    """
    if scraped_prefixes is None:
        scraped_prefixes = set()

    all_series = disc.get_all_series(page)

    for series_info in all_series:
        series_val   = series_info["value"]
        series_label = series_info["label"]

        if sample_mode and series_val != SAMPLE_SERIES:
            continue

        logger.info(f"Processing series: {series_label}")
        bodies = disc.get_bodies(page, series_val)

        for body_info in bodies:
            body_val = body_info["value"]
            models   = disc.get_models(page, series_val, body_val)

            for model_info in models:
                model_val = model_info["value"]

                if sample_mode and model_val != SAMPLE_MODEL:
                    continue

                if is_diesel(model_val):
                    logger.debug(f"Skipping diesel: {model_val}")
                    continue

                # ---- EGY-only market ----
                markets = disc.get_markets(page, series_val, body_val, model_val)
                if "EGY" not in markets:
                    logger.debug(
                        f"No EGY market for {series_val}/{body_val}/{model_val}, skipping"
                    )
                    continue

                market = "EGY"

                # ---- ALL production dates ----
                prods = disc.get_prods(page, series_val, body_val, model_val, market)
                if not prods:
                    logger.warning(
                        f"No prod dates for {series_val}/{body_val}/{model_val}/{market}"
                    )
                    continue

                for prod in prods:
                    # ---- ALL engines ----
                    engines = disc.get_engines(
                        page, series_val, body_val, model_val, market, prod
                    )
                    if not engines:
                        logger.warning(
                            f"No engines for "
                            f"{series_val}/{body_val}/{model_val}/{market}/{prod}"
                        )
                        continue

                    for engine in engines:
                        result = disc.get_type_code_full(
                            page, series_val, body_val, model_val,
                            market, prod, engine
                        )
                        if result is None:
                            logger.warning(
                                f"No type_code for "
                                f"{series_val}/{body_val}/{model_val}/"
                                f"{market}/{prod}/{engine}"
                            )
                            continue

                        tc_full = result["type_code_full"]
                        prefix  = tc_full[:4]

                        # ---- 4-char prefix dedup ----
                        if prefix in scraped_prefixes:
                            logger.info(
                                f"Skipping (prefix {prefix!r} already done): {tc_full}"
                            )
                            continue

                        # Reserve this prefix so within-run duplicates are skipped
                        scraped_prefixes.add(prefix)

                        car = {
                            "series_value":   series_val,
                            "series_label":   series_label,
                            "body":           body_val,
                            "model":          model_val,
                            "market":         market,
                            "prod_month":     prod,
                            "engine":         engine,
                            "steering":       result["steering"],
                            "type_code_full": tc_full,
                        }
                        logger.info(f"Car ready: {tc_full}")
                        yield car

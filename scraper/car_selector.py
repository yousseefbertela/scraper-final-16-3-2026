"""
car_selector.py

High-level orchestration of car selection.
Iterates all series -> bodies -> models (diesel-filtered) -> markets ->
production dates -> engines, and delegates steering + type-code extraction
to discovery.get_type_code_full().

In sample mode only SAMPLE_SERIES / SAMPLE_MODEL are processed.
"""

import logging

from scraper.filters import is_diesel
from scraper import discovery as disc
from config import SAMPLE_SERIES, SAMPLE_MODEL

logger = logging.getLogger(__name__)


def build_car_list(page, sample_mode=True):
    """
    Generator yielding car dicts ready for parts scraping.

    Each dict keys:
      series_value, series_label, body, model, market,
      prod_month, engine, steering, type_code_full
    """
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

                # ---- Market selection with EGY -> EUR fallback ----
                markets = disc.get_markets(page, series_val, body_val, model_val)
                market_priority = [m for m in ["EGY", "EUR"] if m in markets]
                if not market_priority:
                    logger.info(
                        f"No EGY/EUR market for {series_val}/{body_val}/{model_val}, skipping"
                    )
                    continue

                for market in market_priority:
                    # ---- Production date: first available only ----
                    prods = disc.get_prods(page, series_val, body_val, model_val, market)
                    if not prods:
                        logger.warning(
                            f"No prod dates for {series_val}/{body_val}/{model_val}/{market}"
                        )
                        continue
                    prod = prods[0]

                    # ---- Engines: scrape ALL of them ----
                    engines = disc.get_engines(page, series_val, body_val, model_val, market, prod)
                    if not engines:
                        logger.warning(
                            f"No engines for {series_val}/{body_val}/{model_val}/{market}/{prod}"
                        )
                        continue

                    found_any = False
                    for engine in engines:
                        result = disc.get_type_code_full(
                            page, series_val, body_val, model_val, market, prod, engine
                        )
                        if result is None:
                            logger.warning(
                                f"No type_code for "
                                f"{series_val}/{body_val}/{model_val}/{market}/{prod}/{engine}"
                            )
                            continue

                        found_any = True
                        car = {
                            "series_value":   series_val,
                            "series_label":   series_label,
                            "body":           body_val,
                            "model":          model_val,
                            "market":         market,
                            "prod_month":     prod,
                            "engine":         engine,
                            "steering":       result["steering"],
                            "type_code_full": result["type_code_full"],
                        }
                        logger.info(f"Car ready: {result['type_code_full']}")
                        yield car

                    if found_any:
                        break  # found cars with this market, don't try next market

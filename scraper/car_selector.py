"""
car_selector.py

Direct-navigation car lookup for the 5-scraper architecture.

Each scraper loads its car list from the DB (scraper_car_lists table).
Every car entry already knows: code, series, model, body, engine, market, prod_month.

find_car_type_code(page, car_info) navigates RealOEM dropdowns directly using
these known values to obtain the full type_code_full URL parameter needed for
parts scraping.  No full-site enumeration is performed.

Market handling:
  - EGY cars (market="EGY"): enumerate production months to find the one whose
    type_code_full starts with the expected 4-char code.
  - EUR cars (market="EUR"): prod_month is known; navigate directly.

LHD is always preferred; falls back to first available steering.
"""

import logging

from scraper.filters import is_diesel
from scraper import discovery as disc

logger = logging.getLogger(__name__)


def find_car_type_code(page, car_info: dict):
    """
    Navigate RealOEM to find the type_code_full for a car from scraper_car_lists.

    Parameters
    ----------
    page      : Playwright page
    car_info  : dict with keys: code, series, model, body, engine, market, prod_month

    Returns a full car dict (ready for parts scraping) with type_code_full set,
    or None if the car could not be found.
    """
    code   = car_info["code"]        # 4-char prefix, e.g. "NA36"
    series = car_info.get("series", "")
    market = car_info.get("market", "EUR")
    engine = car_info.get("engine", "")
    body   = car_info.get("body", "")

    # Clean model name (remove brand prefix if accidentally included)
    model = car_info.get("model", "").strip()
    for brand in ("BMW ", "MINI "):
        if model.startswith(brand):
            model = model[len(brand):]

    # Convert prod_month "YYYY-MM" → "YYYYMM" (RealOEM format)
    prod_raw   = car_info.get("prod_month")
    prod_known = prod_raw.replace("-", "") if prod_raw else None

    if not series:
        logger.warning(f"Car {code}: no series value, skipping")
        return None

    if is_diesel(model):
        logger.info(f"Car {code}: diesel model {model!r}, skipping")
        return None

    # ── Fast path: construct type_code_full directly (no dropdown navigation) ──
    # Works when prod_month is known (all EUR cars + any EGY with known prod_month)
    if prod_known and len(prod_known) == 6:
        tc = _try_construct_type_code(page, code, series, model, market, prod_known)
        if tc:
            return _build_car_dict(car_info, {"type_code_full": tc, "steering": ""}, series, body, model, prod_known)

    # ── Step 1: find the body dropdown value where our model appears ──────
    target_body = _find_body_for_model(page, series, model, body)
    if target_body is None:
        logger.warning(f"Car {code}: model {model!r} not found in series {series}")
        return None

    # ── Step 2: get type_code_full ────────────────────────────────────────
    if prod_known:
        # EUR path: prod month is known — navigate directly
        result = disc.get_type_code_full(
            page, series, target_body, model, market, prod_known, engine
        )
        if result:
            return _build_car_dict(car_info, result, series, target_body, model, prod_known)
        logger.warning(
            f"Car {code}: direct navigation failed "
            f"({series}/{target_body}/{model}/{market}/{prod_known}/{engine})"
        )
        return None

    else:
        # EGY path: enumerate prod months, find the one matching our 4-char code
        prods = disc.get_prods(page, series, target_body, model, market)
        if not prods:
            logger.warning(f"Car {code}: no prod months for {series}/{target_body}/{model}/{market}")
            return None

        for prod in prods:
            engines = disc.get_engines(page, series, target_body, model, market, prod)
            if engine and engine not in engines:
                continue
            result = disc.get_type_code_full(
                page, series, target_body, model, market, prod, engine
            )
            if result:
                tc = result["type_code_full"]
                if tc[:4] == code:
                    return _build_car_dict(car_info, result, series, target_body, model, prod)

        logger.warning(
            f"Car {code}: could not find matching type_code "
            f"in {series}/{target_body}/{model}/{market} over {len(prods)} prod months"
        )
        return None


# ── Helpers ───────────────────────────────────────────────────────────────

def _try_construct_type_code(page, code: str, series: str, model: str,
                              market: str, prod_known: str) -> str | None:
    """
    Attempt to construct type_code_full directly and verify it works on RealOEM.
    Format: {CODE}-{MARKET}-{MM}-{YYYY}-{SERIES}-{MAKE}-{MODEL}
    Navigates directly to partgrp?id=... — zero dropdown interaction.
    Returns type_code_full if the page loads successfully, None otherwise.
    """
    from config import BASE_URL
    from scraper.browser import safe_goto
    from bs4 import BeautifulSoup

    year  = prod_known[:4]   # "201507" → "2015"
    month = prod_known[4:]   # "201507" → "07"

    # Determine make: MINI models don't contain BMW model naming patterns
    mini_keywords = ("cooper", "one ", "countryman", "clubman", "paceman", "roadster")
    make = "MINI" if any(k in model.lower() for k in mini_keywords) else "BMW"

    candidate = f"{code}-{market}-{month}-{year}-{series}-{make}-{model}"

    try:
        safe_goto(page, f"{BASE_URL}/bmw/enUS/partgrp?id={candidate}")
        soup = BeautifulSoup(page.content(), "html.parser")
        # Confirm we got real group data (not an error page)
        has_groups = bool(soup.find_all("a", href=lambda h: h and "showparts" in str(h)))
        if has_groups:
            logger.info(f"Car {code}: direct type_code constructed instantly → {candidate}")
            return candidate
    except Exception as e:
        logger.debug(f"Car {code}: direct construct failed for {candidate}: {e}")

    return None


def _find_body_for_model(page, series: str, model: str, hint_body: str) -> str | None:
    """
    Return the body dropdown value for the given series where `model` appears.
    Tries `hint_body` first (fast path), then searches all bodies.
    """
    # Fast path: body from car list matches directly
    if hint_body:
        models_in_hint = [m["value"] for m in disc.get_models(page, series, hint_body)]
        if model in models_in_hint:
            return hint_body

    # Search all available bodies
    bodies = disc.get_bodies(page, series)
    for body_info in bodies:
        b = body_info["value"]
        if b == hint_body:
            continue  # already tried
        models_in_b = [m["value"] for m in disc.get_models(page, series, b)]
        if model in models_in_b:
            return b

    return None


def _build_car_dict(car_info: dict, result: dict,
                    series_value: str, body: str, model: str, prod_month: str) -> dict:
    return {
        "type_code_full": result["type_code_full"],
        "series_value":   series_value,
        "series_label":   series_value,   # simplified; parts_scraper only needs type_code_full
        "body":           body,
        "model":          model,
        "market":         car_info.get("market", "EUR"),
        "prod_month":     prod_month,
        "engine":         car_info.get("engine", ""),
        "steering":       result.get("steering", ""),
    }

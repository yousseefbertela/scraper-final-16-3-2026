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

    # ── Step 1: find the body dropdown value where our model appears ──────
    target_body = _find_body_for_model(page, series, model, body)
    if target_body is None:
        logger.warning(f"Car {code}: model {model!r} not found in series {series}")
        return None

    # ── Step 2: navigate dropdowns exactly like a human ──────────────────
    #
    # EUR cars (prod_month known): go directly to exact prod_month via dropdowns.
    # EGY cars (no prod_month):    enumerate all prod months, match by 4-char prefix.
    #
    # For EUR: use _ajax_get_type_code directly — no URL-param guessing.
    # If type_code prefix doesn't match our code → skip car.

    if prod_known:
        # EUR path: step-by-step dropdown navigation with known prod_month.
        # If the exact prod_month isn't in the dropdown, use the closest available
        # prod_month that is ≤ prod_known (i.e. the car was produced by that date).
        prod_to_use = _resolve_prod_month(
            page, code, series, target_body, model, market, prod_known
        )
        if prod_to_use is None:
            logger.warning(
                f"Car {code}: no usable prod_month found in dropdown "
                f"({series}/{target_body}/{model}/{market}) — skipping"
            )
            return None

        tc = disc._ajax_get_type_code(
            page, series, target_body, model, market, prod_to_use, engine
        )
        if not tc:
            logger.warning(
                f"Car {code}: step-by-step navigation failed "
                f"({series}/{target_body}/{model}/{market}/{prod_to_use}/{engine}) — skipping"
            )
            return None
        if tc[:4] != code:
            logger.warning(
                f"Car {code}: type_code prefix mismatch — got {tc[:4]}, expected {code} — skipping"
            )
            return None
        logger.info(f"Car {code}: found via step-by-step → {tc}")
        return _build_car_dict(car_info, {"type_code_full": tc, "steering": ""}, series, target_body, model, prod_to_use)

    else:
        # EGY path: enumerate prod months, match by 4-char prefix
        prods = disc.get_prods(page, series, target_body, model, market)
        if not prods:
            logger.warning(f"Car {code}: no prod months for {series}/{target_body}/{model}/{market}")
            return None

        for prod in prods:
            engines = disc.get_engines(page, series, target_body, model, market, prod)
            if engine and engine not in engines:
                continue
            tc = disc._ajax_get_type_code(
                page, series, target_body, model, market, prod, engine
            )
            if tc and tc[:4] == code:
                logger.info(f"Car {code}: found via enumeration → {tc}")
                return _build_car_dict(car_info, {"type_code_full": tc, "steering": ""}, series, target_body, model, prod)

        logger.warning(
            f"Car {code}: no matching type_code found in "
            f"{series}/{target_body}/{model}/{market} over {len(prods)} prod months — skipping"
        )
        return None


# ── Helpers ───────────────────────────────────────────────────────────────

def _resolve_prod_month(page, code: str, series: str, body: str, model: str,
                        market: str, prod_known: str) -> str | None:
    """
    Return the best prod_month to use for navigation.

    1. If prod_known exists exactly in the dropdown → use it directly.
    2. Otherwise fetch the available list and pick the closest prod ≤ prod_known
       (the latest production date that is at or before the car's known month).
    3. If no prod ≤ prod_known exists, use the earliest available prod instead.
    4. Returns None if no prods are available at all.
    """
    available = disc.get_prods(page, series, body, model, market)
    if not available:
        return None

    if prod_known in available:
        return prod_known

    # Find closest prod <= prod_known (same or earlier)
    earlier = [p for p in available if p <= prod_known]
    if earlier:
        closest = max(earlier)
        logger.info(
            f"Car {code}: prod_month {prod_known} not in dropdown "
            f"— using closest earlier: {closest}"
        )
        return closest

    # All available prods are after prod_known; use the earliest one
    closest = min(available)
    logger.info(
        f"Car {code}: prod_month {prod_known} not in dropdown and no earlier option "
        f"— using earliest available: {closest}"
    )
    return closest


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

"""
car_selector.py  -  Target-list driven RealOEM navigation (new architecture)

Navigation flow per car:
  1. Read core fields  : code, series, model, market, engine
  2. Read custom cols  : catalog, series_label, body, prod_month, steering
  3. Navigate RealOEM  :
       a. Load SELECT_URL with ?archive=1 for Classic, default for Current
       b. Select series    -> match series_label TEXT in dropdown (not value)
       c. Select body      -> directly from custom 'body' col
       d. Select model     -> from core field
       e. Select market    -> from core field
       f. Select prod_month-> EUR: try stored prod then expand outward until
                             engine is in dropdown | EGY: first dropdown option
       g. Select engine    -> from core field
       h. Select steering  -> prefer custom col, else Left-hand drive, else first
  4. Extract type_code_full
  5. Verify prefix == code  ->  scrape  |  mismatch  ->  skip + warn
"""

import logging
import time
import unicodedata

from bs4 import BeautifulSoup

from scraper.browser import safe_goto
from scraper.filters import is_diesel
from scraper.discovery import _extract_type_code
from config import SELECT_URL

logger = logging.getLogger(__name__)

# -- Custom column IDs (from DB _columns row) ----------------------------------
COL_SERIES_LABEL = "col_1775163870194_93n2x"
COL_BODY         = "col_1775163882980_nr3xh"
COL_STEERING     = "col_1775163897641_k0d2n"
COL_PROD_MONTH   = "col_1775163979435_jilnc"
COL_BRAND        = "col_1775163999384_4jryt"
COL_CATALOG      = "col_1775164005280_2rqu5"


# -- Public entry point --------------------------------------------------------

def find_car_type_code(page, car_info: dict):
    """
    Navigate RealOEM using target-list data to find type_code_full.

    Parameters
    ----------
    page     : Playwright page
    car_info : dict from scraper_car_lists (includes 'custom' sub-dict)

    Returns full car dict with type_code_full set, or None on failure.
    """
    code   = car_info.get("code", "")
    market = car_info.get("market", "EUR")
    engine = car_info.get("engine", "")

    # Strip brand prefix from model if accidentally present
    model = car_info.get("model", "").strip()
    for brand in ("BMW ", "MINI "):
        if model.startswith(brand):
            model = model[len(brand):]

    # -- Pull custom column values ---------------------------------------------
    custom        = car_info.get("custom") or {}
    catalog       = custom.get(COL_CATALOG,      "").strip()
    series_label  = custom.get(COL_SERIES_LABEL, "").strip()
    body          = custom.get(COL_BODY,         car_info.get("body", "")).strip()
    steering_pref = custom.get(COL_STEERING,     "").strip()

    # prod_month: DB stores YYYYMM00 (8-digit) — keep as-is; RealOEM dropdown
    # values are also 8-digit (e.g. "20100100"), so we match directly.
    custom_prod = custom.get(COL_PROD_MONTH, "").strip().replace("-", "")

    # -- Guards ----------------------------------------------------------------
    if is_diesel(model):
        logger.info(f"Car {code}: diesel model {model!r} -- skipping")
        return None

    if not series_label:
        logger.warning(f"Car {code}: series_label missing in custom columns -- skipping")
        return None

    if not body:
        logger.warning(f"Car {code}: body missing in custom columns -- skipping")
        return None

    logger.info(
        f"Car {code}: navigating | catalog={catalog!r} | "
        f"series_label={series_label!r} | body={body!r} | model={model!r} | "
        f"market={market} | prod={custom_prod or 'EGY-first'} | engine={engine}"
    )

    # -- Navigate --------------------------------------------------------------
    tc = _navigate(
        page, code,
        catalog=catalog,
        series_label=series_label,
        body=body,
        model=model,
        market=market,
        prod_known=custom_prod,
        engine=engine,
        steering_pref=steering_pref,
    )

    if not tc:
        logger.warning(f"Car {code}: navigation failed -- no type_code found")
        return None

    if tc[:4] != code:
        logger.warning(
            f"Car {code}: type_code prefix mismatch -- "
            f"got {tc[:4]!r}, expected {code!r} -- skipping"
        )
        return None

    logger.info(f"Car {code}: matched -> {tc}")

    # Reconstruct prod_month used from type_code_full (CODE-MKT-MM-YYYY-...)
    parts = tc.split("-")
    prod_used = ""
    if len(parts) >= 4:
        try:
            prod_used = parts[3] + parts[2].zfill(2)   # YYYY + MM -> YYYYMM
        except Exception:
            prod_used = custom_prod

    return {
        "type_code_full": tc,
        "series_value":   car_info.get("series", ""),
        "series_label":   series_label,
        "body":           body,
        "model":          model,
        "market":         market,
        "prod_month":     prod_used or custom_prod or "",
        "engine":         engine,
        "steering":       steering_pref,
    }


# -- Core navigation -----------------------------------------------------------

def _navigate(page, code, *, catalog, series_label, body, model,
              market, prod_known, engine, steering_pref):
    """
    Step-by-step RealOEM form navigation.
    Returns type_code_full string or None.
    """

    # Step 1: Load page with correct catalog context
    # Classic catalog is controlled via ?archive=1 URL param on RealOEM,
    # NOT via a named select dropdown element.
    if catalog.lower() == "classic":
        safe_goto(page, SELECT_URL + "?archive=1")
        logger.info(f"Car {code}: Classic catalog loaded (archive=1)")
    else:
        safe_goto(page, SELECT_URL)

    # Step 2: Series -- find option by matching label text
    series_val = _find_option_by_label(page, "series", series_label)
    if not series_val:
        logger.warning(
            f"Car {code}: series_label {series_label!r} not found in dropdown"
        )
        return None
    if not _sel_nav(page, "series", series_val):
        return None

    # Step 3: Body -- select directly by value; if not found search all bodies
    if not _sel_nav(page, "body", body):
        logger.info(
            f"Car {code}: body {body!r} not in dropdown -- "
            f"searching all body types for model {model!r}"
        )
        body = _find_body_for_model(page, model)
        if body is None:
            logger.warning(f"Car {code}: model {model!r} not found in any body type")
            return None
        if not _sel_nav(page, "body", body):
            return None

    # Step 4: Model
    if not _sel_nav(page, "model", model):
        logger.warning(f"Car {code}: model {model!r} not found in dropdown")
        return None

    # Step 5: Market
    if not _sel_nav(page, "market", market):
        logger.warning(f"Car {code}: market {market!r} not found in dropdown")
        return None

    # Step 6 + 7: Prod month + Engine
    # For EUR: try prod_months ordered by closeness to prod_known, pick the
    #          first one that has our engine in its dropdown.
    # For EGY: just pick the first prod option.

    if market == "EGY" or not prod_known:
        # EGY: first available prod_month, then select engine
        prod_val = _get_first_option(page, "prod")
        if not prod_val:
            logger.warning(f"Car {code}: no prod_month options available")
            return None
        logger.info(f"Car {code}: EGY -- using first prod_month: {prod_val}")
        if not _sel_nav(page, "prod", prod_val):
            return None
        if not _sel_nav(page, "engine", engine):
            logger.warning(f"Car {code}: engine {engine!r} not in dropdown at prod {prod_val}")
            return None
    else:
        # EUR: scan prod_months ordered by distance from prod_known
        prod_val = _find_prod_with_engine(page, prod_known, engine, code)
        if prod_val is None:
            logger.warning(
                f"Car {code}: no prod_month found that has engine {engine!r}"
            )
            return None
        # prod_val already selected by _find_prod_with_engine; now select engine
        if not _sel_nav(page, "engine", engine):
            logger.warning(f"Car {code}: engine {engine!r} select failed")
            return None

    # Step 8: Steering (optional)
    _handle_steering(page, steering_pref)

    # Wait for Browse Parts to appear
    try:
        page.wait_for_selector(
            "a[href*='partgrp'], form[action*='partgrp']", timeout=6000
        )
    except Exception:
        pass

    soup = BeautifulSoup(page.content(), "html.parser")
    return _extract_type_code(soup)


# -- Helpers -------------------------------------------------------------------

def _sel_nav(page, name: str, value: str) -> bool:
    """
    Select `value` in <select name=name> and wait for the page reload.
    Returns True on success, False if option not found or error.
    """
    try:
        el = page.locator(f"select[name='{name}']").first
        el.wait_for(state="visible", timeout=12000)

        if page.locator(f"select[name='{name}'] option[value='{value}']").count() == 0:
            logger.warning(f"Option {value!r} not in <select name={name!r}>")
            return False

        with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            el.select_option(value=value)
        time.sleep(1.5)
        return True
    except Exception as e:
        logger.warning(f"_sel_nav({name!r}, {value!r}): {e}")
        return False


def _find_option_by_label(page, name: str, target_label: str) -> str | None:
    """
    Scan <select name=name> options and return the VALUE whose label
    fuzzy-matches target_label. Returns None if not found.
    """
    try:
        sel = page.locator(f"select[name='{name}']").first
        sel.wait_for(state="visible", timeout=10000)
        for opt in page.locator(f"select[name='{name}'] option").all():
            label = (opt.inner_text() or "").strip()
            val   = (opt.get_attribute("value") or "").strip()
            if not val or val.startswith("-"):
                continue
            if _labels_match(label, target_label):
                logger.debug(f"Matched {target_label!r} -> value={val!r}")
                return val
    except Exception as e:
        logger.warning(f"_find_option_by_label({name!r}): {e}")
    return None


def _get_all_options(page, name: str) -> list:
    """Return all non-blank option values from <select name=name>."""
    try:
        return [
            (opt.get_attribute("value") or "").strip()
            for opt in page.locator(f"select[name='{name}'] option").all()
            if (opt.get_attribute("value") or "").strip()
            and not (opt.get_attribute("value") or "").startswith("-")
        ]
    except Exception:
        return []


def _get_first_option(page, name: str) -> str | None:
    """Return first non-blank option value from <select name=name>."""
    opts = _get_all_options(page, name)
    return opts[0] if opts else None


def _find_prod_with_engine(page, prod_known: str, engine: str, code: str) -> str | None:
    """
    Try available prod_months ordered by closeness to prod_known.
    For each: select it, check if `engine` is in the engine dropdown.
    Returns the prod_val that worked (page left in that state), or None.
    """
    available = _get_all_options(page, "prod")
    if not available:
        return None

    # Sort by distance from prod_known (closest first), then by value for ties
    def dist(p):
        try:
            return abs(int(p) - int(prod_known))
        except Exception:
            return 999999

    candidates = sorted(available, key=lambda p: (dist(p), p))

    for i, prod_val in enumerate(candidates):
        if not _sel_nav(page, "prod", prod_val):
            continue

        # Check engine dropdown
        engine_opts = _get_all_options(page, "engine")
        if engine in engine_opts:
            logger.info(
                f"Car {code}: prod_month {prod_val!r} has engine {engine!r} "
                f"(tried {i+1}/{len(candidates)} options)"
            )
            return prod_val

        # Engine not here; loop to try next prod
        logger.debug(
            f"Car {code}: prod {prod_val!r} -- engine {engine!r} not available "
            f"(engines: {engine_opts})"
        )

    return None


def _resolve_prod(page, prod_known: str) -> str | None:
    """Legacy helper: exact match, else closest earlier, else earliest."""
    available = _get_all_options(page, "prod")
    if not available:
        return None
    if prod_known in available:
        return prod_known
    earlier = [p for p in available if p <= prod_known]
    return max(earlier) if earlier else min(available)


def _labels_match(dropdown_label: str, target: str) -> bool:
    """
    Fuzzy match: normalize unicode spaces/dashes/quotes, collapse whitespace,
    normalize spaces-around-hyphens, compare lowercase.
    Handles all common RealOEM unicode variants including:
      em-space + em-dash + em-space -> " - " which we normalize to "-"
    """
    import re
    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKC", s)
        s = (s
             .replace("\u2014", "-")   # em-dash
             .replace("\u2013", "-")   # en-dash
             .replace("\u2003", " ")   # em-space
             .replace("\u2002", " ")   # en-space
             .replace("\u00a0", " ")   # non-breaking space
             .replace("\u2019", "'")   # right single quote -> apostrophe
             .replace("\u2018", "'")   # left single quote  -> apostrophe
             )
        s = " ".join(s.lower().split())       # collapse whitespace
        s = re.sub(r'\s*-\s*', '-', s)        # "2005 - 2010" -> "2005-2010"
        return s
    return norm(dropdown_label) == norm(target)


def _find_body_for_model(page, model: str) -> str | None:
    """
    Fallback: iterate all body options and return the first whose
    model dropdown contains `model`. Used when stored body value
    doesn't match a RealOEM dropdown value (e.g. "Sedan" vs "Lim").
    """
    body_opts = _get_all_options(page, "body")
    for body_val in body_opts:
        # Select body to populate model dropdown
        if not _sel_nav(page, "body", body_val):
            continue
        model_opts = _get_all_options(page, "model")
        if model in model_opts:
            logger.info(f"Body fallback: model {model!r} found in body={body_val!r}")
            return body_val
        # Body didn't have model; reset by going back to first body
        # (next _sel_nav call will re-select from current page state)
    return None


def _handle_steering(page, steering_pref: str):
    """
    Select steering if dropdown is present.
    Priority: custom steering_pref -> Left-hand drive -> first available.
    """
    try:
        steer_el = page.locator("select[name='steering']")
        steer_el.wait_for(state="visible", timeout=4000)
        valid = [
            o for o in page.locator("select[name='steering'] option").all()
            if (o.get_attribute("value") or "").strip()
            and not (o.get_attribute("value") or "").startswith("-")
        ]
        if not valid:
            return
        chosen = None
        if steering_pref:
            chosen = next(
                (o for o in valid
                 if steering_pref.lower() in (o.inner_text() or "").lower()),
                None
            )
        if not chosen:
            chosen = next(
                (o for o in valid if "left" in (o.inner_text() or "").lower()),
                valid[0]
            )
        _sel_nav(page, "steering", chosen.get_attribute("value") or "")
    except Exception:
        pass  # No steering dropdown -- that's fine

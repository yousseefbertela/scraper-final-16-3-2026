"""
RealOEM dropdown discovery via URL-parameter navigation.

Instead of relying on AJAX dropdown chaining, we navigate to the select page
with query parameters and let the server return pre-populated HTML. Each
function is independent and navigates directly to the right URL.

When URL-param navigation fails to reveal a Browse Parts link (it doesn't
trigger JavaScript onChange events), a step-by-step form-navigation fallback
is used: each dropdown selection causes the form to submit via GET (page reload),
so we use page.expect_navigation() to properly wait for each reload.
"""

import re
import time
import logging
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from scraper.browser import safe_goto, human_delay
from config import SELECT_URL, ACTION_DELAY

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Classic-catalog detection                                           #
# ------------------------------------------------------------------ #

# These BMW series live in RealOEM's "Classic" catalog, not "Current".
_CLASSIC_SERIES = frozenset({
    "E21", "E23", "E24", "E28", "E30", "E31", "E32", "E34",
    "E36", "E38", "E39", "E46", "E52", "E53",
})


def _catalog_for(series: str) -> str:
    """Return 'Classic' if this series lives in the Classic catalog, else ''."""
    return "Classic" if series in _CLASSIC_SERIES else ""


# ------------------------------------------------------------------ #
# Internal helpers                                                     #
# ------------------------------------------------------------------ #

def _nav(page, **params):
    """Navigate to SELECT_URL with given query params, return BeautifulSoup."""
    qs = urlencode({"product": "P", **params})
    url = f"{SELECT_URL}?{qs}"
    safe_goto(page, url)
    return BeautifulSoup(page.content(), "html.parser")


def _read_select(soup, name):
    """Return [{value, label}] from <select name=name>, skipping blanks."""
    sel = soup.find("select", {"name": name})
    if not sel:
        return []
    result = []
    for opt in sel.find_all("option"):
        v = (opt.get("value") or "").strip()
        l = opt.get_text(strip=True)
        # Skip blank / placeholder rows
        if not v or v.startswith("-") or l.startswith("-") or not l:
            continue
        result.append({"value": v, "label": l})
    return result


def _extract_type_code(soup):
    """Find Browse Parts form/link and extract type_code_full.

    RealOEM renders Browse Parts as a <form action='/bmw/enUS/partgrp'>
    with a hidden <input type='hidden' value='XX99-EUR-...'> and a submit
    button - NOT as an <a> link.  We check the form hidden inputs first,
    then fall back to link scanning.
    """
    # Primary: form with action containing partgrp/showparts + hidden input
    for form in soup.find_all("form"):
        action = form.get("action", "")
        if "partgrp" in action or "showparts" in action:
            for inp in form.find_all("input", attrs={"type": "hidden"}):
                val = (inp.get("value") or "").strip()
                if val.count("-") >= 4:
                    return val

    # Fallback: <a> tag href with id= param
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "partgrp" in href or "showparts" in href:
            m = re.search(r'[?&]id=([^& ]+)', href)
            if m:
                tc = m.group(1)
                if tc.count("-") >= 4:
                    return tc
    return None


def _ajax_get_type_code(page, series, body, model, market, prod, engine):
    """
    Fallback: fill form dropdowns step-by-step.

    The RealOEM select form submits via GET on each dropdown change (full page
    reload), so we use page.expect_navigation() to wait for the reload before
    proceeding to the next dropdown.

    For Classic-catalog series (E46, E36, etc.), selects the catalog dropdown
    first before navigating the rest of the form.

    Returns type_code_full string or None.
    """
    safe_goto(page, SELECT_URL)

    def sel_nav(name, value):
        """Select option and wait for the resulting page navigation."""
        selector = f"select[name='{name}']"
        try:
            el = page.locator(selector).first
            el.wait_for(state="visible", timeout=12000)
            # Check the option actually exists before selecting
            opt_sel = f"select[name='{name}'] option[value='{value}']"
            if page.locator(opt_sel).count() == 0:
                logger.warning(f"AJAX fallback: option {value!r} not found in {selector}")
                return False
            # The form submits on change, causing a GET navigation
            with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                el.select_option(value=value)
            time.sleep(1.5)
            return True
        except Exception as e:
            logger.warning(f"AJAX fallback: error selecting {value!r} in {selector}: {e}")
            return False

    # --- Classic catalog selection (E46, E36, etc.) ---
    catalog = _catalog_for(series)
    if catalog:
        logger.info(f"Series {series} is Classic catalog — selecting catalog dropdown first")
        try:
            cat_sel = page.locator("select[name='catalog']")
            cat_sel.wait_for(state="visible", timeout=6000)
            # Find the Classic option by label (case-insensitive)
            opts = page.locator("select[name='catalog'] option").all()
            classic_val = None
            for opt in opts:
                label = (opt.inner_text() or "").strip().lower()
                val   = (opt.get_attribute("value") or "").strip()
                if "classic" in label or "classic" in val.lower():
                    classic_val = val
                    break
            if classic_val is not None:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
                    cat_sel.select_option(value=classic_val)
                time.sleep(1.5)
                logger.info(f"Catalog set to Classic (value={classic_val!r})")
            else:
                logger.warning(f"Classic option not found in catalog dropdown for series {series}")
        except Exception as e:
            logger.warning(f"Could not select Classic catalog for series {series}: {e}")

    if not sel_nav("series",  series):  return None
    if not sel_nav("body",    body):    return None
    if not sel_nav("model",   model):   return None
    if not sel_nav("market",  market):  return None
    if not sel_nav("prod",    prod):    return None
    if not sel_nav("engine",  engine):  return None

    # Handle optional steering dropdown
    try:
        steering_sel = page.locator("select[name='steering']")
        steering_sel.wait_for(state="visible", timeout=4000)
        opts = page.locator("select[name='steering'] option").all()
        valid = [
            o for o in opts
            if (o.get_attribute("value") or "").strip()
            and not (o.get_attribute("value") or "").startswith("-")
        ]
        if valid:
            chosen = next(
                (o for o in valid if "left" in (o.inner_text() or "").lower()),
                valid[0]
            )
            sel_nav("steering", chosen.get_attribute("value"))
    except Exception:
        pass  # No steering dropdown - that's fine

    # Wait for Browse Parts link to appear (JavaScript may add it async)
    try:
        page.wait_for_selector("a[href*='partgrp']", timeout=6000)
    except Exception:
        pass

    soup = BeautifulSoup(page.content(), "html.parser")
    return _extract_type_code(soup)


# ------------------------------------------------------------------ #
# Public discovery functions                                           #
# ------------------------------------------------------------------ #

def get_all_series(page):
    """Return all BMW car series options: [{value, label}, ...]"""
    soup = _nav(page)
    series = _read_select(soup, "series")
    logger.info(f"Found {len(series)} series")
    return series


def get_bodies(page, series):
    """Return body type options for the given series."""
    params = {"series": series}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)
    bodies = _read_select(soup, "body")
    logger.debug(f"Series {series}: {len(bodies)} bodies")
    return bodies


def get_models(page, series, body):
    """Return model options for series + body."""
    params = {"series": series, "body": body}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)
    models = _read_select(soup, "model")
    logger.debug(f"{series}/{body}: {len(models)} models")
    return models


def get_markets(page, series, body, model):
    """Return available market value-strings for series+body+model."""
    params = {"series": series, "body": body, "model": model}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)
    markets = [o["value"] for o in _read_select(soup, "market")]
    logger.debug(f"{series}/{body}/{model} -> markets: {markets}")
    return markets


def get_prods(page, series, body, model, market):
    """Return production date values (YYYYMM) for given config."""
    params = {"series": series, "body": body, "model": model, "market": market}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)
    prods = [o["value"] for o in _read_select(soup, "prod")]
    logger.debug(f"{series}/{body}/{model}/{market} -> prods: {prods[:3]}")
    return prods


def get_engines(page, series, body, model, market, prod):
    """Return all available engine codes."""
    params = {"series": series, "body": body, "model": model,
              "market": market, "prod": prod}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)
    engines = [o["value"] for o in _read_select(soup, "engine")]
    logger.debug(f"Engines: {engines}")
    return engines


def get_type_code_full(page, series, body, model, market, prod, engine):
    """
    Navigate with all params. Try to extract type_code_full from Browse Parts link.
    If a steering dropdown is present, select LHD (or first available) and retry.
    If URL-param approach fails (Browse Parts requires JS onChange events to appear),
    try step-by-step form-navigation fallback.

    Returns dict {type_code_full, steering} or None on failure.
    """
    params = {"series": series, "body": body, "model": model,
              "market": market, "prod": prod, "engine": engine}
    cat = _catalog_for(series)
    if cat:
        params["catalog"] = cat
    soup = _nav(page, **params)

    # Happy path: Browse Parts link is already present
    tc = _extract_type_code(soup)
    if tc:
        logger.info(f"Type code: {tc}")
        return {"type_code_full": tc, "steering": ""}

    # Check if a steering dropdown needs to be filled
    steerings = _read_select(soup, "steering")
    if steerings:
        # Prefer Left-hand drive; fall back to first available
        chosen = next(
            (s for s in steerings if "left" in s["label"].lower()),
            steerings[0]
        )
        logger.debug(f"Steering choices: {[s['label'] for s in steerings]} -> selecting: {chosen['label']}")

        soup2 = _nav(page, **{**params, "steering": chosen["value"]})

        tc = _extract_type_code(soup2)
        if tc:
            logger.info(f"Type code: {tc} (steering: {chosen['label']})")
            return {"type_code_full": tc, "steering": chosen["label"]}

    # URL-param approach failed - Browse Parts link requires JS onChange events.
    # Use step-by-step form navigation fallback.
    logger.info(
        f"URL-param approach failed for {series}/{body}/{model}/{market}/{prod}/{engine}, "
        f"trying step-by-step form fallback..."
    )
    tc = _ajax_get_type_code(page, series, body, model, market, prod, engine)
    if tc:
        logger.info(f"Type code (form fallback): {tc}")
        return {"type_code_full": tc, "steering": ""}

    logger.warning(
        f"No Browse Parts found for "
        f"{series}/{body}/{model}/{market}/{prod}/{engine}"
    )
    return None

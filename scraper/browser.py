"""
Browser setup with anti-detection measures.
"""

import os
import re
import random
import time
import logging
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright_stealth import Stealth as _PlaywrightStealth

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_virtual_display = None

# Optional outbound proxy. RealOEM's Cloudflare hard-blocks datacenter egress
# IPs (seen on DigitalOcean from 2026-09-05) while the same browser passes from
# a residential/proxy IP. PROXY_SERVERS is a comma-separated host:port list;
# each browser launch rotates to the next one. Unset = direct, as before.
# PROXY_SERVERS/PROXY_USERNAME/PROXY_PASSWORD are the static fallback. With
# WEBSHARE_API_KEY set, the list is pulled from the Webshare account instead
# (every proxy on the plan, with its own credentials) and refreshed periodically,
# so a plan change or a replaced exit needs no redeploy.
_PROXY_SERVERS = [s.strip() for s in os.environ.get("PROXY_SERVERS", "").split(",") if s.strip()]
_PROXY_USERNAME = os.environ.get("PROXY_USERNAME") or None
_PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD") or None
_WEBSHARE_API_KEY = os.environ.get("WEBSHARE_API_KEY", "")
# Exits in these countries are tried first (closest to the Frankfurt datacenter
# and the ones that have passed Cloudflare so far); the rest are kept as spares.
_PROXY_PREFERRED_COUNTRIES = [
    c.strip().upper() for c in os.environ.get(
        "PROXY_PREFERRED_COUNTRIES", "DE,NL,GB,FR,BE,IE,DK,SE,CZ,ES,IT,PT,HU,LV,RO,HR,GR"
    ).split(",") if c.strip()
]
_PROXY_REFRESH_S = int(os.environ.get("PROXY_REFRESH_S", str(6 * 3600)))
_proxies_loaded_at = 0.0


def _load_proxies_from_webshare():
    """Replace the proxy list with the account's current list. Returns True on success."""
    global _PROXY_SERVERS, _PROXY_USERNAME, _PROXY_PASSWORD, _proxies_loaded_at
    if not _WEBSHARE_API_KEY:
        return False
    import json
    import urllib.request
    results = []
    for page in range(1, 11):
        req = urllib.request.Request(
            f"https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page={page}&page_size=100",
            headers={"Authorization": f"Token {_WEBSHARE_API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        results.extend(body.get("results") or [])
        if not body.get("next"):
            break
    valid = [p for p in results if p.get("valid") and p.get("proxy_address") and p.get("port")]
    if not valid:
        raise RuntimeError("Webshare returned no valid proxies")

    def rank(p):
        c = str(p.get("country_code") or "").upper()
        return _PROXY_PREFERRED_COUNTRIES.index(c) if c in _PROXY_PREFERRED_COUNTRIES else 999

    valid.sort(key=rank)
    _PROXY_SERVERS = [f"{p['proxy_address']}:{p['port']}" for p in valid]
    _PROXY_USERNAME = valid[0].get("username") or _PROXY_USERNAME
    _PROXY_PASSWORD = valid[0].get("password") or _PROXY_PASSWORD
    _proxies_loaded_at = time.time()
    preferred = sum(1 for p in valid if rank(p) != 999)
    logger.info(
        f"Loaded {len(valid)} proxies from Webshare ({preferred} in preferred countries; "
        f"first {_PROXY_SERVERS[0]} {valid[0].get('country_code')})"
    )
    return True


def _maybe_refresh_proxies():
    if not _WEBSHARE_API_KEY:
        return
    if _proxies_loaded_at and time.time() - _proxies_loaded_at < _PROXY_REFRESH_S:
        return
    try:
        _load_proxies_from_webshare()
    except Exception as e:
        logger.warning(
            f"Webshare proxy list unavailable ({e}); using {len(_PROXY_SERVERS)} "
            f"{'previously loaded' if _proxies_loaded_at else 'static PROXY_SERVERS'} proxies"
        )
# Start each worker instance at a different point in the list so a fleet
# scraping one car does not pile onto the same first exit.
import socket as _socket
import zlib as _zlib
_INSTANCE_SEED = _zlib.crc32((os.environ.get("HOSTNAME") or _socket.gethostname() or "").encode())
_proxy_cursor = _INSTANCE_SEED
_current_proxy = None
# Not every exit passes Cloudflare (2026-09-20: Webshare exit #1 fine, #2 challenged
# on every request). Remember blocked ones and skip them for a cooldown.
_PROXY_BLOCK_COOLDOWN_S = int(os.environ.get("PROXY_BLOCK_COOLDOWN_S", "1800"))
_blocked_proxies: dict = {}   # server -> time.time() when last blocked


def _next_proxy():
    global _proxy_cursor, _current_proxy
    _maybe_refresh_proxies()
    if not _PROXY_SERVERS:
        _current_proxy = None
        return None
    now = time.time()
    chosen = None
    for _ in range(len(_PROXY_SERVERS)):
        server = _PROXY_SERVERS[_proxy_cursor % len(_PROXY_SERVERS)]
        _proxy_cursor += 1
        if now - _blocked_proxies.get(server, 0) > _PROXY_BLOCK_COOLDOWN_S:
            chosen = server
            break
    if chosen is None:
        chosen = min(_PROXY_SERVERS, key=lambda s: _blocked_proxies.get(s, 0))
        logger.warning(f"All {len(_PROXY_SERVERS)} proxies recently blocked; retrying {chosen}")
    _current_proxy = chosen
    return {"server": f"http://{chosen}", "username": _PROXY_USERNAME, "password": _PROXY_PASSWORD}


def mark_current_proxy_blocked():
    if _current_proxy:
        _blocked_proxies[_current_proxy] = time.time()
        logger.warning(f"Proxy {_current_proxy} marked blocked for {_PROXY_BLOCK_COOLDOWN_S}s")


class BrowserCrashError(RuntimeError):
    """Raised when the Chromium renderer crashes (OOM or process kill).
    Signals main.py to close and reopen the browser then resume from checkpoint.
    """
    pass


# ------------------------------------------------------------------ #
# Virtual display (Xvfb) - Linux only                                #
# ------------------------------------------------------------------ #

def start_virtual_display():
    global _virtual_display
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=False, size=(1920, 1080))
        _virtual_display.start()
        logger.info("Xvfb virtual display started (1920x1080)")
    except Exception as e:
        logger.info(f"Virtual display not started ({e}) - continuing without it")
        _virtual_display = None


def stop_virtual_display():
    global _virtual_display
    if _virtual_display is not None:
        try:
            _virtual_display.stop()
            logger.info("Virtual display stopped")
        except Exception:
            pass
        _virtual_display = None


# ------------------------------------------------------------------ #
# Browser launch                                                      #
# ------------------------------------------------------------------ #

def launch_browser(playwright_instance) -> tuple:
    proxy = _next_proxy()
    if proxy:
        logger.info(f"Launching browser via proxy {proxy['server']}")
    browser: Browser = playwright_instance.chromium.launch(
        headless=False,
        proxy=proxy,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1920,1080",
            "--start-maximized",
        ],
    )
    context: BrowserContext = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=_USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        java_script_enabled=True,
        accept_downloads=False,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
        },
    )
    # Diagram URLs are read from <img src> in the HTML, never from the bytes, so
    # skipping the downloads loses nothing. It removes most of the per-page
    # transfer (the parts pages are text; the diagrams are the weight), which
    # matters both for speed and for a metered proxy allowance.
    _blocked_hosts = re.compile(
        r"googletagmanager|google-analytics|doubleclick|facebook\.net|adsystem|adservice", re.I
    )

    def _skip_heavy_resources(route):
        req = route.request
        if req.resource_type in ("image", "font", "media") or _blocked_hosts.search(req.url):
            return route.abort()
        return route.continue_()

    context.route("**/*", _skip_heavy_resources)

    page: Page = context.new_page()
    _PlaywrightStealth().apply_stealth_sync(page)
    page.set_default_timeout(45_000)   # 45s cap on ALL page ops incl. page.title()
    logger.info("Browser launched (headed Chrome + stealth)")
    return browser, context, page


# ------------------------------------------------------------------ #
# Human-like helpers                                                  #
# ------------------------------------------------------------------ #

def human_delay(range_tuple: tuple):
    duration = random.uniform(*range_tuple)
    logger.debug(f"Sleeping {duration:.1f}s")
    time.sleep(duration)


def human_move_and_click(page: Page, selector: str):
    from config import ACTION_DELAY
    element = page.locator(selector).first
    box = element.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        y = box["y"] + box["height"] * random.uniform(0.2, 0.8)
        page.mouse.move(x + random.randint(-50, 50), y + random.randint(-30, 30))
        time.sleep(random.uniform(0.1, 0.3))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.click(x, y)
    else:
        element.click()
    human_delay(ACTION_DELAY)


def human_select(page: Page, selector: str, value: str):
    from config import ACTION_DELAY
    element = page.locator(selector).first
    element.focus()
    time.sleep(random.uniform(*ACTION_DELAY))
    element.select_option(value=value)
    time.sleep(random.uniform(*ACTION_DELAY))


def human_scroll(page: Page):
    scroll_amount = random.randint(200, 600)
    page.mouse.wheel(0, scroll_amount)
    time.sleep(random.uniform(0.3, 0.8))


# ------------------------------------------------------------------ #
# Cloudflare handling                                                 #
# ------------------------------------------------------------------ #

def wait_for_no_cloudflare(page: Page, timeout: int = 60):
    start = time.time()
    while True:
        title = page.title()
        if "just a moment" not in title.lower():
            cf_frames = [
                f for f in page.frames
                if "challenges.cloudflare.com" in f.url
            ]
            if not cf_frames:
                return
        elapsed = time.time() - start
        if elapsed > timeout:
            # A challenge that never clears is the exit IP being refused, not a
            # slow page. Raising BrowserCrashError makes main.py relaunch the
            # browser — which rotates to the next (non-blocked) proxy — and
            # resume this car from its checkpoint.
            mark_current_proxy_blocked()
            raise BrowserCrashError(
                f"Cloudflare challenge did not clear within {timeout}s "
                f"on proxy {_current_proxy or 'direct'}"
            )
        logger.warning(
            f"Cloudflare challenge active, waiting... ({elapsed:.0f}s elapsed)"
        )
        time.sleep(2)


# ------------------------------------------------------------------ #
# Ad / popup dismissal                                                #
# ------------------------------------------------------------------ #

_CLOSE_SELECTORS = [
    "button[class*=close]",
    "button[class*=dismiss]",
    "button[aria-label*=Close]",
    "a[class*=close]",
    "div[class*=close-btn]",
    "span[class*=close]",
    "[class*=overlay] button",
    "[class*=modal] button",
    "[class*=popup] button",
]

def dismiss_popups(page):
    try:
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        pass
    for sel in _CLOSE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=300):
                btn.click(timeout=500)
                logger.debug(f"Closed popup: {sel}")
                time.sleep(0.2)
        except Exception:
            pass


# ------------------------------------------------------------------ #
# Safe navigation                                                     #
# ------------------------------------------------------------------ #

def safe_goto(page: Page, url: str, retries: int = 3):
    """
    Navigate to url with retry logic.
    Raises BrowserCrashError immediately if the page/renderer crashes.
    Raises RuntimeError after max retries for other errors.
    """
    from config import PAGE_LOAD_DELAY, RETRY_DELAY, MAX_RETRIES

    max_tries = max(retries, MAX_RETRIES)
    for attempt in range(1, max_tries + 1):
        try:
            logger.debug(f"Navigating to {url} (attempt {attempt})")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=1_000)
            except Exception:
                pass
            wait_for_no_cloudflare(page)
            dismiss_popups(page)
            human_delay(PAGE_LOAD_DELAY)
            return
        except BrowserCrashError:
            raise
        except Exception as e:
            err_str = str(e)
            logger.warning(f"Navigation error (attempt {attempt}): {e}")
            # Crash detected - no point retrying on a dead renderer
            if "crashed" in err_str.lower():
                raise BrowserCrashError(
                    f"Chromium renderer crashed navigating to {url}: {e}"
                )
        if attempt < max_tries:
            logger.info(f"Retrying in {RETRY_DELAY[0]}-{RETRY_DELAY[1]}s ...")
            human_delay(RETRY_DELAY)

    raise RuntimeError(f"Failed to navigate to {url} after {max_tries} attempts")

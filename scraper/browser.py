"""
Browser setup with anti-detection measures.

Launches a headed (visible) Chrome browser using Playwright with:
  - playwright-stealth to mask automation signals
  - Realistic user-agent and viewport
  - Human-like helper functions (delays, mouse movement, scrolling)
  - Cloudflare challenge detection and waiting
  - Safe page navigation with retry logic

Virtual display (Xvfb):
  - start_virtual_display() / stop_virtual_display() wrap pyvirtualdisplay
  - On Linux servers (e.g. Railway) this creates an invisible Xvfb screen so
    the headed browser can run without a physical display
  - On Windows / Mac the functions are silent no-ops
"""

import random
import time
import logging
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright_stealth import Stealth as _PlaywrightStealth

logger = logging.getLogger(__name__)

# Realistic Windows Chrome user-agent
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Module-level virtual display handle
_virtual_display = None


# ------------------------------------------------------------------ #
# Virtual display (Xvfb) — Linux only                                #
# ------------------------------------------------------------------ #

def start_virtual_display():
    """
    Start an Xvfb virtual display so a headed browser can run on a headless
    Linux server (e.g. Railway).  Silent no-op on Windows / Mac or if
    pyvirtualdisplay is not installed.
    """
    global _virtual_display
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=False, size=(1920, 1080))
        _virtual_display.start()
        logger.info("Xvfb virtual display started (1920x1080)")
    except Exception as e:
        logger.info(f"Virtual display not started ({e}) — continuing without it")
        _virtual_display = None


def stop_virtual_display():
    """Stop the virtual display if one was started."""
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
    """
    Launch a headed Chrome browser with stealth settings.
    Returns (browser, context, page).
    """
    browser: Browser = playwright_instance.chromium.launch(
        headless=False,
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

    page: Page = context.new_page()
    _PlaywrightStealth().apply_stealth_sync(page)

    logger.info("Browser launched (headed Chrome + stealth)")
    return browser, context, page


# ------------------------------------------------------------------ #
# Human-like helpers                                                   #
# ------------------------------------------------------------------ #

def human_delay(range_tuple: tuple):
    """Sleep a random number of seconds within range_tuple=(min, max)."""
    duration = random.uniform(*range_tuple)
    logger.debug(f"Sleeping {duration:.1f}s")
    time.sleep(duration)


def human_move_and_click(page: Page, selector: str):
    """
    Locate an element, move the mouse toward it with a slight random offset,
    pause briefly, then click. Simulates natural human mouse movement.
    """
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
    """
    Focus a <select> element, wait briefly, select by value, wait again.
    """
    from config import ACTION_DELAY
    element = page.locator(selector).first
    element.focus()
    time.sleep(random.uniform(*ACTION_DELAY))
    element.select_option(value=value)
    time.sleep(random.uniform(*ACTION_DELAY))


def human_scroll(page: Page):
    """Scroll down a random amount (200-600px) to simulate reading."""
    scroll_amount = random.randint(200, 600)
    page.mouse.wheel(0, scroll_amount)
    time.sleep(random.uniform(0.3, 0.8))


# ------------------------------------------------------------------ #
# Cloudflare handling                                                  #
# ------------------------------------------------------------------ #

def wait_for_no_cloudflare(page: Page, timeout: int = 60):
    """
    Wait until the Cloudflare 'Just a moment' challenge has cleared.
    Polls every 2 seconds. Raises TimeoutError if timeout exceeded.
    """
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
            raise TimeoutError(
                f"Cloudflare challenge did not clear within {timeout}s. "
                "You may need to solve the CAPTCHA manually."
            )
        logger.warning(
            f"Cloudflare challenge active, waiting... ({elapsed:.0f}s elapsed)"
        )
        time.sleep(2)


# ------------------------------------------------------------------ #
# Ad / popup dismissal                                                 #
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
    """Close any ad overlays or popups. Silent on failure."""
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
# Safe navigation                                                      #
# ------------------------------------------------------------------ #

def safe_goto(page: Page, url: str, retries: int = 3):
    """
    Navigate to url with retry logic.
    After load: waits for networkidle, checks for Cloudflare, applies human delay.
    Raises RuntimeError after max retries.
    """
    from config import PAGE_LOAD_DELAY, RETRY_DELAY, MAX_RETRIES

    max_tries = max(retries, MAX_RETRIES)
    for attempt in range(1, max_tries + 1):
        try:
            logger.debug(f"Navigating to {url} (attempt {attempt})")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=4_000)
            except Exception:
                pass
            wait_for_no_cloudflare(page)
            dismiss_popups(page)
            human_delay(PAGE_LOAD_DELAY)
            return
        except Exception as e:
            logger.warning(f"Navigation error (attempt {attempt}): {e}")

        if attempt < max_tries:
            logger.info(f"Retrying in {RETRY_DELAY[0]}-{RETRY_DELAY[1]}s ...")
            human_delay(RETRY_DELAY)

    raise RuntimeError(f"Failed to navigate to {url} after {max_tries} attempts")

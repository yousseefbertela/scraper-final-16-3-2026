import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Scraper identity (1-5); each instance reads its own car list from DB ---
SCRAPER_ID = int(os.environ.get("SCRAPER_ID", "1"))

BASE_URL   = "https://www.realoem.com"
SELECT_URL = f"{BASE_URL}/bmw/enUS/select"

# --- Browser ---
HEADLESS = False          # Always headed; Xvfb virtual display used on Linux servers

# --- Human-like delay ranges (tuned to ~80/1000 safe speed) ---
PAGE_LOAD_DELAY  = (0.4, 0.8)   # After each page navigation
ACTION_DELAY     = (0.2, 0.4)   # Between small UI actions
GROUP_DELAY      = (0.5, 0.9)   # Between main groups
SUBGROUP_DELAY   = (0.2, 0.4)   # Between subgroups
RETRY_DELAY      = (5,  12)     # On error / rate-limit

MAX_RETRIES = 3

# --- Market ---
EGY_ONLY = False          # Multi-market: each scraper targets markets per its car list

# --- Output paths (system temp dir -- nothing written to project folder) ---
import tempfile
DATA_DIR             = os.path.join(tempfile.gettempdir(), "bmw_scraper_data")
VFINAL_NOTES_FILE    = os.path.join(DATA_DIR, "vFinal_notes.json")
CHECKPOINT_FILE      = os.path.join(DATA_DIR, "checkpoint.json")
LOG_FILE             = os.path.join(DATA_DIR, "scraper.log")
PROGRESS_FILE        = os.path.join(DATA_DIR, "scraped_progress.csv")
CAR_LIST_CACHE_FILE  = os.path.join(DATA_DIR, "car_list.json")

# --- DigitalOcean PostgreSQL — read from environment variables only ---
# Set these in DO App Platform env vars (or a local .env file for development).
DO_DB_HOST     = os.environ.get("DO_DB_HOST", "")
DO_DB_PORT     = int(os.environ.get("DO_DB_PORT", "25060"))
DO_DB_NAME     = os.environ.get("DO_DB_NAME", "defaultdb")
DO_DB_USER     = os.environ.get("DO_DB_USER", "doadmin")
DO_DB_PASSWORD = os.environ.get("DO_DB_PASSWORD", "")
DO_DB_SSLMODE  = os.environ.get("DO_DB_SSLMODE", "require")
DO_ADVISORY_LOCK_KEY = 42

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{DO_DB_USER}:{DO_DB_PASSWORD}@{DO_DB_HOST}:{DO_DB_PORT}/{DO_DB_NAME}?sslmode={DO_DB_SSLMODE}"
)

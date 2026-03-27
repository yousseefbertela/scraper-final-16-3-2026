import os
import tempfile

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
PAGE_LOAD_DELAY  = (1.0, 1.5)   # After each page navigation
ACTION_DELAY     = (0.3, 0.6)   # Between small UI actions
GROUP_DELAY      = (1.0, 1.5)   # Between main groups
SUBGROUP_DELAY   = (0.5, 1.0)   # Between subgroups
RETRY_DELAY      = (8,  20)     # On error / rate-limit

MAX_RETRIES = 3

# --- Market ---
EGY_ONLY = False          # Multi-market: each scraper targets markets per its car list

# --- Output paths (system temp dir -- nothing written to project folder) ---
DATA_DIR             = os.path.join(tempfile.gettempdir(), "bmw_scraper_data")
VFINAL_NOTES_FILE    = os.path.join(DATA_DIR, "vFinal_notes.json")
CHECKPOINT_FILE      = os.path.join(DATA_DIR, "checkpoint.json")
LOG_FILE             = os.path.join(DATA_DIR, "scraper.log")
PROGRESS_FILE        = os.path.join(DATA_DIR, "scraped_progress.csv")
CAR_LIST_CACHE_FILE  = os.path.join(DATA_DIR, "car_list.json")

# --- DigitalOcean PostgreSQL (primary storage for 5-scraper architecture) ---
DO_DB_HOST     = "db-postgresql-fra1-49814-do-user-35023198-0.m.db.ondigitalocean.com"
DO_DB_PORT     = 25060
DO_DB_NAME     = "defaultdb"
DO_DB_USER     = "doadmin"
DO_DB_PASSWORD = "AVNS_-XlC2DQ9aUXXALj8pp_"
DO_DB_SSLMODE  = "require"
DO_ADVISORY_LOCK_KEY = 42

# Legacy Railway DATABASE_URL (kept for backward compatibility / frontend)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{DO_DB_USER}:{DO_DB_PASSWORD}@{DO_DB_HOST}:{DO_DB_PORT}/{DO_DB_NAME}?sslmode={DO_DB_SSLMODE}"
)

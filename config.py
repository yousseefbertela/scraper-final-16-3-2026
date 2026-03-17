import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
EGY_ONLY = True           # Only scrape EGY market; skip all others

# --- Output paths ---
DATA_DIR          = "data"
VFINAL_NOTES_FILE = "data/vFinal_notes.json"
CHECKPOINT_FILE   = "data/checkpoint.json"
LOG_FILE          = "data/scraper.log"
PROGRESS_FILE     = "data/scraped_progress.csv"

# --- PostgreSQL (file-mirror storage) ---
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:kfljOWQPmNhIgUeyngUywqHNBreAIrGf@gondola.proxy.rlwy.net:36301/railway"
)

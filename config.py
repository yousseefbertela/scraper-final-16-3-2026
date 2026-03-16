BASE_URL = "https://www.realoem.com"
SELECT_URL = f"{BASE_URL}/bmw/enUS/select"

# --- Sample mode ---
SAMPLE_MODE   = True
SAMPLE_SERIES = "E90"
SAMPLE_MODEL  = "320i"

# --- Browser ---
HEADLESS = False

# --- Human-like delay ranges (seconds) ---
# Tuned: fast enough to be practical, slow enough to avoid rate-limits
PAGE_LOAD_DELAY  = (1.5, 3.0)   # After each page navigation
ACTION_DELAY     = (0.3, 0.8)   # Between small UI actions
GROUP_DELAY      = (1.5, 3.0)   # Between main groups
SUBGROUP_DELAY   = (0.8, 1.8)   # Between subgroups
RETRY_DELAY      = (8,  20)     # On error / rate-limit

MAX_RETRIES = 3

# --- Output paths ---
DATA_DIR        = "data"
NOTES_FILE      = "data/notes.json"
CHECKPOINT_FILE = "data/checkpoint.json"
LOG_FILE        = "data/scraper.log"

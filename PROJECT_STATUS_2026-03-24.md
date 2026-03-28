# BMW RealOEM Scraper — Full Project State
**Date:** 2026-03-24

---

## What This Project Does

Scrapes **BMW parts data** from [realoem.com](https://www.realoem.com) for **Egypt-market (EGY)** vehicles.
Automates a real Chromium browser, enumerates all car dropdowns, navigates groups → subgroups → parts tables,
and stores everything in PostgreSQL. Includes a Node.js frontend to view/search/export the data.
Deployed on **Railway** with always-restart policy.

---

## Scraping Progress

| Prefix | Car | Status |
|--------|-----|--------|
| VA99 | E90 320i (N46) | ✅ Complete |
| VG99 | E90 318i (N46) | ✅ Complete |
| PG59 | E46 316i (N45) | ✅ Complete |
| PG99 | E46 318i (N46) | ✅ Complete |
| PM39 | E36 316i | ✅ Complete |
| 3F54 | (EGY variant) | ✅ Complete |
| 3F55 | (EGY variant) | ✅ Complete |
| 3F56 | (EGY variant) | 🔄 **In Progress** — group 03, 153 subgroups done |

- **~112,000 total parts** in DB so far
- DB file key: `vFinal_notes.json` (65 MB when last exported)
- Local snapshot: `frontend-database/bmw-parts-export (1).json` (63 MB)

---

## Directory Structure

```
scraper-final-16,3,2026/
│
├── main.py                      Entry point — session loop, restart every 4 cars
├── config.py                    All constants: delays, paths, DB URL
├── requirements.txt             Python deps
├── Dockerfile                   Railway container (python:3.13-slim + Xvfb + Chromium)
├── railway.toml                 Railway deploy config (always-restart, 100 retries)
├── .env.example                 DATABASE_URL template
├── .gitignore
│
├── scraper/
│   ├── browser.py               Playwright launch + stealth + Xvfb + human helpers
│   ├── discovery.py             Dropdown enumeration + type_code extraction
│   ├── car_selector.py          Build EGY car list with 4-char prefix dedup
│   ├── filters.py               is_diesel() / select_market() / select_steering()
│   └── parts_scraper.py         groups → subgroups → parts, flush to DB per group
│
├── storage/
│   ├── db.py                    PostgreSQL file-mirror (scraped_files table)
│   ├── notes.py                 NotesWriter — buffer subgroups, flush to DB per group
│   ├── checkpoint.py            CheckpointManager — track done groups/cars in DB
│   └── progress.py              ProgressWriter — audit CSV only, never used for skip logic
│
├── frontend-database/
│   ├── server.js                Express API (port 3000) — 6 endpoints
│   ├── public/
│   │   ├── index.html           SPA shell: sidebar + 4 views
│   │   ├── app.js               Frontend logic: dashboard, catalog, search, target list
│   │   └── style.css            Styling
│   ├── scripts/
│   │   ├── update-3f56.js       Merge 3F56 data from local JSON into DB
│   │   └── upload-car-list.js   Upload egy_cars_only.json → DB as car_list.json
│   └── node_modules/
│
├── egy_cars_only.json           272 KB — cached EGY car reference list
├── prompt-explanation.md        34 KB — detailed project docs
└── PROJECT_STATUS_2026-03-24.md This file
```

---

## Full Architecture

### Data Flow (end to end)

```
Railway startup
    │
    ├─ db.ensure_table()               Create scraped_files table if missing
    ├─ checkpoint._load()              Restore checkpoint.json from DB
    ├─ notes._load()                   Restore vFinal_notes.json from DB
    │
    └─ Session loop (restarts every 4 cars)
           │
           ├─ start_virtual_display()  Xvfb 1920×1080 (Linux only)
           ├─ launch_browser()         Chromium + stealth + 45s timeout
           │
           ├─ _get_remaining_cars()    Determine what to scrape:
           │      ├─ Resume in-progress car from checkpoint (in_progress_group)
           │      ├─ Load car_list.json from DB
           │      └─ Filter out prefixes in checkpoint.get_done_prefixes()
           │
           └─ For each car (max 4 per session):
                  │
                  ├─ parts_scraper.scrape_car_parts()
                  │      │
                  │      ├─ get_main_groups()         /bmw/enUS/partgrp?id=TYPE_CODE
                  │      │
                  │      └─ For each group (mg):
                  │             ├─ skip if checkpoint.is_group_done()
                  │             ├─ checkpoint.set_in_progress(mg)
                  │             ├─ get_subgroups()    /bmw/enUS/partgrp?id=...&mg=XX
                  │             │
                  │             └─ For each subgroup (diagId):
                  │                    ├─ scrape_parts_table()  /bmw/enUS/showparts?...
                  │                    ├─ get_diagram_image_url()
                  │                    └─ notes.save_subgroup()  (buffer in memory)
                  │
                  ├─ notes.flush()                    Write group to DB (sync_file)
                  ├─ checkpoint.mark_group_done()     Save to DB
                  └─ checkpoint.mark_car_done()       Save to DB
```

### PostgreSQL Schema

```sql
CREATE TABLE scraped_files (
    filename   TEXT PRIMARY KEY,   -- e.g. "vFinal_notes.json"
    content    TEXT,               -- full JSON/CSV as string
    updated_at TIMESTAMP DEFAULT NOW()
);
```

**Three files stored:**

| filename | What it is |
|----------|-----------|
| `vFinal_notes.json` | All scraped parts data (~65 MB JSON) |
| `checkpoint.json` | Which cars/groups are done |
| `scraped_progress.csv` | Audit log (started/completed rows) |
| `car_list.json` | Target EGY car list (uploaded once) |

---

## Module-by-Module Breakdown

---

### `main.py` — Entry Point

**Modes:**
- `py -3 main.py` → Full run (all EGY cars)
- `py -3 main.py --sample` → Sample run (E90 320i only, for testing)

**Constants:**
- `BROWSER_RESTART_EVERY = 4` — Restart browser every 4 cars (memory leak prevention)
- `_SAMPLE_CAR` = `VA99-EGY-05-2005-E90-BMW-320i`

**Key function — `_get_remaining_cars(sample_mode, scraped_prefixes, notes, checkpoint)`:**
1. Sample mode: return `[_SAMPLE_CAR]` if not already done
2. Full mode:
   - Check `checkpoint.in_progress_group` → resume that car first
   - Load `car_list.json` from DB
   - Filter out 4-char prefixes in `scraped_prefixes` (from `checkpoint.get_done_prefixes()`)
   - Return up to `BROWSER_RESTART_EVERY` cars

**Session loop in `main()`:**
1. Start Xvfb
2. Launch browser
3. `_get_remaining_cars()` → get batch
4. For each car: call `scrape_car_parts()`, catch `BrowserCrashError` → restart
5. `progress.mark_completed()`, `checkpoint.mark_car_done()`
6. After 4 cars: close browser, loop again
7. Stop only when `_get_remaining_cars()` returns empty list

---

### `config.py` — Constants

```python
BASE_URL    = "https://www.realoem.com"
SELECT_URL  = BASE_URL + "/bmw/enUS/select"
HEADLESS    = False  # Headed always; Xvfb provides display on server

PAGE_LOAD_DELAY  = (1.0, 1.5)   # After page navigation
ACTION_DELAY     = (0.3, 0.6)   # Between UI interactions
GROUP_DELAY      = (1.0, 1.5)   # Between main groups
SUBGROUP_DELAY   = (0.5, 1.0)   # Between subgroups
RETRY_DELAY      = (8, 20)      # On error / rate-limit
MAX_RETRIES      = 3

EGY_ONLY = True

# All paths in system temp dir (Railway ephemeral FS)
DATA_DIR         = tempfile.gettempdir() + "/bmw_scraper_data"
VFINAL_NOTES_FILE = DATA_DIR + "/vFinal_notes.json"
CHECKPOINT_FILE   = DATA_DIR + "/checkpoint.json"
LOG_FILE          = DATA_DIR + "/scraper.log"
PROGRESS_FILE     = DATA_DIR + "/scraped_progress.csv"
CAR_LIST_CACHE_FILE = DATA_DIR + "/car_list.json"

DATABASE_URL = "postgresql://postgres:...@gondola.proxy.rlwy.net:36301/railway"
```

---

### `scraper/browser.py` — Playwright + Anti-Detection

**Virtual Display (Linux/Railway):**
- `start_virtual_display()` → Xvfb 1920×1080, stores in `_display` global
- `stop_virtual_display()` → stops and cleans up

**`launch_browser(playwright_instance)` → (browser, context, page)**
- Chromium with args: `--disable-blink-features=AutomationControlled`, `--no-sandbox`, `--disable-dev-shm-usage`
- `playwright_stealth(context)` — removes automation fingerprints
- Viewport: 1920×1080
- User-Agent: Chrome 124 on Windows 10
- Timezone: America/New_York, Locale: en-US
- Default timeout: **45 seconds** (prevents infinite hang on page.title())

**Human-like helpers:**
- `human_delay(range_tuple)` — `time.sleep(random.uniform(a, b))`
- `human_move_and_click(page, selector)` — Move mouse with offset + random jitter, then click
- `human_select(page, selector, value)` — Focus element, delay, select value
- `human_scroll(page)` — Random wheel scroll 200–600px

**Cloudflare + Popup handling:**
- `wait_for_no_cloudflare(page, timeout=60)` — Polls title + frame count
- `dismiss_popups(page)` — Tries Esc + multiple CSS selectors for close buttons

**Safe navigation:**
- `safe_goto(page, url, retries=3)` — Navigate with retries
- Immediately raises `BrowserCrashError` on "Target page, context or browser has been closed"
- Waits for `domcontentloaded` + `networkidle` + no Cloudflare

**Custom exception:**
- `class BrowserCrashError(RuntimeError)` — signals main.py to restart browser

---

### `scraper/discovery.py` — Dropdown Enumeration

**Critical fix (2026-03-16):**
RealOEM's "Browse Parts" is a `<form action='/bmw/enUS/partgrp'>` with a hidden `<input value='TYPE_CODE_FULL'>` — NOT a link. `_extract_type_code()` now checks form hidden inputs first, falls back to `<a href>` tags.

**Internal helpers:**
- `_nav(page, **params)` → Navigate to SELECT_URL with query params, return BeautifulSoup
- `_read_select(soup, name)` → Extract `[{value, label}]` from `<select name=name>`, skip blanks
- `_extract_type_code(soup)` → form hidden input first → `<a href>` fallback
- `_ajax_get_type_code(page, series, body, model, market, prod, engine)` → Step-by-step form fill (last resort)

**Public enumeration functions:**
```
get_all_series(page)                        → [{value, label}, ...]
get_bodies(page, series)                    → [{value, label}, ...]
get_models(page, series, body)              → [{value, label}, ...]
get_markets(page, series, body, model)      → [market_code, ...]
get_prods(page, series, body, model, mkt)   → ["200805", "200901", ...]  (YYYYMM)
get_engines(page, ..., prod)                → ["N46", "M43", ...]
get_type_code_full(page, ...)               → {type_code_full, steering} or None
```

**`get_type_code_full()` logic:**
1. URL-param approach (`_nav()` with all params)
2. If steering dropdown present → select LHD → retry
3. If still no type_code → `_ajax_get_type_code()` step-by-step fallback

**TYPE_CODE_FULL format:** `VA99-EGY-05-2005-E90-BMW-320i`
- 4-char prefix = `VA99`
- Market = `EGY`
- Prod month/year
- Series, Make, Model

---

### `scraper/car_selector.py` — Build EGY Car List

**`build_car_list(page, sample_mode=False, scraped_prefixes=None)` → generator of car dicts**

Each car dict:
```python
{
    "series_value": "E90",
    "series_label": "3' E90",
    "body": "Lim",
    "model": "320i",
    "market": "EGY",
    "prod_month": "200805",
    "engine": "N46",
    "steering": "Left hand drive",
    "type_code_full": "VA99-EGY-05-2005-E90-BMW-320i"
}
```

**Enumeration logic:**
```
For each series:
  Skip if sample_mode and series != "E90"
  For each body:
    For each model:
      Skip if is_diesel(model)
      Check if "EGY" in get_markets() → skip if not
      For each prod_month in get_prods():         ← ALL prod dates
        For each engine in get_engines():          ← ALL engines
          type_code = get_type_code_full()
          Skip if type_code[:4] in scraped_prefixes  ← 4-char dedup
          Add type_code[:4] to scraped_prefixes
          Yield car dict
```

---

### `scraper/filters.py` — Pure Filter Functions

**`is_diesel(model_name: str) → bool`**
- Regex: matches models ending in `d`, `xd`, `td`, `d ed`
- Examples: `316d` ✅, `320xd` ✅, `318td` ✅, `320d ed` ✅
- Not diesel: `316i` ❌, `320e` ❌, `M3` ❌

**`select_market(available_markets) → str | None`**
- Priority: EGY → EUR → None

**`select_steering(available_steerings) → str | None`**
- Prefers "Left hand drive"
- Falls back to `available_steerings[0]` if LHD not in list
- Returns None if list is empty

---

### `scraper/parts_scraper.py` — Core Scraping Logic

**`get_main_groups(page, type_code_full)` → [{mg, name}, ...]**
- URL: `/bmw/enUS/partgrp?id={type_code_full}`
- Scrolls page, parses `<a href>` with `?mg=\d+`
- Deduplicates by `mg` value

**`get_subgroups(page, type_code_full, mg)` → [{diagId, name}, ...]**
- URL: `/bmw/enUS/partgrp?id={type_code_full}&mg={mg}`
- Parses `<a>` tags containing `showparts` + `diagId=`
- Deduplicates by `diagId`

**`get_diagram_image_url(page, type_code_full, diag_id)` → URL string**
- URL: `/bmw/enUS/showparts?id={type_code_full}&diagId={diag_id}`
- Finds `<img>` where src contains `diag_id`
- Fallback: first non-logo/icon img
- Returns absolute URL via `urljoin(BASE_URL, src)`

**`scrape_parts_table(page, type_code_full, diag_id)` → [part_dict, ...]**
- Finds parts `<table>` by checking header row for "part number" / "no." / "description"
- Dynamically maps column indices from header
- Per-row extraction: `ref_no, description, supplier, qty, from_date, to_date, part_number, price, notes, detail_url`
- Skips rows with no part_number

**`scrape_car_parts(page, car, notes_writer, checkpoint_manager)` → int (total parts)**
```
get_main_groups()
For each group:
    Skip if checkpoint.is_group_done()
    checkpoint.set_in_progress(group)
    get_subgroups()
    For each subgroup:
        human_delay(SUBGROUP_DELAY)
        scrape_parts_table()    → parts[]
        get_diagram_image_url() → diagram_url
        notes.save_subgroup()   → buffer in memory
        (errors logged + stored with scrape_error field)
    notes.flush()               → write group to DB
    checkpoint.mark_group_done()
    human_delay(GROUP_DELAY)
checkpoint.mark_car_done()
return total_parts
```

---

### `storage/db.py` — PostgreSQL File Mirror

**Table:** `scraped_files(filename TEXT PK, content TEXT, updated_at TIMESTAMP)`

**Functions:**
```python
ensure_table()                           # CREATE TABLE IF NOT EXISTS, sets _DB_AVAILABLE flag
sync_file(filename, content)             # INSERT ... ON CONFLICT DO UPDATE
sync_file_from_path(filepath)            # Read local file → sync_file()
restore_file_to_path(filename, filepath) # DB → write to local path, returns bool
get_file_content(filename)               # SELECT content, returns str or None
```

---

### `storage/notes.py` — Parts Data Writer

**Class `NotesWriter`:**

**JSON structure in DB:**
```json
{
  "meta": { "created_at": "...", "last_updated": "...", "version": "1.0" },
  "data": {
    "E90": {
      "series_label": "3' E90",
      "models": {
        "VA99-EGY-05-2005-E90-BMW-320i": {
          "series_value": "E90", "body": "Lim", "model": "320i",
          "market": "EGY", "prod_month": "200805", "engine": "N46",
          "steering": "Left hand drive", "type_code_full": "VA99-...",
          "groups": {
            "01": {
              "group_name": "ENGINE",
              "subgroups": {
                "03_2479": {
                  "subgroup_name": "SHORT ENGINE",
                  "diagram_image_url": "https://...",
                  "scraped_at": "2026-03-16T10:30:00",
                  "parts": [ { "ref_no": "1", "part_number": "11001...", ... } ]
                }
              }
            }
          }
        }
      }
    }
  }
}
```

**Methods:**
- `__init__(filepath)` — Load from `db.get_file_content("vFinal_notes.json")`
- `save_subgroup(car, group, subgroup, diagram_url, parts, error=None)` — Buffer in `self._data` (no DB write)
- `flush()` — Call `_write_to_db()` → `db.sync_file("vFinal_notes.json", json_str)`
- `get_car_dict(type_code_full)` → metadata dict for resume (finds car across series)
- `_load()` — Load from DB, init fresh `{"meta": ..., "data": {}}` if not found

**Write pattern:** Buffer all subgroups in memory → `flush()` once per group → single DB write per group.

---

### `storage/checkpoint.py` — Progress Tracker

**JSON structure in DB:**
```json
{
  "last_updated": "2026-03-21T14:00:00",
  "cars": {
    "VA99-EGY-05-2005-E90-BMW-320i": {
      "completed": true,
      "completed_groups": ["01", "02", "03", ...],
      "completed_subgroups": { "03": ["03_2479", "03_2480"] },
      "in_progress_group": null
    }
  }
}
```

**Class `CheckpointManager`:**

**Methods:**
```python
is_car_done(type_code_full)             → bool
is_group_done(type_code_full, mg)       → bool
is_subgroup_done(type_code, mg, diagId) → bool
set_in_progress(car, mg)                → saves to DB
mark_group_done(car, mg)                → moves mg to completed_groups, clears in_progress, saves
mark_car_done(car)                      → sets completed=True, saves
get_done_prefixes()                     → set of 4-char prefixes (e.g. {"VA99","VG99",...})
```

**Critical rule:** `main.py` uses `checkpoint.get_done_prefixes()` ONLY — not progress.csv.
This was the root cause of the 2026-03-21 bug where a Railway crash between `progress.mark_completed()` and `checkpoint.mark_car_done()` caused 3F56 to be falsely skipped.

---

### `storage/progress.py` — Audit Log Only

**CSV format:** `type_code, prefix, status, timestamp, parts_count`

**Class `ProgressWriter`:**
- `mark_started(type_code)` — append row with `status="started"`
- `mark_completed(type_code, parts_count)` — append row with `status="completed"`
- Stored in DB as `scraped_progress.csv`

**⚠️ NOT used for skip logic.** Checkpoint is the only source of truth for resume decisions.

---

### `frontend-database/server.js` — Express API

**Port:** 3000
**Cache:** In-memory 60-second TTL for notes data

**6 API endpoints:**

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/overview` | Total stats + per-car summary with checkpoint status |
| GET | `/api/cars/:typeCode` | Car metadata + groups list with part counts |
| GET | `/api/cars/:typeCode/groups/:groupId` | Group + subgroups + parts + diagram URLs |
| GET | `/api/search?q=...` | Full-text search, max 100 results |
| GET | `/api/target-list` | All EGY targets with scraped/unscraped status |
| GET | `/api/export` | Downloads `vFinal_notes.json` from DB as attachment |

**`/api/export` — modified (uncommitted):**
- **Before:** Used in-memory cache + `JSON.stringify(data, null, 2)`
- **After:** `SELECT content FROM scraped_files WHERE filename = 'vFinal_notes.json'` → send raw string
- Faster, bypasses 60s cache, no re-serialization overhead

**Overview response shape:**
```json
{
  "total_cars": 8,
  "total_parts": 112000,
  "total_groups": 280,
  "total_subgroups": 14000,
  "checkpoint_updated": "2026-03-21T...",
  "cars": [{ "type_code": "...", "model": "...", "parts_count": 14029, "completed": true, ... }]
}
```

---

### `frontend-database/public/app.js` — Frontend SPA

**4 views:** Dashboard, Catalog, Search, Target List

**State:** `{ view, currentCar, currentGroup, overviewData }`

**Key interactions:**
- Dashboard → click car → Groups view → click group → Parts view (drill-down)
- Search: debounced 400ms, calls `/api/search`
- Export: fetches `/api/export`, downloads as blob with `<a download>`
- Target List: shows scraped vs unscraped per 4-char prefix

**Modal:** Click diagram image → fullscreen overlay

---

### `frontend-database/scripts/`

**`update-3f56.js`:**
1. Read local `bmw-parts-export (1).json`
2. Extract 3F56 car entry
3. Fetch current `vFinal_notes.json` from DB
4. Overwrite only 3F56 car entry in the DB copy
5. Write back to DB

**`upload-car-list.js`:**
- `node scripts/upload-car-list.js <path/to/egy_cars_only.json>`
- Reads JSON file → upserts to DB as `car_list.json`

---

## Business Rules

| Rule | Implementation |
|------|---------------|
| EGY market only | `car_selector.py`: skip model if "EGY" not in `get_markets()` |
| Skip diesel | `filters.is_diesel()` regex check on model name |
| ALL production dates | `car_selector.py`: iterate all `get_prods()` results |
| ALL engines per prod | `car_selector.py`: iterate all `get_engines()` results |
| LHD preferred | `filters.select_steering()`: "Left hand drive" > first available |
| 4-char prefix dedup | `car_selector.py`: mutate `scraped_prefixes` set |
| Cars only (no motos) | Series enumeration naturally skips motorcycle series |
| Save after every group | `parts_scraper.py`: `notes.flush()` after each group loop |
| Never lose data on crash | Checkpoint + notes saved to DB independently and atomically |

---

## Deployment

```toml
# railway.toml
[deploy]
startCommand = "python main.py"
restartPolicyType = "ALWAYS"
restartPolicyMaxRetries = 100
```

**On every Railway restart:**
1. `db.ensure_table()` — verify DB connection
2. `checkpoint._load()` — restore checkpoint.json from DB
3. `notes._load()` — restore vFinal_notes.json from DB
4. `_get_remaining_cars()` — figure out where we left off
5. Resume from `in_progress_group` if any

**No local files are committed.** All data lives in PostgreSQL. Local paths (in `tempfile.gettempdir()`) are ephemeral and always restored from DB.

---

## Git Status (2026-03-24)

**Branch:** main (up to date with origin)

**Modified (uncommitted):**
- `frontend-database/server.js` — `/api/export` queries DB directly

**Untracked:**
- `PROJECT_STATUS_2026-03-24.md` — this file

**Last 10 commits:**
```
1a93f26  Track scrape errors per subgroup in DB
63d566e  Fix: add 45s default timeout to prevent page.title() infinite hang
151c8d5  Fix: log group save at INFO level so it appears in Railway logs
b118ddd  Add frontend DB viewer + DB-only scraper refactor
b987e1f  Save after every group instead of every subgroup; wipe 3F56 for fresh scrape
531a5ec  Refactor: all data goes to system temp dir, read directly from PostgreSQL
8c333d0  Fix: browser launch crash + unlimited restart policy
f886632  Fix: scraper stops early + decouple checkpoint from progress tracking
72b28d5  Fix scraper stopping after Phase 1 car instead of continuing
343e307  Fix OOM crash recovery and add exact-subgroup resume
```

---

## Critical Bug Fix History

### 2026-03-16 — `_extract_type_code()` form fix
- **Problem:** Code searched `<a href>` for type_code. RealOEM uses `<form>` + hidden `<input value='TYPE_CODE'>` — NOT a link.
- **Fix:** Check `form[action*='partgrp'] input[type=hidden]` first, fallback to links.

### 2026-03-21 — commit f886632 (3 fixes)
1. **Fix 1:** `scraped_prefixes = checkpoint.get_done_prefixes()` ONLY (removed progress.csv from this)
   - Root cause: Railway crashed between `progress.mark_completed()` and `checkpoint.mark_car_done()`, so progress said 3F56 done but checkpoint did not → skipped 3F56 → 0 cars found → false "all done"
2. **Fix 2:** Phase 2 cache returning 0 cars → delete cache + force fresh RealOEM enumeration next session
3. **Fix 3:** For-loop finishing with <4 cars → force `need_restart=True` instead of exiting

### 2026-03-21 — commit 63d566e
- Added 45-second default timeout to Playwright context
- Prevents `page.title()` from hanging indefinitely on a crashed/stale page

### 2026-03-21 — commit 1a93f26
- When `scrape_parts_table()` raises an exception, store `scrape_error` field in subgroup dict
- Allows identifying which subgroups failed without losing progress

---

## Open Items

- [ ] **3F56 still in progress** — needs Railway to continue or manual resume
- [ ] **`server.js` export change uncommitted** — commit when ready to deploy
- [ ] **Unknown remaining cars after 3F56** — scraper will enumerate fresh from RealOEM when 3F56 finishes
- [ ] **Frontend has no authentication** — publicly accessible if the Railway URL is known
- [ ] **`egy_cars_only.json` not in gitignore** — 272 KB committed to repo (see .gitignore — it actually IS ignored)

---

## Quick Reference — Run Commands

```bash
# Local test (sample car only)
py -3 main.py --sample

# Full run (all EGY cars)
py -3 main.py

# Start frontend
cd frontend-database && node server.js

# Upload car list to DB
node frontend-database/scripts/upload-car-list.js egy_cars_only.json

# Update 3F56 in DB from local JSON
node frontend-database/scripts/update-3f56.js
```

---

## Stable Restore Point

```bash
git checkout de2ac0c   # Full EGY scraper redesign + Railway deployment (2026-03-17)
```

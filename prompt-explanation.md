# BMW RealOEM Parts Scraper — Full Rebuild Prompt

You are an AI agent. Your job is to build a complete, production-ready BMW parts scraper and catalog system from scratch. Read every word of this document before writing a single line of code.

---

## What You Are Building

Two separate services:

1. **Scraper** — A Python/Playwright bot that visits `https://www.realoem.com`, navigates BMW car dropdowns, and scrapes every part for every EGY-market BMW car. It runs 24/7 on a Linux Docker container (long-running process).

2. **Frontend Catalog** — A Next.js web app (React) that reads scraped data from Appwrite Database and displays it in a clean dark-theme UI. Users can browse vehicles, groups, subgroups, parts, and search.

**Database: Appwrite Databases (NoSQL).** No PostgreSQL. No Railway. Everything lives in Appwrite.

---

## CRITICAL: Hosting Constraints

### Scraper
Appwrite Functions have a 15-minute execution limit and cannot run Playwright/Chromium. **The scraper CANNOT run as an Appwrite Function.**

Run the scraper as a **Docker container on any Linux VPS** (DigitalOcean, Linode, Hetzner, etc.) that supports long-running Docker processes. The container connects to Appwrite via the Appwrite Python SDK.

### Frontend
Deploy the Next.js app anywhere (Vercel, Netlify, or Appwrite's own static hosting via Functions). It connects to Appwrite Databases via the Appwrite JS SDK.

---

## Step 0: Data Migration (Do This First)

You will receive a file called `bmw-parts-export.json`. This file contains all scraped data from 8 complete BMW cars.

**Your first job** is to write a standalone migration script (`migrate.py`) that:

1. Reads `bmw-parts-export.json`
2. Creates all necessary Appwrite collections (see schema below)
3. Inserts all cars, groups, subgroups, and parts into Appwrite
4. Marks all 8 cars as `completed = true` in the checkpoint collection
5. Deletes `bmw-parts-export.json` from disk
6. Prints `"Migration complete. You can now test the frontend."`

Run `python migrate.py` once after setting up Appwrite credentials. The agent must not proceed until the user confirms migration succeeded.

---

## Appwrite Database Schema

### Database name: `bmw_parts`

Create these 6 collections:

---

### Collection: `cars`
One document per BMW car variant.

| Attribute | Type | Notes |
|-----------|------|-------|
| `type_code_full` | String (255) | e.g. `VA99-EGY-05-2005-E90-BMW-320i` — unique identifier |
| `prefix` | String (4) | First 4 chars of type_code_full e.g. `VA99` |
| `series_value` | String (20) | e.g. `E90` |
| `series_label` | String (100) | e.g. `3' E90 (2004-2012)` |
| `body` | String (50) | e.g. `Lim` |
| `model` | String (50) | e.g. `320i` |
| `market` | String (10) | Always `EGY` |
| `prod_month` | String (20) | e.g. `20050500` |
| `engine` | String (50) | e.g. `N46` |
| `steering` | String (50) | e.g. `Left hand drive` |

Index: `type_code_full` (unique), `prefix`, `series_value`

---

### Collection: `groups`
One document per main group per car.

| Attribute | Type | Notes |
|-----------|------|-------|
| `car_type_code` | String (255) | Foreign key → cars.type_code_full |
| `group_id` | String (10) | e.g. `11`, `12`, `21` |
| `group_name` | String (255) | e.g. `ENGINE` |

Index: `car_type_code`, compound (`car_type_code` + `group_id`) unique

---

### Collection: `subgroups`
One document per subgroup.

| Attribute | Type | Notes |
|-----------|------|-------|
| `car_type_code` | String (255) | |
| `group_id` | String (10) | |
| `diag_id` | String (50) | e.g. `11_3171` — unique per car |
| `subgroup_name` | String (255) | |
| `diagram_image_url` | String (1000) | |
| `scraped_at` | String (50) | ISO timestamp |

Index: `car_type_code`, `diag_id`, compound (`car_type_code` + `diag_id`) unique

---

### Collection: `parts`
One document per part row. This will have ~112,000+ documents.

| Attribute | Type | Notes |
|-----------|------|-------|
| `car_type_code` | String (255) | |
| `diag_id` | String (50) | Foreign key → subgroups.diag_id |
| `ref_no` | String (20) | Reference number |
| `description` | String (500) | Part description |
| `supplier` | String (100) | |
| `qty` | String (20) | |
| `from_date` | String (20) | |
| `to_date` | String (20) | |
| `part_number` | String (100) | BMW part number |
| `price` | String (50) | |
| `notes` | String (500) | |
| `detail_url` | String (1000) | |

Index: `car_type_code`, `diag_id`, `part_number`

---

### Collection: `checkpoint`
One document per car, tracks scraping progress.

| Attribute | Type | Notes |
|-----------|------|-------|
| `type_code_full` | String (255) | Unique |
| `completed` | Boolean | True when all groups scraped |
| `completed_groups` | String[] | List of group_ids done |
| `in_progress_group` | String (10) | Currently being scraped |
| `last_updated` | String (50) | ISO timestamp |

Index: `type_code_full` (unique), `completed`

---

### Collection: `scrape_log`
Audit log for scrape events.

| Attribute | Type | Notes |
|-----------|------|-------|
| `type_code` | String (255) | |
| `prefix` | String (4) | |
| `status` | String (20) | `started` or `completed` |
| `timestamp` | String (50) | ISO timestamp |
| `parts_count` | Integer | 0 if started |

Index: `type_code`, `timestamp`

---

## Scraper Architecture

### File structure
```
scraper/
├── main.py              # Entry point, session loop
├── config.py            # All constants + Appwrite credentials
├── requirements.txt
├── Dockerfile
├── scraper/
│   ├── __init__.py
│   ├── browser.py       # Playwright launch + stealth + helpers
│   ├── discovery.py     # RealOEM dropdown navigation
│   ├── car_selector.py  # Build car list from RealOEM
│   ├── filters.py       # Diesel filter, market/steering selection
│   └── parts_scraper.py # Scrape groups→subgroups→parts
└── storage/
    ├── __init__.py
    ├── appwrite_db.py   # Appwrite SDK wrapper (replaces old db.py)
    ├── notes.py         # In-memory data tree + flush to Appwrite
    ├── checkpoint.py    # Resume tracker using Appwrite checkpoint collection
    └── progress.py      # Audit log using Appwrite scrape_log collection
```

---

### config.py

```python
import os
from dotenv import load_dotenv
load_dotenv()

BASE_URL   = "https://www.realoem.com"
SELECT_URL = f"{BASE_URL}/bmw/enUS/select"

HEADLESS = False  # Always headed; Xvfb handles display on Linux

# Human-like delay ranges (tuned to avoid rate-limiting)
PAGE_LOAD_DELAY  = (1.0, 1.5)
ACTION_DELAY     = (0.3, 0.6)
GROUP_DELAY      = (1.0, 1.5)
SUBGROUP_DELAY   = (0.5, 1.0)
RETRY_DELAY      = (8, 20)
MAX_RETRIES      = 3

EGY_ONLY = True

# Appwrite
APPWRITE_ENDPOINT   = os.getenv("APPWRITE_ENDPOINT", "https://fra.cloud.appwrite.io/v1")
APPWRITE_PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID", "69be7ac700290e5d31c9")
APPWRITE_API_KEY    = os.getenv("APPWRITE_API_KEY", "standard_8629b847a5bdb0a83235c54eb9803cdc491e019bb23991ba898c9df48fcc88a7d43e9821e43f30733c9eccad91fc5d204cbc38c6ab1ae011cda7df29b1567675d2a979b29a924498c4db6d4688e17bf600c0a236d1027c59df8bc849b7f9c22ebfacabd31f7625e4a847cd4acf9f0c55d0fe8c54438b9d358022b9fea370c38b")
APPWRITE_DB_ID      = os.getenv("APPWRITE_DB_ID", "69be7ad40015bfb6fe70")

# Collection IDs (set these after creating collections in Appwrite console)
COL_CARS      = "cars"
COL_GROUPS    = "groups"
COL_SUBGROUPS = "subgroups"
COL_PARTS     = "parts"
COL_CHECKPOINT = "checkpoint"
COL_SCRAPE_LOG = "scrape_log"
```

---

### storage/appwrite_db.py

This replaces `storage/db.py`. It wraps the Appwrite Python SDK.

```python
from appwrite.client import Client
from appwrite.services.databases import Databases
from appwrite.id import ID
from appwrite.query import Query
from config import (APPWRITE_ENDPOINT, APPWRITE_PROJECT_ID, APPWRITE_API_KEY,
                    APPWRITE_DB_ID, COL_CARS, COL_GROUPS, COL_SUBGROUPS,
                    COL_PARTS, COL_CHECKPOINT, COL_SCRAPE_LOG)

def get_client():
    client = Client()
    client.set_endpoint(APPWRITE_ENDPOINT)
    client.set_project(APPWRITE_PROJECT_ID)
    client.set_key(APPWRITE_API_KEY)
    return client

def get_db():
    return Databases(get_client())
```

Implement these functions in `appwrite_db.py`:

- `upsert_car(car_dict)` — create or update a car document
- `upsert_group(car_type_code, group_id, group_name)` — create or update
- `upsert_subgroup(car_type_code, group_id, diag_id, name, diagram_url, scraped_at)` — create or update
- `insert_parts_batch(car_type_code, diag_id, parts_list)` — delete existing parts for this diag_id then insert all new ones in batches of 100
- `get_checkpoint(type_code_full)` → dict or None
- `upsert_checkpoint(type_code_full, completed, completed_groups, in_progress_group)`
- `get_all_completed_prefixes()` → set of 4-char strings
- `log_scrape_event(type_code, status, parts_count=0)`
- `get_all_cars()` → list of car dicts (for frontend)
- `get_car(type_code_full)` → car dict
- `get_groups_for_car(type_code_full)` → list
- `get_subgroups_for_group(car_type_code, group_id)` → list
- `get_parts_for_subgroup(car_type_code, diag_id)` → list
- `search_parts(query_string, limit=100)` → list of matching parts with car/group/subgroup context

Use Appwrite's `Query.equal()`, `Query.limit()`, `Query.offset()` for filtering.

**Important**: Appwrite list queries return max 25 documents by default. Always paginate with `Query.limit(100)` and loop until all documents are fetched for large collections.

---

### storage/notes.py

Keep the same in-memory tree structure as before but replace disk writes with Appwrite calls.

```python
class NotesWriter:
    def __init__(self):
        self.data = {}  # in-memory: {type_code: {groups: {mg: {subgroups: {diag_id: {...}}}}}}

    def save_subgroup(self, car, group, subgroup, diagram_url, parts):
        """Buffer subgroup data in memory only."""
        # Update self.data tree (same structure as before)

    def flush(self, car, group_id):
        """
        After a group is done, write to Appwrite:
        1. upsert_car(car)
        2. upsert_group(car_type_code, group_id, group_name)
        3. For each subgroup: upsert_subgroup(...) + insert_parts_batch(...)
        Clear the group from self.data after flush.
        """
```

---

### storage/checkpoint.py

```python
class CheckpointManager:
    def __init__(self):
        pass  # No file - reads/writes directly from Appwrite

    def is_car_done(self, type_code_full) -> bool:
        cp = get_checkpoint(type_code_full)
        return cp and cp.get("completed", False)

    def is_group_done(self, type_code_full, mg) -> bool:
        cp = get_checkpoint(type_code_full)
        return cp and mg in (cp.get("completed_groups") or [])

    def set_in_progress(self, car, mg):
        cp = get_checkpoint(car["type_code_full"]) or {}
        upsert_checkpoint(
            car["type_code_full"],
            completed=False,
            completed_groups=cp.get("completed_groups", []),
            in_progress_group=mg
        )

    def mark_group_done(self, car, mg):
        cp = get_checkpoint(car["type_code_full"]) or {}
        done = list(cp.get("completed_groups") or [])
        if mg not in done:
            done.append(mg)
        upsert_checkpoint(
            car["type_code_full"],
            completed=False,
            completed_groups=done,
            in_progress_group=None
        )

    def mark_car_done(self, car):
        cp = get_checkpoint(car["type_code_full"]) or {}
        upsert_checkpoint(
            car["type_code_full"],
            completed=True,
            completed_groups=cp.get("completed_groups", []),
            in_progress_group=None
        )

    def get_done_prefixes(self) -> set:
        """ONLY source of truth for which cars are done. Never use scrape_log for this."""
        return get_all_completed_prefixes()
```

---

### main.py session loop

The main loop logic must be preserved exactly:

```
BROWSER_RESTART_EVERY = 4  # Restart Chrome every 4 cars to prevent OOM

while True:
    session += 1
    scraped_prefixes = checkpoint.get_done_prefixes()  # ONLY from checkpoint, never from scrape_log

    with sync_playwright() as p:
        browser = None
        try:
            browser, context, page = launch_browser(p)
            cars, needs_followup = _get_cars_for_session(page, sample_mode, scraped_prefixes, notes, checkpoint)

            if not cars and not needs_followup:
                log("All EGY cars scraped! Done.")
                break

            for car in cars:
                if checkpoint.is_car_done(car["type_code_full"]):
                    continue
                progress.mark_started(car["type_code_full"])
                try:
                    parts_count = scrape_car_parts(page, car, notes, checkpoint)
                    progress.mark_completed(car["type_code_full"], parts_count)
                except BrowserCrashError:
                    need_restart = True
                    break
                except Exception as e:
                    log_error(e)

                cars_this_session += 1
                if cars_this_session >= BROWSER_RESTART_EVERY:
                    need_restart = True
                    break

            if needs_followup and not need_restart:
                need_restart = True

        finally:
            if browser is not None:
                browser.close()

    gc.collect()

    if interrupted:
        break

    if not need_restart:
        need_restart = True  # Always restart to check for new EGY cars
        # Scraper stops ONLY when _get_cars_for_session returns ([], False)
        # after a fresh RealOEM enumeration that finds zero remaining cars
```

---

### _get_cars_for_session logic

```
Phase 1 (resume) — check checkpoint for in-progress cars:
    For each car in checkpoint where completed=False:
        Get car metadata from Appwrite cars collection
        Add to resume list
    If resume list not empty → return (resume_list, needs_followup=True)

Phase 2 (discover) — load car list cache or enumerate RealOEM:
    Check if car_list cache exists in Appwrite (store as a document in a 'cache' collection or a single document)
    If cache exists:
        Filter out prefixes already in scraped_prefixes
        If remaining is empty:
            Delete cache document
            Return ([], needs_followup=True) ← force fresh RealOEM enumeration next session
        Return (remaining, needs_followup=False)
    If no cache:
        Run build_car_list(page) to enumerate all EGY cars from RealOEM
        Save full list to Appwrite cache
        Filter and return (remaining, needs_followup=False)
```

---

### scraper/parts_scraper.py — scrape_car_parts()

```
def scrape_car_parts(page, car, notes_writer, checkpoint_manager):
    groups = get_main_groups(page, type_code)
    total_parts = 0

    for group in groups:
        mg = group["mg"]
        if checkpoint_manager.is_group_done(type_code, mg):
            continue  # skip already scraped groups

        checkpoint_manager.set_in_progress(car, mg)
        subgroups = get_subgroups(page, type_code, mg)

        for subgroup in subgroups:
            parts = scrape_parts_table(page, type_code, subgroup["diagId"])
            diagram_url = get_diagram_image_url(page, type_code, subgroup["diagId"])
            notes_writer.save_subgroup(car, group, subgroup, diagram_url, parts)
            total_parts += len(parts)

        notes_writer.flush(car, mg)         # Write to Appwrite after every group
        checkpoint_manager.mark_group_done(car, mg)   # Mark group done in Appwrite

    checkpoint_manager.mark_car_done(car)
    return total_parts
```

**Critical rule**: Data is written to Appwrite after every complete group, not every subgroup. This minimizes API calls while ensuring no data is lost if the process crashes.

---

### scraper/browser.py

Keep exactly the same as the original:
- `launch_browser()` — headed Chrome with stealth + anti-detection headers
- `start_virtual_display()` / `stop_virtual_display()` — Xvfb for Linux headless servers
- `wait_for_no_cloudflare()` — polls until Cloudflare challenge clears
- `dismiss_popups()` — tries common close-button selectors + ESC key
- `safe_goto()` — retries up to 3 times, raises `BrowserCrashError` if renderer crashes
- `human_delay()`, `human_move_and_click()`, `human_select()`, `human_scroll()` — random human-like timing

Chrome launch args must include:
```
--disable-blink-features=AutomationControlled
--no-sandbox
--disable-dev-shm-usage
--disable-infobars
--window-size=1920,1080
--start-maximized
```

Context must include:
```
user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36..."
locale: "en-US"
timezone_id: "America/New_York"
Accept-Language: "en-US,en;q=0.9"
```

Apply `playwright-stealth` to every new page.

---

### scraper/discovery.py — RealOEM navigation

RealOEM's select page: `https://www.realoem.com/bmw/enUS/select`

**Critical behavior**: RealOEM renders each dropdown as a `<select>` tag. When you navigate with URL params (`?product=P&series=E90&body=...`), the server pre-populates subsequent dropdowns. BUT the "Browse Parts" button only appears as a `<form action='/bmw/enUS/partgrp'>` with a hidden `<input type='hidden' value='TYPE_CODE_FULL'>` — it is NOT an `<a>` link.

Functions needed:
- `get_all_series(page)` → list of `{value, label}`
- `get_bodies(page, series)` → list
- `get_models(page, series, body)` → list
- `get_markets(page, series, body, model)` → list of market strings
- `get_prods(page, series, body, model, market)` → list of `YYYYMM00` strings
- `get_engines(page, series, body, model, market, prod)` → list
- `get_type_code_full(page, series, body, model, market, prod, engine)` → `{type_code_full, steering}` or None

`_extract_type_code(soup)`:
1. First check: find `<form action='...partgrp...'>`, look for `<input type='hidden'>` with a value that has 4+ hyphens → that's the type code
2. Fallback: scan `<a href>` tags for `?id=` param with 4+ hyphens

`_ajax_get_type_code()` fallback:
- Navigate step-by-step using `page.expect_navigation()` for each dropdown (each selection triggers a full GET page reload)
- Use this when URL-param approach fails to show Browse Parts

---

### scraper/car_selector.py — Business Rules

```
EGY market only — skip any model not available in EGY
Skip diesel models (regex: starts with digits, then 'd' at end, 'xd', 'd ed', 'td')
Diesel regex: r'^\d+[a-z]*d(xd?|\s|$)'  (case insensitive)
ALL production dates per model (iterate all, not just first)
ALL engines per production date (iterate all)
4-char prefix dedup: type_code_full[:4] e.g. "VA99" — if prefix already scraped/seen this run, skip silently
Left-hand drive preferred; if absent use first available steering option
Cars only, no motorcycles (filter by series — motorcycles have different series codes on RealOEM)
```

---

### scraper/filters.py

```python
# Diesel detection regex
_DIESEL_RE = re.compile(
    r'^\d+[a-z]*d(xd?|\s|$)',
    re.IGNORECASE
)

def is_diesel(model_name: str) -> bool:
    return bool(_DIESEL_RE.match(model_name.strip()))

def select_market(available_markets: list) -> str | None:
    for preferred in ("EGY", "EUR"):
        if preferred in available_markets:
            return preferred
    return None

def select_steering(available_steerings: list) -> str | None:
    if not available_steerings:
        return None
    for opt in available_steerings:
        if "left" in opt.lower():
            return opt
    return available_steerings[0]
```

---

### RealOEM URL patterns

```
Select page:    https://www.realoem.com/bmw/enUS/select?product=P&series=E90&body=...
Parts groups:   https://www.realoem.com/bmw/enUS/partgrp?id={TYPE_CODE_FULL}
Group subs:     https://www.realoem.com/bmw/enUS/partgrp?id={TYPE_CODE_FULL}&mg={mg}
Parts table:    https://www.realoem.com/bmw/enUS/showparts?id={TYPE_CODE_FULL}&diagId={diagId}

TYPE_CODE_FULL format: VA99-EGY-05-2005-E90-BMW-320i
  Prefix: first 4 chars (VA99, PG59, 3F56...)
  Market: EGY
  Prod:   05-2005 (month-year)
  Series: E90
  Make:   BMW
  Model:  320i
```

---

### Parts table scraping (scrape_parts_table)

Find the `<table>` that has headers containing any of: `part number`, `no.`, `description`, `price`

Map column indices by name:
- `idx_ref` → column containing "no." or "ref"
- `idx_desc` → "description" or "desc"
- `idx_supp` → "supp"
- `idx_qty` → "qty" or "quantity"
- `idx_from` → "from"
- `idx_to` → "up to" or "to"
- `idx_partnum` → "part number" or "part no"
- `idx_price` → "price"
- `idx_notes` → "notes" or "remarks"

For part number cell: extract `<a href>` for `detail_url` and link text for `part_number`.

Each part dict has keys: `ref_no`, `description`, `supplier`, `qty`, `from_date`, `to_date`, `part_number`, `price`, `notes`, `detail_url`

---

## Dockerfile (Scraper)

```dockerfile
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xauth libglib2.0-0 libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps
COPY . .

CMD ["python", "main.py"]
```

### requirements.txt (Scraper)
```
playwright>=1.47.0
playwright-stealth==2.0.1
beautifulsoup4==4.12.3
pyvirtualdisplay>=3.0
python-dotenv>=1.0.0
appwrite>=5.0.0
```

### .env (Scraper)
```
APPWRITE_ENDPOINT=https://fra.cloud.appwrite.io/v1
APPWRITE_PROJECT_ID=69be7ac700290e5d31c9
APPWRITE_API_KEY=standard_8629b847a5bdb0a83235c54eb9803cdc491e019bb23991ba898c9df48fcc88a7d43e9821e43f30733c9eccad91fc5d204cbc38c6ab1ae011cda7df29b1567675d2a979b29a924498c4db6d4688e17bf600c0a236d1027c59df8bc849b7f9c22ebfacabd31f7625e4a847cd4acf9f0c55d0fe8c54438b9d358022b9fea370c38b
APPWRITE_DB_ID=69be7ad40015bfb6fe70
```

---

## Frontend Architecture (Next.js)

### File structure
```
frontend/
├── package.json
├── next.config.js
├── .env.local
├── app/
│   ├── layout.tsx
│   ├── page.tsx              # Overview/dashboard
│   ├── catalog/
│   │   └── page.tsx          # Vehicle list
│   ├── catalog/[typeCode]/
│   │   └── page.tsx          # Car groups view
│   ├── catalog/[typeCode]/[groupId]/
│   │   └── page.tsx          # Parts view (subgroups + parts table)
│   ├── search/
│   │   └── page.tsx          # Full-text part search
│   └── log/
│       └── page.tsx          # Scrape log / vehicle progress
├── components/
│   ├── Sidebar.tsx
│   ├── StatCard.tsx
│   ├── CarCard.tsx
│   ├── GroupCard.tsx
│   ├── PartsTable.tsx
│   ├── SearchBar.tsx
│   └── ProgressBar.tsx
└── lib/
    └── appwrite.ts           # Appwrite JS SDK client + query helpers
```

### .env.local (Frontend)
```
NEXT_PUBLIC_APPWRITE_ENDPOINT=https://fra.cloud.appwrite.io/v1
NEXT_PUBLIC_APPWRITE_PROJECT_ID=69be7ac700290e5d31c9
NEXT_PUBLIC_APPWRITE_DB_ID=69be7ad40015bfb6fe70
```

**Note**: Frontend uses the Appwrite JS SDK with a public client (no API key). Set up Appwrite Database permissions so the `cars`, `groups`, `subgroups`, `parts`, `checkpoint`, and `scrape_log` collections allow **Any** role to **Read**. Only the scraper (server-side with API key) can write.

---

### Frontend Pages & Features

**Overview page** (`/`):
- 4 stat cards: total vehicles, total parts, total groups, total subgroups
- Grid of vehicle cards showing: BMW model name, series, type code prefix, market tag, engine tag, production year, parts count, groups count, scrape status (complete/in-progress with progress bar)
- Click any car → navigate to `/catalog/[typeCode]`
- Export JSON button → download all data as `bmw-parts-export.json`
- Refresh button → re-fetch from Appwrite

**Catalog page** (`/catalog`):
- List of all vehicles, smaller cards
- Click → go to groups view

**Groups view** (`/catalog/[typeCode]`):
- Car metadata header (type code, series, market, body, engine, production)
- Grid of group cards: group number, group name, subgroup count, parts count
- Click group → go to parts view

**Parts view** (`/catalog/[typeCode]/[groupId]`):
- Back button
- For each subgroup:
  - Subgroup header: ID, name, scrape timestamp
  - Diagram image (if available) — click to enlarge in modal
  - Parts table with columns: Ref, Description, Supplier, Qty, From Date, To Date, Part Number, Price, Notes

**Search page** (`/search`):
- Debounced search (400ms) — minimum 2 characters
- Shows first 100 results max with truncation notice
- Each result: car model, group name, subgroup name, part details
- Click result → navigate to that car/group

**Scrape Log page** (`/log`):
- Vehicle progress grid: each car shows a progress bar (completed_groups / total_groups), status badge
- Scrape event table from `scrape_log` collection: type_code, status, timestamp, parts count

---

### Design Requirements

Dark theme. Professional. Clean.

```
Background:     #0d0d0f
Surface:        #141416
Card:           #1a1a1d
Border:         #2a2a2e
Text primary:   #f0f0f0
Text secondary: #888
Accent blue:    #3b82f6
Success green:  #22c55e
Warning orange: #f59e0b
```

Font: Inter (body), JetBrains Mono (type codes, part numbers)

Cards have subtle border, hover lifts with box-shadow. Status dot: green = complete, orange pulse = in-progress.

Sidebar navigation with icons (same 4 sections: Overview, Catalog, Part Search, Scrape Log).

---

## Data Safety Rules (Never Break These)

1. **Checkpoint is the ONLY source of truth** for which cars are done. Never use `scrape_log` to decide what to skip.
2. **Write to Appwrite after every complete group**, not every subgroup (reduces API calls while ensuring crash safety).
3. **Restart browser every 4 cars** to prevent Chrome OOM on long-running Linux servers.
4. **Never exit the session loop** unless `_get_cars_for_session()` returns `([], False)` after a fresh RealOEM enumeration.
5. **When the car list cache shows 0 remaining** cars → delete the cache and return `([], needs_followup=True)` to force a fresh RealOEM enumeration next session.
6. **Progress log is append-only audit**. A crash between `progress.mark_completed()` and `checkpoint.mark_car_done()` must not cause the scraper to skip a car on restart.

---

## Migration Script (migrate.py)

This is the very first thing to run.

```python
#!/usr/bin/env python3
"""
migrate.py — Import bmw-parts-export.json into Appwrite

Run once: python migrate.py
Requires: bmw-parts-export.json in the same directory
Sets all 8 cars as completed in checkpoint collection.
Deletes bmw-parts-export.json when done.
"""

import json
import os
import sys
from pathlib import Path
# Import Appwrite SDK and your appwrite_db module

EXPORT_FILE = "bmw-parts-export.json"

def main():
    if not Path(EXPORT_FILE).exists():
        print(f"ERROR: {EXPORT_FILE} not found.")
        sys.exit(1)

    print(f"Reading {EXPORT_FILE}...")
    with open(EXPORT_FILE, encoding="utf-8") as f:
        export = json.load(f)

    # Ensure Appwrite collections exist (create them if not)
    # For each series → for each car → insert car
    # For each group → insert group
    # For each subgroup → insert subgroup + parts
    # Mark car as completed in checkpoint

    total_cars = 0
    total_parts = 0

    for series_key, series_data in export["data"].items():
        for type_code, car_data in series_data["models"].items():
            print(f"Migrating {type_code}...")

            # Insert car
            upsert_car({
                "type_code_full": type_code,
                "prefix": type_code[:4],
                "series_value": car_data["series_value"],
                "series_label": car_data["series_label"],
                "body": car_data["body"],
                "model": car_data["model"],
                "market": car_data["market"],
                "prod_month": car_data["prod_month"],
                "engine": car_data["engine"],
                "steering": car_data["steering"],
            })

            completed_groups = []
            for group_id, group_data in car_data["groups"].items():
                upsert_group(type_code, group_id, group_data["group_name"])
                completed_groups.append(group_id)

                for diag_id, sg_data in group_data["subgroups"].items():
                    upsert_subgroup(type_code, group_id, diag_id,
                                    sg_data["subgroup_name"],
                                    sg_data.get("diagram_image_url", ""),
                                    sg_data.get("scraped_at", ""))
                    parts = sg_data.get("parts", [])
                    insert_parts_batch(type_code, diag_id, parts)
                    total_parts += len(parts)

            # Mark car complete in checkpoint
            upsert_checkpoint(type_code,
                              completed=True,
                              completed_groups=completed_groups,
                              in_progress_group=None)
            total_cars += 1
            print(f"  Done: {type_code}")

    print(f"\nMigration complete: {total_cars} cars, {total_parts} parts")

    # Delete export file
    os.remove(EXPORT_FILE)
    print(f"Deleted {EXPORT_FILE}")
    print("\nI'm done. You can now test the frontend.")

if __name__ == "__main__":
    main()
```

---

## Checklist for the Agent

Complete in this order:

- [ ] 1. Set up Appwrite project, create database `bmw_parts`, create all 6 collections with correct attributes and indexes
- [ ] 2. Write `migrate.py` with full Appwrite SDK integration
- [ ] 3. Run migration: `python migrate.py` with `bmw-parts-export.json` in same folder
- [ ] 4. Confirm: file deleted, terminal says "I'm done. You can now test the frontend."
- [ ] 5. Build scraper project (Python): `appwrite_db.py`, `notes.py`, `checkpoint.py`, `progress.py`, `browser.py`, `discovery.py`, `filters.py`, `car_selector.py`, `parts_scraper.py`, `main.py`, `config.py`
- [ ] 6. Build Dockerfile + docker-compose for scraper
- [ ] 7. Build Next.js frontend with all 5 pages + components + Appwrite JS SDK
- [ ] 8. Test frontend locally (`npm run dev`) against Appwrite data from migration
- [ ] 9. Configure Appwrite permissions: collections readable by Any, writable only by server key
- [ ] 10. Deploy scraper Docker container to Linux VPS
- [ ] 11. Deploy frontend to Vercel or Appwrite hosting

---

## What to Test After Migration

1. Open frontend at `localhost:3000`
2. Overview should show: 8 vehicles, ~112,000 parts, correct groups/subgroups count
3. Click any vehicle → see its groups
4. Click any group → see subgroups and parts table with all columns
5. Search "320" → get results from multiple cars
6. Scrape Log → all 8 cars show 100% progress, Complete badge
7. Export JSON → downloads a valid JSON file with all 8 cars

---

## Notes on Appwrite SDK

Use `appwrite` PyPI package for scraper. Use `appwrite` npm package for frontend.

Appwrite `Databases.list_documents()` returns `{ "documents": [...], "total": N }`. Always paginate:

```python
def list_all(db, database_id, collection_id, queries=[]):
    all_docs = []
    offset = 0
    while True:
        result = db.list_documents(database_id, collection_id,
                                   queries=queries + [Query.limit(100), Query.offset(offset)])
        all_docs.extend(result["documents"])
        if len(all_docs) >= result["total"]:
            break
        offset += len(result["documents"])
    return all_docs
```

Use `ID.unique()` for auto-generated document IDs. For upsert pattern (update if exists, create if not), catch the Appwrite `AppwriteException` with code 404 on update and then create instead.

Appwrite document attribute `$id` is the auto-generated document ID. Store your business keys (like `type_code_full`) as regular attributes, not as the document `$id`, so you can query by them.

---

## Common Mistakes to Avoid

1. **Do not use scrape_log to decide which cars to skip** — only checkpoint
2. **Do not run the scraper as an Appwrite Function** — it will timeout in 15 minutes
3. **Do not store the entire JSON blob as one Appwrite document** — Appwrite has document size limits
4. **Do not forget to paginate Appwrite list queries** — default limit is 25 documents
5. **Do not use Railway** — everything is on Appwrite + Linux VPS for scraper
6. **Do not forget to index** `car_type_code` on groups/subgroups/parts collections — queries will be extremely slow without it
7. **Do not hard-code credentials** — use `.env` / `.env.local` for all Appwrite keys
8. **Do not exit the scraper loop prematurely** — the ONLY valid exit is `([], False)` from `_get_cars_for_session` after a fresh RealOEM enumeration
9. **Do not write data after every subgroup** — only after every complete group (reduces API calls)
10. **Do not forget playwright-stealth** — without it, RealOEM will detect the bot immediately

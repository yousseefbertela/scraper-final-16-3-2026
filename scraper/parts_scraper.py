
import re, logging
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scraper.browser import safe_goto, human_delay, human_scroll, BrowserCrashError
from config import BASE_URL, SUBGROUP_DELAY, GROUP_DELAY
import os
logger = logging.getLogger(__name__)

# Main groups PartPilot never needs. 03 "Retrofitting / Conversion / Accessories"
# is dealer add-ons (M Performance trim, floor mats, umbrellas) and by itself is
# ~25% of a car's subgroups — decided 2026-09-21 to skip it for good.
SKIP_GROUPS = {g.strip() for g in os.environ.get("SKIP_GROUPS", "03").split(",") if g.strip()}
# Save a long group in slices this big, and open a fresh tab at the same time
# (100+ page loads in one tab was enough to OOM-kill a 1 GB pod).
SUBGROUP_SLICE = int(os.environ.get("SUBGROUP_SLICE", "25"))
PARTGRP_URL   = BASE_URL + "/bmw/enUS/partgrp"
SHOWPARTS_URL = BASE_URL + "/bmw/enUS/showparts"

def get_main_groups(page, type_code_full):
    url = f"{PARTGRP_URL}?id={type_code_full}"
    safe_goto(page, url)
    human_scroll(page)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    groups = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"[?&]mg=(\d+)", href)
        if m and "partgrp" in href:
            mg = m.group(1)
            name = a.get_text(strip=True)
            if mg and name and not any(g["mg"] == mg for g in groups):
                groups.append({"mg": mg, "name": name})
    skipped = [g["mg"] for g in groups if g["mg"] in SKIP_GROUPS]
    groups = [g for g in groups if g["mg"] not in SKIP_GROUPS]
    logger.info(f"{type_code_full}: {len(groups)} main groups found"
                + (f" (skipping {', '.join(skipped)})" if skipped else ""))
    return groups

def get_subgroups(page, type_code_full, mg):
    url = f"{PARTGRP_URL}?id={type_code_full}&mg={mg}"
    safe_goto(page, url)
    human_scroll(page)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    subgroups = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "showparts" not in href:
            continue
        m = re.search(r"[?&]diagId=([^&]+)", href)
        if m:
            diag_id = m.group(1).strip()
            name = a.get_text(strip=True)
            if diag_id and name and not any(s["diagId"] == diag_id for s in subgroups):
                subgroups.append({"diagId": diag_id, "name": name})
    logger.debug(f"Group {mg}: {len(subgroups)} subgroups")
    return subgroups

def get_diagram_image_url(page, type_code_full, diag_id):
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if diag_id.replace("_", "") in src.replace("_", "") or "diag" in src.lower():
            return urljoin(BASE_URL, src)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not any(x in src.lower() for x in ("logo", "icon", "button", "arrow")):
            return urljoin(BASE_URL, src)
    return ""

def scrape_parts_table(page, type_code_full, diag_id):
    url = f"{SHOWPARTS_URL}?id={type_code_full}&diagId={diag_id}"
    safe_goto(page, url)
    human_scroll(page)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    tables = soup.find_all("table")
    parts_table = None
    for tbl in tables:
        header_texts = [th.get_text(strip=True).lower() for th in tbl.find_all("th")]
        if any(h in ("part number", "no.", "description", "price") for h in header_texts):
            parts_table = tbl
            break
    if parts_table is None:
        logger.debug(f"No parts table found for diagId={diag_id}")
        return []
    header_row = parts_table.find("tr")
    if not header_row:
        return []
    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
    def col(idx, cells):
        if 0 <= idx < len(cells):
            return cells[idx].get_text(" ", strip=True)
        return ""
    def find_col(*names):
        for name in names:
            for i, h in enumerate(headers):
                if name in h:
                    return i
        return -1
    idx_ref     = find_col("no.", "no ", "ref")
    idx_desc    = find_col("description", "desc")
    idx_supp    = find_col("supp")
    idx_qty     = find_col("qty", "quantity")
    idx_from    = find_col("from")
    idx_to      = find_col("up to", "to")
    idx_partnum = find_col("part number", "part no")
    idx_price   = find_col("price")
    idx_notes   = find_col("notes", "note", "remarks")
    rows = parts_table.find_all("tr")[1:]
    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        detail_url = ""
        part_number = ""
        if 0 <= idx_partnum < len(cells):
            pn_cell = cells[idx_partnum]
            a_tag = pn_cell.find("a", href=True)
            if a_tag:
                detail_url = urljoin(BASE_URL, a_tag["href"])
                part_number = a_tag.get_text(strip=True)
            else:
                part_number = pn_cell.get_text(strip=True)
        part = {
            "ref_no":      col(idx_ref, cells)     if idx_ref >= 0     else "",
            "description": col(idx_desc, cells)    if idx_desc >= 0    else "",
            "supplier":    col(idx_supp, cells)    if idx_supp >= 0    else "",
            "qty":         col(idx_qty, cells)     if idx_qty >= 0     else "",
            "from_date":   col(idx_from, cells)    if idx_from >= 0    else "",
            "to_date":     col(idx_to, cells)      if idx_to >= 0      else "",
            "part_number": part_number,
            "price":       col(idx_price, cells)   if idx_price >= 0   else "",
            "notes":       col(idx_notes, cells)   if idx_notes >= 0   else "",
            "detail_url":  detail_url,
        }
        if not any(v for v in part.values()):
            continue
        parts.append(part)
    logger.debug(f"diagId={diag_id}: {len(parts)} parts parsed")
    return parts

def scrape_group(page, car, group, on_subgroup=None, skip_ids=None, on_slice=None):
    """
    Scrape ONE main group and return (group_node, parts_count), where group_node
    is {"group_name", "subgroups": {diagId: {...}}} in the exact shape stored in
    scraped_files. Used by the parallel worker: each instance takes groups from
    the shared job table and merges the finished node into the catalog.

    skip_ids       subgroups already stored (resume after a takeover) — not re-scraped.
    on_slice(node) called every SUBGROUP_SLICE subgroups with the subgroups scraped
                   since the last call, so the caller can persist them; the tab is
                   recycled at the same point to keep Chromium's memory flat.
    on_subgroup(diag_id) called after every subgroup (heartbeat hook); if it returns
                   False the group was handed to another instance and we stop —
                   the function then returns (None, 0).
    Raises BrowserCrashError for the caller to relaunch.
    The returned node holds only what was scraped since the last slice; callers
    merge, so nothing is lost.
    """
    type_code = car["type_code_full"]
    mg = group["mg"]
    skip_ids = set(skip_ids or ())
    node = {"group_name": group["name"], "subgroups": {}}
    parts_total = 0
    try:
        subgroups = get_subgroups(page, type_code, mg)
    except BrowserCrashError:
        raise
    except Exception as e:
        logger.error(f"Error getting subgroups for group {mg}: {e}")
        subgroups = []
    todo = [sg for sg in subgroups if sg["diagId"] not in skip_ids]
    if skip_ids:
        logger.info(f"Group {mg}: resuming — {len(subgroups) - len(todo)} of {len(subgroups)} subgroups already stored")
    since_slice = 0
    for subgroup in todo:
        diag_id = subgroup["diagId"]
        logger.info("  Subgroup %s: %s", diag_id, subgroup["name"])
        human_delay(SUBGROUP_DELAY)
        scrape_error = None
        try:
            parts = scrape_parts_table(page, type_code, diag_id)
            diagram_url = get_diagram_image_url(page, type_code, diag_id)
        except BrowserCrashError:
            raise
        except Exception as e:
            logger.error(f"  Error scraping subgroup {diag_id}: {e}")
            parts, diagram_url, scrape_error = [], "", str(e)
        entry = {
            "subgroup_name":     subgroup["name"],
            "diagram_image_url": diagram_url,
            "scraped_at":        datetime.utcnow().isoformat(),
            "parts":             parts,
        }
        if scrape_error:
            entry["scrape_error"] = scrape_error
        node["subgroups"][diag_id] = entry
        parts_total += len(parts)
        since_slice += 1
        if on_subgroup is not None and on_subgroup(diag_id) is False:
            logger.warning(f"Group {mg} was reassigned to another instance; abandoning it")
            return None, 0
        if on_slice is not None and since_slice >= SUBGROUP_SLICE:
            on_slice({"group_name": group["name"], "subgroups": dict(node["subgroups"])})
            node["subgroups"].clear()
            since_slice = 0
            page = _recycle_tab(page)
    return node, parts_total


def _recycle_tab(page):
    """Open a fresh tab in the same context and close the old one (memory reset, cookies kept)."""
    try:
        ctx = page.context
        fresh = ctx.new_page()
        fresh.set_default_timeout(45_000)
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(fresh)
        except Exception:
            pass
        page.close()
        logger.debug("Recycled browser tab")
        return fresh
    except Exception as e:
        logger.warning(f"Tab recycle failed ({e}); continuing with the old tab")
        return page


def scrape_car_parts(page, car, notes_writer, checkpoint_manager):
    """
    Scrape all groups/subgroups/parts for a single car.
    Returns total parts count (int).
    Raises BrowserCrashError if the browser dies mid-scrape (caller restarts).
    """
    type_code = car["type_code_full"]
    logger.info(f"Starting parts scrape for {type_code}")
    groups = get_main_groups(page, type_code)
    if not groups:
        logger.warning(f"No groups found for {type_code}")
        return 0
    checkpoint_manager.set_total_groups(car, len(groups))
    total_parts = 0
    for group in groups:
        mg = group["mg"]
        if checkpoint_manager.is_group_done(type_code, mg):
            logger.info("Skipping (already done): group %s - %s", mg, group["name"])
            continue
        logger.info("Scraping group %s: %s", mg, group["name"])
        checkpoint_manager.set_in_progress(car, mg)
        try:
            subgroups = get_subgroups(page, type_code, mg)
        except BrowserCrashError:
            raise
        except Exception as e:
            logger.error(f"Error getting subgroups for group {mg}: {e}")
            subgroups = []
        for subgroup in subgroups:
            diag_id = subgroup["diagId"]
            logger.info("  Subgroup %s: %s", diag_id, subgroup["name"])
            human_delay(SUBGROUP_DELAY)
            scrape_error = None
            try:
                parts = scrape_parts_table(page, type_code, diag_id)
                diagram_url = get_diagram_image_url(page, type_code, diag_id)
            except BrowserCrashError:
                raise  # propagate up to main.py for browser restart
            except Exception as e:
                logger.error(f"  Error scraping subgroup {diag_id}: {e}")
                parts = []
                diagram_url = ""
                scrape_error = str(e)
            notes_writer.save_subgroup(car, group, subgroup, diagram_url, parts, error=scrape_error)
            total_parts += len(parts)
            logger.info(f"  Buffered {len(parts)} parts for {diag_id}")
        notes_writer.flush()
        checkpoint_manager.mark_group_done(car, mg)
        human_delay(GROUP_DELAY)
    checkpoint_manager.mark_car_done(car)
    logger.info(f"Completed: {type_code} - {total_parts} total parts")
    return total_parts

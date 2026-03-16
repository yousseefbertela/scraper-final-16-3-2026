
import re, logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from scraper.browser import safe_goto, human_delay, human_scroll
from config import BASE_URL, SUBGROUP_DELAY, GROUP_DELAY
logger = logging.getLogger(__name__)
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
    logger.info(f"{type_code_full}: {len(groups)} main groups found")
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

def scrape_car_parts(page, car, notes_writer, checkpoint_manager):
    type_code = car["type_code_full"]
    logger.info(f"Starting parts scrape for {type_code}")
    groups = get_main_groups(page, type_code)
    if not groups:
        logger.warning(f"No groups found for {type_code}")
        return
    for group in groups:
        mg = group["mg"]
        if checkpoint_manager.is_group_done(type_code, mg):
            logger.info("Skipping (already done): group %s - %s", mg, group["name"])
            continue
        logger.info("Scraping group %s: %s", mg, group["name"])
        checkpoint_manager.set_in_progress(car, mg)
        try:
            subgroups = get_subgroups(page, type_code, mg)
        except Exception as e:
            logger.error(f"Error getting subgroups for group {mg}: {e}")
            subgroups = []
        for subgroup in subgroups:
            diag_id = subgroup["diagId"]
            logger.info("  Subgroup %s: %s", diag_id, subgroup["name"])
            human_delay(SUBGROUP_DELAY)
            try:
                parts = scrape_parts_table(page, type_code, diag_id)
                diagram_url = get_diagram_image_url(page, type_code, diag_id)
            except Exception as e:
                logger.error(f"  Error scraping subgroup {diag_id}: {e}")
                parts = []
                diagram_url = ""
            notes_writer.save_subgroup(car, group, subgroup, diagram_url, parts)
            logger.info(f"  Saved {len(parts)} parts for {diag_id}")
        checkpoint_manager.mark_group_done(car, mg)
        human_delay(GROUP_DELAY)
    checkpoint_manager.mark_car_done(car)
    logger.info(f"Completed: {type_code}")

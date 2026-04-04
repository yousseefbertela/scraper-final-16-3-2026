// Load .env from parent directory (for local development)
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const express = require('express');
const { Pool } = require('pg');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

/**
 * pg v8+ / pg-connection-string: sslmode=require is treated like verify-full unless
 * uselibpqcompat=true is set (see server warning). DigitalOcean managed PG needs TLS
 * but uses a chain Node rejects without this + rejectUnauthorized: false.
 */
function buildPgConnectionString() {
  const raw = process.env.DATABASE_URL;
  if (!raw) return raw;
  try {
    const url = new URL(raw);
    if (!url.searchParams.has('uselibpqcompat')) {
      url.searchParams.set('uselibpqcompat', 'true');
    }
    return url.toString();
  } catch {
    const sep = raw.includes('?') ? '&' : '?';
    return raw.includes('uselibpqcompat')
      ? raw
      : `${raw}${sep}uselibpqcompat=true`;
  }
}

const pool = new Pool({
  connectionString: buildPgConnectionString(),
  ssl: { rejectUnauthorized: false },
});

app.use(express.static(path.join(__dirname, 'public')));

// ── Two-tier cache ─────────────────────────────────────────────────────────────
//
// Tier 1: _summary  — metadata + counts only (~5KB total for all cars).
//         Used by: /api/overview, /api/target-list
//         Load time: <1 second
//
// Tier 2: full data — all parts arrays (~50-100MB).
//         Used by: /api/search, /api/export only
//         Load time: slow (only load when user actually searches)
//
// Per-car data — one prefix row (~1-2MB).
//         Used by: /api/cars/:typeCode, /api/cars/:typeCode/groups/:groupId
//         Load time: ~2-5 seconds

let summaryCache      = null;
let summaryDbTime     = null;   // DB updated_at of the cached _summary row
let summaryBuildLock  = null;   // prevents thundering herd on first build
let listCache         = null;
let listTime          = null;
let fullCache         = null;
let fullTime          = null;
const LIST_TTL    = 60 * 60_000; // 1 hour  (car lists change very rarely)
const FULL_TTL    = 30 * 60_000; // 30 minutes

function invalidateCache() {
  summaryCache = null; summaryDbTime = null;
  listCache    = null; listTime      = null;
  fullCache    = null; fullTime      = null;
}

// ── Tier 1: Summary ───────────────────────────────────────────────────────────
// Always checks DB updated_at — cache refreshes the instant the scraper writes a new group.

async function getSummaryData() {
  const res = await pool.query(
    "SELECT content, updated_at FROM scraped_files WHERE filename = '_summary'"
  );
  if (res.rows.length > 0) {
    const dbTime = new Date(res.rows[0].updated_at).getTime();
    // Return cached version if DB hasn't changed since we last loaded
    if (summaryCache && summaryDbTime === dbTime) return summaryCache;
    summaryCache  = JSON.parse(res.rows[0].content);
    summaryDbTime = dbTime;
    return summaryCache;
  }

  // _summary missing — only ONE build runs at a time (thundering herd fix)
  if (!summaryBuildLock) {
    summaryBuildLock = buildAndCacheSummary().finally(() => { summaryBuildLock = null; });
  }
  return summaryBuildLock;
}

// Car lists: cached for 1 hour (289 cars × 5 scrapers, changes only when redeployed)
async function getCarLists() {
  if (listCache && (Date.now() - listTime < LIST_TTL)) return listCache;
  const res = await pool.query(
    'SELECT scraper_id, car_data FROM scraper_car_lists ORDER BY scraper_id'
  );
  listCache = res.rows;
  listTime  = Date.now();
  console.log(`[cache] Car lists loaded from DB (${res.rows.length} scrapers)`);
  return listCache;
}

async function buildAndCacheSummary() {
  // Load existing summary from DB (may be empty on first run)
  const existingRes = await pool.query(
    "SELECT content, updated_at FROM scraped_files WHERE filename = '_summary'"
  );
  const summary    = existingRes.rows.length > 0 ? JSON.parse(existingRes.rows[0].content) : {};
  const lastBuilt  = existingRes.rows.length > 0 ? new Date(existingRes.rows[0].updated_at) : new Date(0);

  // Only fetch prefix rows that changed since last summary build
  const changedRes = await pool.query(
    "SELECT filename, content FROM scraped_files WHERE filename ~ '^[A-Z0-9]{4}$' AND updated_at > $1",
    [lastBuilt]
  );

  if (changedRes.rows.length === 0 && existingRes.rows.length > 0) {
    // Nothing changed — return existing summary as-is
    summaryCache = summary;
    summaryDbTime = null;  // force re-check from DB on next request
    console.log(`[summary] No changes since last build — reusing (${Object.keys(summary).length} cars)`);
    return summary;
  }

  console.log(`[summary] Incremental update: ${changedRes.rows.length} changed prefix(es)...`);

  for (const row of changedRes.rows) {
    try {
      const prefixData = JSON.parse(row.content);
      for (const [tc, car] of Object.entries(prefixData)) {
        let parts = 0, groups = 0, subgroups = 0;
        for (const g of Object.values(car.groups || {})) {
          groups++;
          for (const sg of Object.values(g.subgroups || {})) {
            subgroups++;
            parts += (sg.parts || []).length;
          }
        }
        summary[tc] = {
          series_label:    car.series_label || car.series_value || '',
          model:           car.model        || '',
          market:          car.market       || '',
          body:            car.body         || '',
          engine:          car.engine       || '',
          prod_month:      car.prod_month   || '',
          parts_count:     parts,
          groups_count:    groups,
          subgroups_count: subgroups,
        };
      }
    } catch (e) {
      console.error(`[summary] Failed to parse prefix row ${row.filename}:`, e.message);
    }
  }

  await pool.query(
    `INSERT INTO scraped_files (filename, content, updated_at) VALUES ('_summary', $1, NOW())
     ON CONFLICT (filename) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()`,
    [JSON.stringify(summary)]
  );
  summaryCache = summary;
  summaryDbTime = null;  // force re-check from DB on next request
  console.log(`[summary] Updated — ${Object.keys(summary).length} cars total`);
  return summary;
}

// ── Tier 2: Full data (search / export only) ──────────────────────────────────

async function getAllCarsData() {
  if (fullCache && (Date.now() - fullTime < FULL_TTL)) return fullCache;

  const res = await pool.query(
    "SELECT filename, content FROM scraped_files WHERE filename ~ '^[A-Z0-9]{4}$' ORDER BY filename"
  );
  const cars = {};
  for (const row of res.rows) {
    try {
      Object.assign(cars, JSON.parse(row.content));
    } catch (e) {
      console.error(`Failed to parse row ${row.filename}:`, e.message);
    }
  }
  fullCache = cars;
  fullTime  = Date.now();
  return cars;
}

// ── Per-car: load only one prefix row ─────────────────────────────────────────

async function getCarData(typeCode) {
  const prefix = typeCode.substring(0, 4).toUpperCase();
  const res = await pool.query(
    'SELECT content FROM scraped_files WHERE filename = $1',
    [prefix]
  );
  if (!res.rows.length) return null;
  try {
    const prefixData = JSON.parse(res.rows[0].content);
    return prefixData[typeCode] || null;
  } catch (e) {
    console.error(`Failed to parse prefix row ${prefix}:`, e.message);
    return null;
  }
}

// ── API routes ────────────────────────────────────────────────────────────────

// Overview: reads _summary + checkpoints — loads in <1s
// Auth — password checked server-side against ADMIN_PASSWORD env var
app.post('/api/auth', express.json(), (req, res) => {
  const { password } = req.body || {};
  const correct = process.env.ADMIN_PASSWORD;
  if (!correct) return res.status(503).json({ error: 'ADMIN_PASSWORD env var not set' });
  if (password === correct) return res.json({ ok: true });
  return res.status(401).json({ ok: false, error: 'Incorrect password' });
});

app.get('/api/overview', async (req, res) => {
  try {
    const [summary, cpRows] = await Promise.all([
      getSummaryData(),
      pool.query('SELECT scraper_id, checkpoint_data FROM scraper_checkpoints ORDER BY scraper_id'),
    ]);

    // Build set of in-progress type_codes from all scraper checkpoints
    const inProgress = new Map(); // type_code -> { groups_done, scraper_id }
    for (const row of cpRows.rows) {
      const cp = typeof row.checkpoint_data === 'string'
        ? JSON.parse(row.checkpoint_data)
        : row.checkpoint_data;
      for (const [tc, info] of Object.entries(cp.cars || {})) {
        if (!info.completed) {
          const done = info.completed_groups || [];
          inProgress.set(tc, {
            scraper_id:       row.scraper_id,
            groups_done:      done.length,
            in_progress_group: info.in_progress_group || (done.length > 0 ? String(done.length + 1).padStart(2,'0') : '01'),
          });
        }
      }
    }

    let totalParts = 0, totalGroups = 0, totalSubgroups = 0;
    const carList = [];

    for (const [tc, car] of Object.entries(summary)) {
      totalParts     += car.parts_count;
      totalGroups    += car.groups_count;
      totalSubgroups += car.subgroups_count;
      const prog = inProgress.get(tc);
      carList.push({
        type_code:       tc,
        series:          car.series_label,
        model:           car.model,
        market:          car.market,
        body:            car.body,
        engine:          car.engine,
        prod_month:      car.prod_month,
        parts_count:     car.parts_count,
        groups_count:    car.groups_count,
        subgroups_count: car.subgroups_count,
        completed:          !prog,
        in_progress:        !!prog,
        groups_done:        prog ? prog.groups_done : car.groups_count,
        in_progress_group:  prog ? prog.in_progress_group : null,
      });
    }

    // Add in-progress cars not yet in summary (started but 0 groups flushed yet)
    for (const [tc, prog] of inProgress.entries()) {
      if (!summary[tc]) {
        carList.push({
          type_code:   tc,
          series:      '',
          model:       tc,
          market:      '',
          body:        '',
          engine:      '',
          prod_month:  '',
          parts_count: 0,
          groups_count: 0,
          subgroups_count: 0,
          completed:          false,
          in_progress:        true,
          groups_done:        prog.groups_done,
          in_progress_group:  prog.in_progress_group,
        });
      }
    }

    res.json({
      total_cars:       carList.length,
      total_parts:      totalParts,
      total_groups:     totalGroups,
      total_subgroups:  totalSubgroups,
      cars:             carList,
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Car detail: reads only one prefix row (~1-2MB) — loads in ~2-5s
app.get('/api/cars/:typeCode', async (req, res) => {
  try {
    const car = await getCarData(req.params.typeCode);
    if (!car) {
      // Check if it's an in-progress car with no data flushed yet
      const cpRes = await pool.query(
        'SELECT scraper_id, checkpoint_data FROM scraper_checkpoints ORDER BY scraper_id'
      );
      for (const row of cpRes.rows) {
        const cp = typeof row.checkpoint_data === 'string'
          ? JSON.parse(row.checkpoint_data) : row.checkpoint_data;
        const info = (cp.cars || {})[req.params.typeCode];
        if (info && !info.completed) {
          const done = info.completed_groups || [];
          return res.json({
            type_code:   req.params.typeCode,
            series:      '',
            model:       req.params.typeCode,
            market:      '',
            body:        '',
            engine:      '',
            prod_month:  '',
            steering:    '',
            in_progress: true,
            groups_done: done.length,
            in_progress_group: info.in_progress_group || (done.length > 0 ? String(done.length + 1).padStart(2,'0') : '01'),
            groups: [],
          });
        }
      }
      return res.status(404).json({ error: 'Car not found' });
    }

    const groups = Object.entries(car.groups || {}).map(([gId, g]) => ({
      group_id:       gId,
      group_name:     g.group_name,
      subgroup_count: Object.keys(g.subgroups || {}).length,
      parts_count:    Object.values(g.subgroups || {})
                        .reduce((s, sg) => s + (sg.parts || []).length, 0),
    })).sort((a, b) => a.group_id.localeCompare(b.group_id));

    res.json({
      type_code:  req.params.typeCode,
      series:     car.series_label || car.series_value || '',
      model:      car.model        || '',
      market:     car.market       || '',
      body:       car.body         || '',
      engine:     car.engine       || '',
      prod_month: car.prod_month   || '',
      steering:   car.steering     || '',
      groups,
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Group/parts: reads only one prefix row — loads in ~2-5s
app.get('/api/cars/:typeCode/groups/:groupId', async (req, res) => {
  try {
    const car = await getCarData(req.params.typeCode);
    if (!car) return res.status(404).json({ error: 'Car not found' });

    const group = (car.groups || {})[req.params.groupId];
    if (!group) return res.status(404).json({ error: 'Group not found' });

    const subgroups = Object.entries(group.subgroups || {}).map(([sgId, sg]) => ({
      subgroup_id:       sgId,
      subgroup_name:     sg.subgroup_name,
      diagram_image_url: sg.diagram_image_url,
      scraped_at:        sg.scraped_at,
      parts:             sg.parts || [],
    }));

    res.json({ group_id: req.params.groupId, group_name: group.group_name, subgroups });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Search: needs full data — will be slow if not cached; use after overview has cached it
app.get('/api/search', async (req, res) => {
  try {
    const q = (req.query.q || '').toLowerCase().trim();
    if (q.length < 2) return res.json({ results: [] });

    const cars = await getAllCarsData();
    const results = [];

    outer:
    for (const [typeCode, car] of Object.entries(cars)) {
      for (const [gId, group] of Object.entries(car.groups || {})) {
        for (const [sgId, sg] of Object.entries(group.subgroups || {})) {
          for (const part of (sg.parts || [])) {
            if (JSON.stringify(part).toLowerCase().includes(q)) {
              results.push({
                type_code:     typeCode,
                model:         car.model,
                series:        car.series_label || car.series_value || '',
                group_id:      gId,
                group_name:    group.group_name,
                subgroup_id:   sgId,
                subgroup_name: sg.subgroup_name,
                part,
              });
              if (results.length >= 100) break outer;
            }
          }
        }
      }
    }

    res.json({ results, truncated: results.length >= 100 });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Export: needs full data
app.get('/api/export', async (req, res) => {
  try {
    const cars = await getAllCarsData();
    res.setHeader('Content-Disposition', 'attachment; filename="bmw-parts-export.json"');
    res.setHeader('Content-Type', 'application/json');
    res.send(JSON.stringify(cars, null, 2));
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Target list: reads _summary + scraper_car_lists — loads in <1s
app.get('/api/target-list', async (req, res) => {
  try {
    const [summary, listsRows] = await Promise.all([
      getSummaryData(),
      getCarLists(),
    ]);

    const scrapedCodes = new Set(Object.keys(summary).map(tc => tc.substring(0, 4)));

    const scrapers = listsRows.map(row => {
      const list = Array.isArray(row.car_data) ? row.car_data : JSON.parse(row.car_data);
      return {
        scraper_id: row.scraper_id,
        label:      row.scraper_id === 9 ? 'Navigation Problem' : `Scraper ${row.scraper_id}`,
        total:      list.length,
        scraped:    list.filter(c => scrapedCodes.has(c.code)).length,
        cars:       list.map(c => ({
          num:        c.num,
          code:       c.code,
          model:      c.model,
          series:     c.series,
          market:     c.market,
          prod_month: c.prod_month,
          engine:     c.engine,
          scraped:    scrapedCodes.has(c.code),
          custom:     c.custom || {},
        })),
      };
    });

    // Cars already in DB but not in any scraper's target list (previously scraped)
    const allTargetCodes = new Set(scrapers.flatMap(sc => sc.cars.map(c => c.code)));
    const prevCars = Object.entries(summary)
      .filter(([tc]) => !allTargetCodes.has(tc.substring(0, 4)))
      .map(([tc, car], i) => ({
        num:        i + 1,
        code:       tc.substring(0, 4),
        model:      car.model        || '',
        series:     car.series_label || '',
        market:     car.market       || '',
        engine:     car.engine       || '',
        prod_month: car.prod_month   || '',
        scraped:    true,
        custom:     {},
      }));

    if (prevCars.length > 0) {
      scrapers.unshift({
        scraper_id: 0,
        label:      `Previously Scraped (${prevCars.length})`,
        total:      prevCars.length,
        scraped:    prevCars.length,
        cars:       prevCars,
      });
    }

    res.json({ scrapers });
  } catch (e) { res.status(500).json({ error: e.message }); }
});




// GET all custom columns
app.get('/api/columns', async (req, res) => {
  try {
    const r = await pool.query("SELECT content FROM scraped_files WHERE filename = '_columns'");
    const cols = r.rows.length ? JSON.parse(r.rows[0].content) : [];
    res.json({ columns: cols });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Add column
app.post('/api/columns', express.json(), async (req, res) => {
  try {
    const { title } = req.body;
    if (!title) return res.status(400).json({ error: 'title required' });
    const id = 'col_' + Date.now() + '_' + Math.random().toString(36).substr(2,5);
    const r = await pool.query("SELECT content FROM scraped_files WHERE filename = '_columns'");
    const cols = r.rows.length ? JSON.parse(r.rows[0].content) : [];
    cols.push({ id, title: title.trim(), order: cols.length });
    await pool.query("INSERT INTO scraped_files (filename,content,updated_at) VALUES ('_columns',$1,NOW()) ON CONFLICT (filename) DO UPDATE SET content=EXCLUDED.content,updated_at=NOW()", [JSON.stringify(cols)]);
    res.json({ success: true, column: { id, title: title.trim() } });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Rename column
app.patch('/api/columns/:id', express.json(), async (req, res) => {
  try {
    const { title } = req.body;
    if (!title) return res.status(400).json({ error: 'title required' });
    const r = await pool.query("SELECT content FROM scraped_files WHERE filename = '_columns'");
    if (!r.rows.length) return res.status(404).json({ error: 'no columns' });
    const cols = JSON.parse(r.rows[0].content);
    const col = cols.find(c => c.id === req.params.id);
    if (!col) return res.status(404).json({ error: 'column not found' });
    col.title = title.trim();
    await pool.query("UPDATE scraped_files SET content=$1,updated_at=NOW() WHERE filename='_columns'", [JSON.stringify(cols)]);
    res.json({ success: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Delete column
app.delete('/api/columns/:id', async (req, res) => {
  try {
    const r = await pool.query("SELECT content FROM scraped_files WHERE filename = '_columns'");
    if (!r.rows.length) return res.status(404).json({ error: 'no columns' });
    const cols = JSON.parse(r.rows[0].content).filter(c => c.id !== req.params.id);
    await pool.query("UPDATE scraped_files SET content=$1,updated_at=NOW() WHERE filename='_columns'", [JSON.stringify(cols)]);
    res.json({ success: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Update car custom field
app.patch('/api/target-list/scraper/:id/cars/:code/custom', express.json(), async (req, res) => {
  try {
    const scraperId = parseInt(req.params.id);
    const code = req.params.code.toUpperCase();
    const { colId, value } = req.body;
    const listRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id=$1', [scraperId]);
    if (!listRes.rows.length) return res.status(404).json({ error: 'scraper not found' });
    const list = Array.isArray(listRes.rows[0].car_data) ? listRes.rows[0].car_data : JSON.parse(listRes.rows[0].car_data);
    const car = list.find(c => c.code === code);
    if (!car) return res.status(404).json({ error: 'car not found' });
    if (!car.custom) car.custom = {};
    car.custom[colId] = value || '';
    await pool.query('UPDATE scraper_car_lists SET car_data=$1 WHERE scraper_id=$2', [JSON.stringify(list), scraperId]);
    listCache = null;
    res.json({ success: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// ── Target List — Edit Routes ─────────────────────────────────────────────────

// Add car to scraper list
app.post('/api/target-list/scraper/:id/cars', express.json(), async (req, res) => {
  try {
    const scraperId = parseInt(req.params.id);
    const { code } = req.body;
    if (!code) return res.status(400).json({ error: 'code required' });
    const upper = code.trim().toUpperCase();

    const listRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id = $1', [scraperId]);
    if (!listRes.rows.length) return res.status(404).json({ error: 'Scraper not found' });

    const list = Array.isArray(listRes.rows[0].car_data) ? listRes.rows[0].car_data : JSON.parse(listRes.rows[0].car_data);
    if (list.find(c => c.code === upper)) return res.status(409).json({ error: 'Car already in list' });

    // Auto-fill from summary if available
    const summary = await getSummaryData();
    let carInfo = { code: upper, model: upper, series: '', market: '', engine: '', prod_month: '' };
    for (const [tc, car] of Object.entries(summary)) {
      if (tc.substring(0, 4) === upper) {
        carInfo = { code: upper, model: car.model || upper, series: car.series_label || '', market: car.market || '', engine: car.engine || '', prod_month: car.prod_month || '' };
        break;
      }
    }

    list.push({ num: list.length + 1, ...carInfo });
    list.forEach((c, i) => { c.num = i + 1; });

    await pool.query('UPDATE scraper_car_lists SET car_data = $1 WHERE scraper_id = $2', [JSON.stringify(list), scraperId]);
    listCache = null;
    res.json({ success: true, car: carInfo });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Move car between scraper groups
app.post('/api/target-list/move-car', express.json(), async (req, res) => {
  try {
    const { fromScraperId, carCode, toScraperId } = req.body;
    const code = String(carCode).toUpperCase();

    // Remove from source
    const srcRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id=$1', [fromScraperId]);
    if (!srcRes.rows.length) return res.status(404).json({ error: 'Source scraper not found' });
    const srcList = Array.isArray(srcRes.rows[0].car_data) ? srcRes.rows[0].car_data : JSON.parse(srcRes.rows[0].car_data);
    const carIdx = srcList.findIndex(c => c.code === code);
    if (carIdx === -1) return res.status(404).json({ error: 'Car not found in source scraper' });
    const [car] = srcList.splice(carIdx, 1);
    srcList.forEach((c, i) => { c.num = i + 1; });
    await pool.query('UPDATE scraper_car_lists SET car_data=$1 WHERE scraper_id=$2', [JSON.stringify(srcList), fromScraperId]);

    // Append to destination (toScraperId=0 means Previously Scraped — just remove from source)
    if (toScraperId > 0) {
      const dstRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id=$1', [toScraperId]);
      if (!dstRes.rows.length) return res.status(404).json({ error: 'Destination scraper not found' });
      const dstList = Array.isArray(dstRes.rows[0].car_data) ? dstRes.rows[0].car_data : JSON.parse(dstRes.rows[0].car_data);
      car.num = dstList.length + 1;
      dstList.push(car);
      await pool.query('UPDATE scraper_car_lists SET car_data=$1 WHERE scraper_id=$2', [JSON.stringify(dstList), toScraperId]);
    }

    listCache = null;
    res.json({ success: true, car, fromScraperId, toScraperId });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Remove car from scraper list
app.delete('/api/target-list/scraper/:id/cars/:code', async (req, res) => {
  try {
    const scraperId = parseInt(req.params.id);
    const code = req.params.code.toUpperCase();

    const listRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id = $1', [scraperId]);
    if (!listRes.rows.length) return res.status(404).json({ error: 'Scraper not found' });

    const list = Array.isArray(listRes.rows[0].car_data) ? listRes.rows[0].car_data : JSON.parse(listRes.rows[0].car_data);
    const car = list.find(c => c.code === code);
    if (!car) return res.status(404).json({ error: 'Car not found' });

    const newList = list.filter(c => c.code !== code);
    newList.forEach((c, i) => { c.num = i + 1; });

    await pool.query('UPDATE scraper_car_lists SET car_data = $1 WHERE scraper_id = $2', [JSON.stringify(newList), scraperId]);
    listCache = null;
    res.json({ success: true, removed: car });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Edit core car fields (model, series, market, engine, prod_month)
app.patch('/api/target-list/scraper/:id/cars/:code', express.json(), async (req, res) => {
  try {
    const scraperId = parseInt(req.params.id);
    const code = req.params.code.toUpperCase();
    const { model, series, market, engine, prod_month } = req.body;
    const listRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id=$1', [scraperId]);
    if (!listRes.rows.length) return res.status(404).json({ error: 'Scraper not found' });
    const list = Array.isArray(listRes.rows[0].car_data) ? listRes.rows[0].car_data : JSON.parse(listRes.rows[0].car_data);
    const car = list.find(c => c.code === code);
    if (!car) return res.status(404).json({ error: 'Car not found' });
    const prev = { model: car.model, series: car.series, market: car.market, engine: car.engine, prod_month: car.prod_month };
    if (model      !== undefined) car.model      = String(model).trim();
    if (series     !== undefined) car.series     = String(series).trim();
    if (market     !== undefined) car.market     = String(market).trim();
    if (engine     !== undefined) car.engine     = String(engine).trim();
    if (prod_month !== undefined) {
      const pm = String(prod_month).trim();
      car.prod_month = pm;
      // Keep custom PROD_MONTH column in sync so the table display updates
      if (!car.custom) car.custom = {};
      car.custom['col_1775163979435_jilnc'] = pm;
    }
    await pool.query('UPDATE scraper_car_lists SET car_data=$1 WHERE scraper_id=$2', [JSON.stringify(list), scraperId]);
    listCache = null;
    res.json({ success: true, car, prev });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Update car notes
app.patch('/api/target-list/scraper/:id/cars/:code/notes', express.json(), async (req, res) => {
  try {
    const scraperId = parseInt(req.params.id);
    const code = req.params.code.toUpperCase();
    const { notes } = req.body;

    const listRes = await pool.query('SELECT car_data FROM scraper_car_lists WHERE scraper_id = $1', [scraperId]);
    if (!listRes.rows.length) return res.status(404).json({ error: 'Scraper not found' });

    const list = Array.isArray(listRes.rows[0].car_data) ? listRes.rows[0].car_data : JSON.parse(listRes.rows[0].car_data);
    const car = list.find(c => c.code === code);
    if (!car) return res.status(404).json({ error: 'Car not found' });

    car.notes = notes || '';
    await pool.query('UPDATE scraper_car_lists SET car_data = $1 WHERE scraper_id = $2', [JSON.stringify(list), scraperId]);
    listCache = null;
    res.json({ success: true });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

app.listen(PORT, async () => {
  console.log(`BMW Parts DB running at http://localhost:${PORT}`);
  // Pre-warm all lightweight caches in parallel so every first user request is instant
  Promise.all([
    getSummaryData()
      .then(s => console.log(`[startup] Summary ready: ${Object.keys(s).length} cars`))
      .catch(e => console.error('[startup] Summary build failed:', e.message)),
    getCarLists()
      .then(r => console.log(`[startup] Car lists ready: ${r.length} scrapers`))
      .catch(e => console.error('[startup] Car lists load failed:', e.message)),
  ]);
});

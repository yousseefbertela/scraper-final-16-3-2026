// Load .env from parent directory (for local development)
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const express = require('express');
const { Pool } = require('pg');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// ssl: rejectUnauthorized false needed for DO managed DB self-signed cert
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
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
let summaryTime       = null;
let summaryBuildLock  = null;   // prevents thundering herd on first build
let listCache         = null;
let listTime          = null;
let fullCache         = null;
let fullTime          = null;
const SUMMARY_TTL = 30 * 60_000; // 30 minutes
const LIST_TTL    = 60 * 60_000; // 1 hour  (car lists change very rarely)
const FULL_TTL    = 30 * 60_000; // 30 minutes

function invalidateCache() {
  summaryCache = null; summaryTime = null;
  listCache    = null; listTime    = null;
  fullCache    = null; fullTime    = null;
}

// ── Tier 1: Summary ───────────────────────────────────────────────────────────

async function getSummaryData() {
  if (summaryCache && (Date.now() - summaryTime < SUMMARY_TTL)) return summaryCache;

  const res = await pool.query(
    "SELECT content FROM scraped_files WHERE filename = '_summary'"
  );
  if (res.rows.length > 0) {
    summaryCache = JSON.parse(res.rows[0].content);
    summaryTime  = Date.now();
    return summaryCache;
  }

  // _summary not built yet — only ONE build runs at a time (thundering herd fix)
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
  console.log('[summary] Building from full data (one-time, will be instant next time)...');
  const cars = await getAllCarsData();
  const summary = {};
  for (const [tc, car] of Object.entries(cars)) {
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
  await pool.query(
    `INSERT INTO scraped_files (filename, content, updated_at) VALUES ('_summary', $1, NOW())
     ON CONFLICT (filename) DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()`,
    [JSON.stringify(summary)]
  );
  summaryCache = summary;
  summaryTime  = Date.now();
  console.log(`[summary] Built and saved to DB (${Object.keys(summary).length} cars)`);
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

// Overview: reads only _summary (~5KB) — loads in <1s
app.get('/api/overview', async (req, res) => {
  try {
    const summary = await getSummaryData();
    let totalParts = 0, totalGroups = 0, totalSubgroups = 0;
    const carList = [];

    for (const [tc, car] of Object.entries(summary)) {
      totalParts     += car.parts_count;
      totalGroups    += car.groups_count;
      totalSubgroups += car.subgroups_count;
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
        completed:       true,
      });
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
    if (!car) return res.status(404).json({ error: 'Car not found' });

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
        label:      `Scraper ${row.scraper_id}`,
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

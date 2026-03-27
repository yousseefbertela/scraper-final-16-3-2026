// Load .env from parent directory (for local development)
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

// Allow self-signed DO managed database certificate (safe for trusted DB endpoint)
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

const express = require('express');
const { Pool } = require('pg');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// Connection string comes from DATABASE_URL env var (set in DO App Platform or local .env)
const pool = new Pool({ connectionString: process.env.DATABASE_URL });

app.use(express.static(path.join(__dirname, 'public')));

let cachedData = null;
let cacheTime = null;
const CACHE_TTL = 60_000; // 1 minute

/**
 * Load all scraped car data from DB.
 * Each row with a 4-char uppercase filename key contains:
 *   { "<type_code_full>": { series_value, body, model, market, prod_month, engine, steering, groups: {...} } }
 *
 * Returns a flat map: { type_code_full → car_data }
 */
async function getAllCarsData() {
  if (cachedData && (Date.now() - cacheTime < CACHE_TTL)) return cachedData;

  // Only load 4-char prefix keys (e.g. "VA99", "NA36") — excludes system keys
  const res = await pool.query(
    "SELECT filename, content FROM scraped_files WHERE filename ~ '^[A-Z0-9]{4}$' ORDER BY filename"
  );

  const cars = {};
  for (const row of res.rows) {
    try {
      const prefixData = JSON.parse(row.content);
      // prefixData: { type_code_full: car_data, ... }
      Object.assign(cars, prefixData);
    } catch (e) {
      console.error(`Failed to parse row ${row.filename}:`, e.message);
    }
  }

  cachedData = cars;
  cacheTime = Date.now();
  return cars;
}

function invalidateCache() {
  cachedData = null;
  cacheTime = null;
}

// ── API routes ────────────────────────────────────────────────────────────────

app.get('/api/overview', async (req, res) => {
  try {
    const cars = await getAllCarsData();
    let totalParts = 0, totalSubgroups = 0, totalGroups = 0;
    const carList = [];

    for (const [typeCode, car] of Object.entries(cars)) {
      let carParts = 0, carGroups = 0, carSubgroups = 0;
      for (const group of Object.values(car.groups || {})) {
        carGroups++;
        for (const sg of Object.values(group.subgroups || {})) {
          carSubgroups++;
          carParts += (sg.parts || []).length;
        }
      }
      totalParts      += carParts;
      totalGroups     += carGroups;
      totalSubgroups  += carSubgroups;

      carList.push({
        type_code:        typeCode,
        series:           car.series_label || car.series_value || '',
        model:            car.model || '',
        market:           car.market || '',
        body:             car.body || '',
        engine:           car.engine || '',
        prod_month:       car.prod_month || '',
        parts_count:      carParts,
        groups_count:     carGroups,
        subgroups_count:  carSubgroups,
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


app.get('/api/cars/:typeCode', async (req, res) => {
  try {
    const cars = await getAllCarsData();
    const car = cars[req.params.typeCode];
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
      model:      car.model || '',
      market:     car.market || '',
      body:       car.body || '',
      engine:     car.engine || '',
      prod_month: car.prod_month || '',
      steering:   car.steering || '',
      groups,
    });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


app.get('/api/cars/:typeCode/groups/:groupId', async (req, res) => {
  try {
    const cars  = await getAllCarsData();
    const car   = cars[req.params.typeCode];
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
                type_code:      typeCode,
                model:          car.model,
                series:         car.series_label || car.series_value || '',
                group_id:       gId,
                group_name:     group.group_name,
                subgroup_id:    sgId,
                subgroup_name:  sg.subgroup_name,
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


app.get('/api/export', async (req, res) => {
  try {
    const cars = await getAllCarsData();
    res.setHeader('Content-Disposition', 'attachment; filename="bmw-parts-export.json"');
    res.setHeader('Content-Type', 'application/json');
    res.send(JSON.stringify(cars, null, 2));
  } catch (e) { res.status(500).json({ error: e.message }); }
});


// Show which cars are in each scraper's target list and which are already scraped
app.get('/api/target-list', async (req, res) => {
  try {
    const [carsData, listsRes] = await Promise.all([
      getAllCarsData(),
      pool.query('SELECT scraper_id, car_data FROM scraper_car_lists ORDER BY scraper_id'),
    ]);

    const scrapedCodes = new Set(Object.keys(carsData).map(tc => tc.substring(0, 4)));

    const scrapers = listsRes.rows.map(row => {
      const list = Array.isArray(row.car_data) ? row.car_data : JSON.parse(row.car_data);
      return {
        scraper_id: row.scraper_id,
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

    res.json({ scrapers });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


app.listen(PORT, () => console.log(`BMW Parts DB running at http://localhost:${PORT}`));

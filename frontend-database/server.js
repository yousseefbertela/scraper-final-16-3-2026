const express = require('express');
const { Pool } = require('pg');
const path = require('path');

const app = express();
const PORT = 3000;

const pool = new Pool({
  connectionString: 'postgresql://postgres:kfljOWQPmNhIgUeyngUywqHNBreAIrGf@gondola.proxy.rlwy.net:36301/railway',
  ssl: { rejectUnauthorized: false }
});

app.use(express.static(path.join(__dirname, 'public')));

let cachedData = null;
let cacheTime = null;

async function getMainData() {
  if (cachedData && (Date.now() - cacheTime < 60000)) return cachedData;
  const res = await pool.query("SELECT content FROM scraped_files WHERE filename = 'vFinal_notes.json'");
  cachedData = JSON.parse(res.rows[0].content);
  cacheTime = Date.now();
  return cachedData;
}

app.get('/api/overview', async (req, res) => {
  try {
    const [notes, cpRow] = await Promise.all([
      getMainData(),
      pool.query("SELECT content, updated_at FROM scraped_files WHERE filename = 'checkpoint.json'")
    ]);
    const checkpoint = JSON.parse(cpRow.rows[0].content);
    let totalParts = 0, totalSubgroups = 0, totalGroups = 0, carList = [];
    for (const series of Object.values(notes.data)) {
      for (const [typeCode, car] of Object.entries(series.models)) {
        let carParts = 0, carGroups = 0, carSubgroups = 0;
        for (const group of Object.values(car.groups || {})) {
          carGroups++;
          for (const sg of Object.values(group.subgroups || {})) {
            carSubgroups++;
            carParts += (sg.parts || []).length;
          }
        }
        totalParts += carParts; totalGroups += carGroups; totalSubgroups += carSubgroups;
        const cp = checkpoint.cars[typeCode];
        carList.push({ type_code: typeCode, series: car.series_label, model: car.model, market: car.market, body: car.body, engine: car.engine, prod_month: car.prod_month, parts_count: carParts, groups_count: carGroups, completed: cp ? cp.completed : false, completed_groups: cp ? cp.completed_groups.length : 0, in_progress_group: cp ? cp.in_progress_group : null });
      }
    }
    res.json({ total_cars: carList.length, total_parts: totalParts, total_groups: totalGroups, total_subgroups: totalSubgroups, checkpoint_updated: cpRow.rows[0].updated_at, cars: carList });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/cars/:typeCode', async (req, res) => {
  try {
    const notes = await getMainData();
    const { typeCode } = req.params;
    let foundCar = null;
    for (const series of Object.values(notes.data)) {
      if (series.models[typeCode]) { foundCar = series.models[typeCode]; break; }
    }
    if (!foundCar) return res.status(404).json({ error: 'Car not found' });
    const groups = Object.entries(foundCar.groups || {}).map(([gId, g]) => ({
      group_id: gId, group_name: g.group_name,
      subgroup_count: Object.keys(g.subgroups || {}).length,
      parts_count: Object.values(g.subgroups || {}).reduce((s, sg) => s + (sg.parts || []).length, 0)
    })).sort((a, b) => a.group_id.localeCompare(b.group_id));
    res.json({ type_code: typeCode, series: foundCar.series_label, model: foundCar.model, market: foundCar.market, body: foundCar.body, engine: foundCar.engine, prod_month: foundCar.prod_month, steering: foundCar.steering, groups });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/cars/:typeCode/groups/:groupId', async (req, res) => {
  try {
    const notes = await getMainData();
    const { typeCode, groupId } = req.params;
    let foundCar = null;
    for (const series of Object.values(notes.data)) {
      if (series.models[typeCode]) { foundCar = series.models[typeCode]; break; }
    }
    if (!foundCar) return res.status(404).json({ error: 'Car not found' });
    const group = (foundCar.groups || {})[groupId];
    if (!group) return res.status(404).json({ error: 'Group not found' });
    const subgroups = Object.entries(group.subgroups || {}).map(([sgId, sg]) => ({
      subgroup_id: sgId, subgroup_name: sg.subgroup_name, diagram_image_url: sg.diagram_image_url, scraped_at: sg.scraped_at, parts: sg.parts || []
    }));
    res.json({ group_id: groupId, group_name: group.group_name, subgroups });
  } catch (e) { res.status(500).json({ error: e.message }); }
});


app.get('/api/search', async (req, res) => {
  try {
    const q = (req.query.q || '').toLowerCase().trim();
    if (q.length < 2) return res.json({ results: [] });
    const notes = await getMainData();
    const results = [];
    outer:
    for (const series of Object.values(notes.data)) {
      for (const [typeCode, car] of Object.entries(series.models)) {
        for (const [gId, group] of Object.entries(car.groups || {})) {
          for (const [sgId, sg] of Object.entries(group.subgroups || {})) {
            for (const part of (sg.parts || [])) {
              if (JSON.stringify(part).toLowerCase().includes(q)) {
                results.push({ type_code: typeCode, model: car.model, series: car.series_label, group_id: gId, group_name: group.group_name, subgroup_id: sgId, subgroup_name: sg.subgroup_name, part });
                if (results.length >= 100) break outer;
              }
            }
          }
        }
      }
    }
    res.json({ results, truncated: results.length >= 100 });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/target-list', async (req, res) => {
  try {
    const row = await pool.query("SELECT content FROM scraped_files WHERE filename = 'car_list.json'");
    if (!row.rows.length) return res.status(404).json({ error: 'car_list.json not in DB. Run: node scripts/upload-car-list.js <path-to-file>' });
    const carList = JSON.parse(row.rows[0].content);

    // Collect scraped 4-char prefixes from notes
    const scrapedPrefixes = new Set();
    try {
      const notes = await getMainData();
      for (const series of Object.values(notes.data || {})) {
        for (const typeCode of Object.keys(series.models || {})) {
          scrapedPrefixes.add(typeCode.substring(0, 4));
        }
      }
    } catch (_) {}

    const groups = [];
    for (const [groupKey, variants] of Object.entries(carList)) {
      const prefixMatch = groupKey.match(/\[([A-Z0-9]+)\]/);
      const prefix = prefixMatch ? prefixMatch[1] : groupKey;
      const variantList = Object.values(variants);
      const first = variantList[0] || {};
      groups.push({
        prefix,
        model: first.model || '',
        series_label: first.series_label || '',
        body: first.body || '',
        engine: first.engine || '',
        market: first.market || 'EGY',
        variant_count: variantList.length,
        scraped: scrapedPrefixes.has(prefix)
      });
    }

    res.json({ total: groups.length, scraped: groups.filter(g => g.scraped).length, groups });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/api/export', async (req, res) => {
  try {
    const notes = await getMainData();
    const filename = 'bmw-parts-export.json';
    res.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    res.setHeader('Content-Type', 'application/json');
    res.send(JSON.stringify(notes, null, 2));
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.listen(PORT, () => console.log(`Running at http://localhost:${PORT}`));

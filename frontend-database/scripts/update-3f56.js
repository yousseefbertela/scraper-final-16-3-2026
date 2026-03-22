const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

async function main() {
  const dbJsonPath = path.join(__dirname, '..', 'bmw-parts-export (1).json');

  console.log('Loading source car data from', dbJsonPath);
  const dbRaw = fs.readFileSync(dbJsonPath, 'utf8');
  const dbData = JSON.parse(dbRaw);

  // Find the series and model key for the 3F56 car in the local file
  let sourceSeriesKey = null;
  let sourceModelKey = null;
  let sourceCar = null;

  for (const [seriesKey, series] of Object.entries(dbData.data || {})) {
    for (const [modelKey, model] of Object.entries(series.models || {})) {
      if (modelKey.startsWith('3F56-')) {
        sourceSeriesKey = seriesKey;
        sourceModelKey = modelKey;
        sourceCar = model;
        break;
      }
    }
    if (sourceCar) break;
  }

  if (!sourceCar) {
    throw new Error('Could not find model starting with "3F56-" in bmw-parts-export (1).json');
  }

  console.log('Found source car:', { sourceSeriesKey, sourceModelKey });

  const pool = new Pool({
    connectionString: 'postgresql://postgres:kfljOWQPmNhIgUeyngUywqHNBreAIrGf@gondola.proxy.rlwy.net:36301/railway',
    ssl: { rejectUnauthorized: false },
  });

  try {
    console.log('Fetching existing vFinal_notes.json from DB...');
    const res = await pool.query(
      "SELECT content FROM scraped_files WHERE filename = 'vFinal_notes.json' LIMIT 1"
    );

    if (res.rows.length === 0) {
      throw new Error("Row with filename 'vFinal_notes.json' not found in scraped_files");
    }

    const row = res.rows[0];
    const existing = JSON.parse(row.content);

    if (!existing.data || typeof existing.data !== 'object') {
      throw new Error("Existing vFinal_notes.json has unexpected structure (missing 'data')");
    }

    if (!existing.data[sourceSeriesKey]) {
      throw new Error(
        `Series '${sourceSeriesKey}' not found in existing vFinal_notes.json; refusing to create new series automatically`
      );
    }

    if (!existing.data[sourceSeriesKey].models) {
      existing.data[sourceSeriesKey].models = {};
    }

    const beforeHasCar =
      Object.prototype.hasOwnProperty.call(existing.data[sourceSeriesKey].models, sourceModelKey);

    console.log('Before update, car exists in DB JSON:', beforeHasCar);

    // Critical part: overwrite ONLY this car entry (all its groups, subgroups, and parts)
    existing.data[sourceSeriesKey].models[sourceModelKey] = sourceCar;

    const updatedContent = JSON.stringify(existing, null, 2);

    console.log('Writing updated JSON back to DB for 3F56 car only...');
    await pool.query(
      "UPDATE scraped_files SET content = $1, updated_at = NOW() WHERE filename = 'vFinal_notes.json'",
      [updatedContent]
    );

    console.log('Update completed successfully for', sourceModelKey);
  } finally {
    await pool.end();
  }
}

main().catch((err) => {
  console.error('Update failed:', err);
  process.exit(1);
});


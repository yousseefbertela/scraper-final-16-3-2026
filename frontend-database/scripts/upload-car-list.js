const { Pool } = require('pg');
const fs = require('fs');

const pool = new Pool({
  connectionString: 'postgresql://postgres:kfljOWQPmNhIgUeyngUywqHNBreAIrGf@gondola.proxy.rlwy.net:36301/railway',
  ssl: { rejectUnauthorized: false }
});

async function main() {
  const filePath = process.argv[2];
  if (!filePath) {
    console.error('Usage: node scripts/upload-car-list.js <path-to-egy_cars_only.json>');
    process.exit(1);
  }
  const content = fs.readFileSync(filePath, 'utf8');
  const parsed = JSON.parse(content);
  const total = Object.keys(parsed).length;
  console.log(`Uploading ${total} type code groups...`);
  await pool.query(`
    INSERT INTO scraped_files (filename, content, updated_at)
    VALUES ('car_list.json', $1, NOW())
    ON CONFLICT (filename) DO UPDATE SET content = $1, updated_at = NOW()
  `, [content]);
  console.log('Done — car_list.json stored in DB.');
  await pool.end();
}

main().catch(e => { console.error(e.message); process.exit(1); });

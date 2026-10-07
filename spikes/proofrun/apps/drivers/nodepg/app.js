// node-pg with the URL as given (SSC-040).
const http = require("node:http");
const { Pool } = require("pg");
const version = require("pg/package.json").version;

async function check() {
  const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 1 });
  try {
    const r = await pool.query("select current_database() as db, (select ssl from pg_stat_ssl where pid = pg_backend_pid()) as ssl");
    return { driver: "node-pg", version, connected: true, database: r.rows[0].db, ssl: r.rows[0].ssl };
  } catch (e) {
    return { driver: "node-pg", version, connected: false, error: String(e.message).slice(0, 200) };
  } finally {
    await pool.end();
  }
}

check().then((r) => {
  console.log("DRIVER_RESULT " + JSON.stringify(r));
  http.createServer((_, res) => res.end('{"ok":true}')).listen(Number(process.env.PORT || 8080));
});

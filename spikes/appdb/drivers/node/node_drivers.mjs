import fs from "node:fs";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const URL_ = process.env.DATABASE_URL;
const out = [];
const rec = (driver, version, ok, needed, error = "") => out.push({ driver, version, accepted_as_given: ok, needed, error });
const ver = (n) => JSON.parse(fs.readFileSync(`node_modules/${n}/package.json`, "utf8")).version;
const caPath = new URL(URL_).searchParams.get("sslrootcert");
const ca = fs.readFileSync(caPath, "utf8");

async function nodePg() {
  const { Client } = require("pg");
  try {
    const c = new Client({ connectionString: URL_ });
    await c.connect();
    const r = await c.query("select 1 as x");
    await c.end();
    if (r.rows[0].x !== 1) throw new Error("bad row");
    rec("node-pg", ver("pg"), true, "nothing; sslrootcert honoured from URL");
  } catch (e) {
    try {
      const c = new Client({ connectionString: URL_.split("?")[0], ssl: { ca, rejectUnauthorized: true } });
      await c.connect();
      await c.query("select 1");
      await c.end();
      rec("node-pg", ver("pg"), false, "ssl: { ca } in code; sslrootcert query param NOT honoured", String(e.message));
    } catch (e2) {
      rec("node-pg", ver("pg"), false, "", `${e.message} || ${e2.message}`);
    }
  }
}

async function drizzle() {
  const { drizzle: d } = require("drizzle-orm/node-postgres");
  const { sql } = require("drizzle-orm");
  const { Pool } = require("pg");
  try {
    const db = d(URL_);
    const r = await db.execute(sql`select 1 as x`);
    if (r.rows[0].x !== 1) throw new Error("bad row");
    rec("drizzle-orm (node-postgres)", ver("drizzle-orm"), true, "nothing; URL passed straight to drizzle()");
    await db.$client.end();
  } catch (e) {
    try {
      const pool = new Pool({ connectionString: URL_.split("?")[0], ssl: { ca, rejectUnauthorized: true } });
      const db = d({ client: pool });
      const r = await db.execute(sql`select 1 as x`);
      if (r.rows[0].x !== 1) throw new Error("bad row");
      await pool.end();
      rec("drizzle-orm (node-postgres)", ver("drizzle-orm"), false, "pg Pool with ssl: { ca } in code", String(e.message));
    } catch (e2) {
      rec("drizzle-orm (node-postgres)", ver("drizzle-orm"), false, "", `${e.message} || ${e2.message}`);
    }
  }
}

await nodePg();
await drizzle();
fs.writeFileSync(process.argv[2], JSON.stringify(out, null, 2));
console.log(JSON.stringify(out, null, 2));

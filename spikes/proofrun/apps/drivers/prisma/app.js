// Prisma 7 through @prisma/adapter-pg with the URL as given (SSC-040).
const http = require("node:http");
const { PrismaClient } = require("@prisma/client");
const { PrismaPg } = require("@prisma/adapter-pg");
const version = require("prisma/package.json").version;

async function check() {
  const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: process.env.DATABASE_URL, max: 1 }) });
  try {
    const r = await prisma.$queryRaw`select current_database() as db, (select ssl from pg_stat_ssl where pid = pg_backend_pid()) as ssl`;
    return { driver: "prisma", version, connected: true, database: r[0].db, ssl: r[0].ssl };
  } catch (e) {
    return { driver: "prisma", version, connected: false, error: String(e.message).slice(0, 200) };
  } finally {
    await prisma.$disconnect();
  }
}

check().then((r) => {
  console.log("DRIVER_RESULT " + JSON.stringify(r));
  http.createServer((_, res) => res.end('{"ok":true}')).listen(Number(process.env.PORT || 8080));
});

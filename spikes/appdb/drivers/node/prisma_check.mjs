import fs from "node:fs";
import { PrismaClient } from "@prisma/client";
import { PrismaPg } from "@prisma/adapter-pg";
const URL_ = process.env.DATABASE_URL;
const ver = (n) => JSON.parse(fs.readFileSync(`node_modules/${n}/package.json`, "utf8")).version;
const out = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const rec = (ok, needed, error = "") => out.push({ driver: "prisma (adapter-pg)", version: `${ver("prisma")} / adapter-pg ${ver("@prisma/adapter-pg")}`, accepted_as_given: ok, needed, error });
try {
  const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: URL_ }) });
  const r = await prisma.$queryRaw`select 1 as x`;
  if (Number(r[0].x) !== 1) throw new Error("bad row");
  await prisma.$disconnect();
  rec(true, "Prisma 7 rejects url= in schema.prisma; URL must be given in code to PrismaPg adapter, unchanged; sslrootcert honoured (node-pg underneath)");
} catch (e) {
  rec(false, "Prisma 7 needs adapter; URL given to PrismaPg", String(e.message));
}
try {
  const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: URL_.replace("ca.crt", "wrong-ca.crt") }) });
  await prisma.$queryRaw`select 1`;
  out.at(-1).error += " | WARNING wrong CA accepted";
} catch (e) {
  out.at(-1).needed += "; wrong CA rejected";
}
fs.writeFileSync(process.argv[2], JSON.stringify(out, null, 2));
console.log(JSON.stringify(out.at(-1), null, 2));

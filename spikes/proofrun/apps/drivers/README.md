# Driver apps (SSC-040)

Each app connects once at start with `DATABASE_URL` exactly as the platform gives it, prints one
`DRIVER_RESULT {...}` line (read with `ssc logs <app> --env preview`), then answers `/health`.
Deployed to the preview of pg01 (node-pg), pg02 (Prisma 7) and pg03 (Django), which already hold
databases; the connection string is never printed.

`hold/` holds both of the app's allowed connections and prints `HOLD_RESULT` every 30 s. Run
2026-10-07 on pg01 to pg10: a preview with no requests sleeps about 3 minutes after it starts, so
all ten were woken together with `ssc database rotate` (a redeploy, no build), and one more
rotation, which is the cell agent's administration connection, ran while every role was at its
limit (its new instance refused with `too many connections for role`).

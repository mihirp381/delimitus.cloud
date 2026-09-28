#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export DATABASE_URL="$(cut -d= -f2- drivers/.env)"
uv run python drivers/py_drivers.py drivers/py_results.json >/dev/null
(cd drivers/node && node node_drivers.mjs ../node_results.json >/dev/null && node prisma_check.mjs ../node_results.json >/dev/null)
uv run python - <<'PY'
import json
rows=json.load(open("drivers/py_results.json"))+json.load(open("drivers/node_results.json"))
lines=["# Driver matrix (SSC-005)","","URL under test, unchanged for every driver:","","`postgresql://app_<id>:<pw>@localhost:55418/app_<id>?sslmode=verify-full&sslrootcert=<abs path to ca.crt>`","","| Driver | Version | Accepted URL as given | What was needed | Error text |","|---|---|---|---|---|"]
for r in rows: lines.append(f"| {r['driver']} | {r['version']} | {'yes' if r['accepted_as_given'] else 'no'} | {r['needed']} | {r['error'] or '-'} |")
open("drivers/RESULTS.md","w").write("\n".join(lines)+"\n")
print("\n".join(lines))
PY

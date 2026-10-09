#!/usr/bin/env bash
# Finishes GA-4 on cell 2 (org 2, proofcell02, logged in as admin2). Run it once, in your own
# Terminal:  bash spikes/proofrun/finish_ga4.sh
# Every step runs whatever the one before it did. Each step's output is in results/<step>.log.

set -u

ORG=org_xbtt2c4ulcxztq6s0518
LABEL=proofcell02
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
KIT=$ROOT/spikes/proofrun
RES=$KIT/results
EXITS=$RES/finish_ga4.exits
exec 3>&1

cd "$ROOT" || exit 1
mkdir -p "$RES"
: > "$EXITS"

stamp() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

seconds_now() {
  python3 -c 'import time;print(time.time())'
}

kit() {
  (cd "$KIT" && uv run python -m proofrun "$@")
}

run_logged() {
  local name=$1
  shift
  echo "[$(stamp)] START $name" >&3
  "$@" 2>&1 | tee "$RES/$name.log"
  local code=${PIPESTATUS[0]}
  echo "$name $code" >> "$EXITS"
  echo "[$(stamp)] END   $name  exit code $code" >&3
}

last_line() {
  grep -v '^[[:space:]]*$' "$RES/$1.log" 2> /dev/null | tail -n 1
}

exit_of() {
  awk -v n="$1" '$1 == n { c = $2 } END { print (c == "" ? "not run" : c) }' "$EXITS"
}

places_left() {
  uv run ssc status ga4rb --json 2> /dev/null | python3 -c '
import json, sys
rows = [e["database"] for e in json.load(sys.stdin)["environments"] if e.get("database")]
rows = [d for d in rows if d.get("places_total") is not None]
print(rows[0]["places_total"] - rows[0]["places_used"] if rows else "?")
'
}

echo "=============================================================="
echo "GA-4 finish: cell 2 ($LABEL), org $ORG"
echo "Started $(stamp). Expect 1.5 to 2.5 hours. Keep this window open."
echo "=============================================================="
echo
echo "TWO CONSOLE CHECKS FOR YOU TO DO WHILE THIS RUNS (the script cannot do them):"
echo
echo "  A. GA-4.5 rollback dialog. Wait until step ga-4.5-run3 prints a line starting"
echo "     'console (manual): open ...'. Then open that address as admin2, press Roll back on"
echo "     the Preview card, pick the first release, type the app name, do NOT tick the"
echo "     checkbox. The dialog must warn about 0002_ga45_second. Press Cancel. Screenshot the"
echo "     dialog to spikes/proofrun/results/ga-4.5-console.png"
echo
echo "  B. GA-4.8 Warm option panel. Any time after the 'warm' step has started. Console as"
echo "     admin2, your environment screen, Warm option panel: tick ga4warm, read the monthly"
echo "     figure (should be \$10), untick it, do NOT press Save. Screenshot the ticked box and"
echo "     the figure to spikes/proofrun/results/ga-4.8-console.png"
echo
echo "Step order below. Exit codes are printed and listed again in the summary at the end."
echo

if command -v caffeinate > /dev/null 2>&1; then
  caffeinate -i -w $$ &
fi

echo "[$(stamp)] START precheck"
who=$(uv run ssc whoami --json 2>&1)
echo "$who" | grep -E '"(org_id|subject|role|kind)"'
if ! echo "$who" | grep -q "\"$ORG\""; then
  echo "STOP: the CLI is not logged in to $ORG. Run: uv run ssc login   (as admin2), then start again."
  exit 1
fi
echo "OK: logged in to $ORG"
for slug in ga4warm ga4rb ga4secret ga4files; do
  if uv run ssc apps --json 2> /dev/null | grep -q "\"slug\": \"$slug\""; then
    echo "OK: app $slug exists"
  else
    echo "STOP: app $slug does not exist yet (README: create and deploy it first)."
    exit 1
  fi
done
kit cookie list 2>&1 | sed 's/^/cookie jar: /'
echo "[$(stamp)] END   precheck"
echo

echo "[$(stamp)] START ga-4.8-run2 in the background (touches only ga4warm; about 45 minutes)"
(
  kit warm --app ga4warm --label "$LABEL" > "$RES/ga-4.8-run2.log" 2>&1
  code=$?
  echo "ga-4.8-run2 $code" >> "$EXITS"
  echo "[$(stamp)] END   ga-4.8-run2  exit code $code" >&3
) &
warm_pid=$!

run_logged ga-4.5-run3 kit rollback --app ga4rb

run_logged ga-4.6-run kit secrets --app ga4secret --label "$LABEL"

run_logged ga-4.2-run kit files --app ga4files --label "$LABEL" --wait-expiry --disable

echo
echo "---- GA-4.3: rotate, then prod and the recovery point, then fill the database to the top ----"
echo "Why this order: ga4rb holds 1 place (preview). Prod takes a second place. So the"
echo "rotate and the promote come first, then the filler apps use the places that are left,"
echo "then one more app is the eleventh and must get DB_TIER_FULL."
echo

run_logged ga-4.3-status-before uv run ssc status ga4rb

rotate_timed() {
  local t0 t1 code
  t0=$(seconds_now)
  uv run ssc database rotate ga4rb --env preview
  code=$?
  t1=$(seconds_now)
  python3 -c 'import sys; s = float(sys.argv[2]) - float(sys.argv[1]); print("ROTATE TOOK %.2f seconds: %s (limit 5 s)" % (s, "PASS" if s < 5 else "FAIL"))' "$t0" "$t1"
  echo "rotate exit code $code"
  return "$code"
}
run_logged ga-4.3-rotate rotate_timed

rotate_healthy() {
  local i newest
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    newest=$(uv run python spikes/proofrun/ga4_read.py deployments ga4rb preview | head -n 1)
    echo "$newest"
    if echo "$newest" | grep -q ' healthy '; then
      echo "APP HEALTHY AFTER ROTATE: newest preview deployment is healthy"
      uv run ssc status ga4rb
      return 0
    fi
    sleep 10
  done
  echo "APP NOT HEALTHY 2 minutes after the rotate: newest preview deployment is not healthy"
  uv run ssc status ga4rb
  return 1
}
run_logged ga-4.3-health-after-rotate rotate_healthy

run_logged ga-4.3-promote uv run ssc promote ga4rb --wait --timeout 1800

recovery_point() {
  local i
  for i in 1 2 3 4 5 6; do
    uv run python spikes/proofrun/ga4_read.py recovery ga4rb && return 0
    sleep 10
  done
  return 1
}
run_logged ga-4.3-recovery-point recovery_point

echo
echo "NOTE: the CLI and the console do not show the recovery point. The step above reads it from"
echo "the control API (GET .../environments/<prod id>/deployments), using your CLI login."
echo

left=$(places_left)
echo "Database places left on the instance after ga4rb preview and prod: $left"
if ! [ "$left" -ge 0 ] 2> /dev/null || [ "$left" -gt 9 ]; then
  echo "Could not read a sensible number of free places ($left). Using 8 (10 minus ga4rb's two)."
  left=8
fi

fill_one() {
  uv run ssc apps create "$1"
  uv run ssc deploy --app "$1" spikes/proofrun/apps/rollback --wait --timeout 1500
}

fill_pids=""
n=1
while [ "$n" -le "$left" ]; do
  slug=$(printf 'ga4db%02d' "$n")
  (run_logged "ga-4.3-fill-$slug" fill_one "$slug" > /dev/null 2>&1) &
  fill_pids="$fill_pids $!"
  n=$((n + 1))
done
echo "[$(stamp)] $left filler apps are deploying in parallel (ga4db01 to $(printf 'ga4db%02d' "$left"))"
for pid in $fill_pids; do
  wait "$pid"
done
echo "[$(stamp)] filler deploys finished. Their exit codes:"
grep '^ga-4.3-fill-' "$EXITS"

run_logged ga-4.3-status-full uv run ssc status ga4rb

eleventh=$(printf 'ga4db%02d' $((left + 1)))
run_logged "ga-4.3-eleventh-$eleventh" fill_one "$eleventh"
if grep -q DB_TIER_FULL "$RES/ga-4.3-eleventh-$eleventh.log"; then
  echo "ELEVENTH ENVIRONMENT REFUSED WITH DB_TIER_FULL: PASS. The hint text is in results/ga-4.3-eleventh-$eleventh.log:"
  grep -i -B1 -A4 'DB_TIER_FULL' "$RES/ga-4.3-eleventh-$eleventh.log" | head -n 20
else
  echo "ELEVENTH ENVIRONMENT DID NOT GET DB_TIER_FULL: FAIL (read results/ga-4.3-eleventh-$eleventh.log)"
fi

ga49() {
  echo "--- doctor on the SQLite fixture (expect BLOCK STATE_SQLITE_EPHEMERAL, exit 4)"
  uv run ssc doctor spikes/proofrun/apps/sqlite_disk
  echo "doctor exit code $?"
  echo "--- doctor on the home-folder fixture (expect WARN WRITES_HOME, exit 0: a warning, not a refusal)"
  uv run ssc doctor spikes/proofrun/apps/disk_write
  echo "doctor exit code $?"
  uv run ssc apps create ga4sqlite
  uv run ssc apps create ga4diskw
  echo "--- deploy of the SQLite fixture (expect refusal STATE_SQLITE_EPHEMERAL before any upload)"
  uv run ssc deploy --app ga4sqlite spikes/proofrun/apps/sqlite_disk --wait --timeout 600 2>&1 | tee "$RES/ga-4.9-sqlite-deploy.txt"
  echo "deploy exit code ${PIPESTATUS[0]}"
  if grep -q STATE_SQLITE_EPHEMERAL "$RES/ga-4.9-sqlite-deploy.txt"; then
    echo "SQLITE FIXTURE REFUSED WITH STATE_SQLITE_EPHEMERAL: PASS"
  else
    echo "SQLITE FIXTURE WAS NOT REFUSED WITH STATE_SQLITE_EPHEMERAL: FAIL"
  fi
  echo "--- deploy of the home-folder fixture (WRITES_HOME is only a doctor warning, so this deploy is expected to go through)"
  uv run ssc deploy --app ga4diskw spikes/proofrun/apps/disk_write --wait --timeout 900
  echo "deploy exit code $?"
}
run_logged ga-4.9-run ga49

echo
echo "[$(stamp)] waiting for the 4.8 warm run (pid $warm_pid)"
wait "$warm_pid"

echo
echo "=============================================================="
echo "SUMMARY at $(stamp)"
echo "=============================================================="
for name in ga-4.8-run2 ga-4.5-run3 ga-4.6-run ga-4.2-run ga-4.3-status-before ga-4.3-rotate ga-4.3-health-after-rotate ga-4.3-promote ga-4.3-recovery-point ga-4.3-status-full "ga-4.3-eleventh-$eleventh" ga-4.9-run; do
  printf '%-34s exit %-8s %s\n' "$name" "$(exit_of "$name")" "$(last_line "$name")"
done
for pid_name in $(awk '$1 ~ /^ga-4.3-fill-/ { print $1 }' "$EXITS"); do
  printf '%-34s exit %-8s %s\n' "$pid_name" "$(exit_of "$pid_name")" "$(last_line "$pid_name")"
done
echo
echo "Exit code 0 means the command finished cleanly. For the proof steps the PASS or FAIL word"
echo "is in the last line. The eleventh-app step and the SQLite deploy are SUPPOSED to fail"
echo "(non-zero exit): what matters is the PASS lines printed above."
echo "Still to do by hand: console checks A and B at the top. Do not save the warm option."

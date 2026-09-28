import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx2


def main():
    p = argparse.ArgumentParser(description="Run a cut-off command, then time until the app stops answering.")
    p.add_argument("--url", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--candidate", default=None)
    p.add_argument("--max-seconds", type=float, default=120)
    a = p.parse_args()

    with httpx2.Client(timeout=5) as c:
        pre = c.get(a.url).status_code
        if not 200 <= pre < 300:
            raise SystemExit(f"app not healthy before test: {pre}")
        t0 = time.perf_counter()
        subprocess.run(a.command, shell=True, check=True)
        cmd_s = time.perf_counter() - t0
        last = pre
        while time.perf_counter() - t0 < a.max_seconds:
            try:
                last = c.get(a.url).status_code
            except Exception as e:
                last = type(e).__name__
            if not (isinstance(last, int) and 200 <= last < 300):
                break
            time.sleep(0.25)
        cut_s = round(time.perf_counter() - t0, 2)

    out = {"cut_off_s": cut_s, "command_s": round(cmd_s, 2), "final_status": last,
           "result": "pass" if cut_s < 10 else "fail"}
    print(json.dumps(out))
    if a.candidate:
        path = Path(f"results/{a.candidate}.json")
        data = json.loads(path.read_text()) if path.exists() else {"candidate": a.candidate, "checks": {}}
        data["checks"]["kill"] = out
        path.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()

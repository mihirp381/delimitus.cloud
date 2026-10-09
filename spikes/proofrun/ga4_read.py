"""Two reads for GA-4.3 that the CLI does not show (the CLI drops the recovery point).

Run at the repository root, so ``ssc_cli`` is importable and the CLI's own login is used:

    uv run python spikes/proofrun/ga4_read.py deployments <slug> <prod|preview>
    uv run python spikes/proofrun/ga4_read.py recovery <slug>

``deployments`` prints each deployment of the environment, newest first. ``recovery`` prints the
newest prod deployment's recovery point and exits 0 only when it holds a write-ahead log position
(LSN). The token stays in the request header; it is never printed.
"""

import json
import re
import sys
import urllib.request
from typing import Any

from ssc_cli.config import load_config
from ssc_cli.credentials import read_token

LSN = re.compile(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}")


def get(api: str, token: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310  (the CLI's own https API address)
        api + path, headers={"Authorization": f"Bearer {token}", "User-Agent": "ssc-proofrun/0.0.1"}
    )
    with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
        return json.loads(answer.read())


def deployments(slug: str, env: str) -> list[dict[str, Any]]:
    api = load_config().api_url
    token = read_token(api)
    apps = get(api, token, "/v1/apps")["apps"]
    app = next(a for a in apps if a["slug"] == slug)
    detail = get(api, token, f"/v1/apps/{app['id']}")
    env_id = next(e["id"] for e in detail["environments"] if e["name"] == env)
    return get(api, token, f"/v1/apps/{app['id']}/environments/{env_id}/deployments")["items"]


def show(items: list[dict[str, Any]]) -> None:
    for d in items:
        point = d.get("recovery_point") or {}
        print(
            f"{d['operation_id']} {d['kind']} {d['state']} release {d['release_number']} "
            f"current {d['current']} recovery_at {point.get('at')} recovery_lsn {point.get('lsn')}"
        )


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "deployments":
        show(deployments(argv[1], argv[2]))
        return 0
    if len(argv) == 2 and argv[0] == "recovery":
        items = deployments(argv[1], "prod")
        show(items[:3])
        point = (items[0].get("recovery_point") or {}) if items else {}
        lsn = point.get("lsn")
        if lsn and LSN.fullmatch(lsn):
            print(f"recovery point: at {point['at']} lsn {lsn}  (a real WAL position)")
            return 0
        print(f"recovery point: no WAL position on the newest prod deployment ({point or 'none'})")
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

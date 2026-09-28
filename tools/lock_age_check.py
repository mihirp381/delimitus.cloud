"""Fail if uv.lock pins any package uploaded to PyPI less than MIN_AGE_DAYS ago.

Exceptions live in docs/lock-exceptions.toml: [[exception]] name, version, reason, expires.
"""

import json
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

MIN_AGE_DAYS = 7
ROOT = Path(__file__).resolve().parent.parent


def upload_time(name: str, version: str) -> datetime | None:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310
        data = json.load(r)
    files = data.get("urls") or []
    if not files:
        return None
    return min(datetime.fromisoformat(f["upload_time_iso_8601"]) for f in files)


def load_exceptions(path: Path, now: datetime) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    out: dict[tuple[str, str], str] = {}
    for e in tomllib.loads(path.read_text()).get("exception", []):
        expires = datetime.fromisoformat(e["expires"]).replace(tzinfo=UTC)
        if expires < now:
            print(f"EXPIRED exception for {e['name']}=={e['version']}: {e['reason']}")  # noqa: T201
            continue
        out[(e["name"].lower(), e["version"])] = e["reason"]
    return out


def main(lock_path: Path, exceptions_path: Path) -> int:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=MIN_AGE_DAYS)
    exceptions = load_exceptions(exceptions_path, now)
    lock = tomllib.loads(lock_path.read_text())
    failures = 0
    for pkg in lock.get("package", []):
        source = pkg.get("source", {})
        if "registry" not in source:
            continue
        name, version = pkg["name"].lower(), pkg["version"]
        try:
            uploaded = upload_time(name, version)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN {name}=={version}: could not query PyPI ({exc})")  # noqa: T201
            continue
        if uploaded is None or uploaded <= cutoff:
            continue
        age = (now - uploaded).days
        if (name, version) in exceptions:
            print(f"EXCEPTION {name}=={version} ({age} d old): {exceptions[(name, version)]}")  # noqa: T201
            continue
        when = f"{uploaded:%Y-%m-%d}"
        print(f"FAIL {name}=={version} uploaded {when} ({age} d old, need {MIN_AGE_DAYS})")  # noqa: T201
        failures += 1
    print(f"lock age check: {failures} failure(s)")  # noqa: T201
    return 1 if failures else 0


if __name__ == "__main__":
    lock = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "uv.lock"
    exc = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "lock-exceptions.toml"
    raise SystemExit(main(lock, exc))

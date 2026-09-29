"""Fail if console/package-lock.json pins any npm version published less than MIN_AGE_DAYS ago.

It also fails on a package fetched from anywhere but the npm registry, since the age rule
cannot see it. Exceptions live in docs/lock-exceptions.toml, in the same format as
tools/lock_age_check.py: [[exception]] name, version, reason, expires.
"""

import json
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lock_age_check import MIN_AGE_DAYS, load_exceptions

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://registry.npmjs.org/"

Fetch = Callable[[str], dict[str, str]]


def publish_times(name: str) -> dict[str, str]:
    """The registry's `time` map for one package: version -> ISO timestamp."""
    url = REGISTRY + urllib.parse.quote(name, safe="@")
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310
        return json.load(r).get("time", {})


def locked_packages(lock: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """(name, version) for every registry package, and the paths fetched from elsewhere."""
    found: set[tuple[str, str]] = set()
    elsewhere: list[str] = []
    for path, entry in lock.get("packages", {}).items():
        if not path or entry.get("link"):
            continue
        resolved = entry.get("resolved", "")
        if not resolved.startswith(REGISTRY):
            elsewhere.append(f"{path} ({resolved or 'no resolved URL'})")
            continue
        name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
        found.add((name, entry["version"]))
    return sorted(found), elsewhere


def main(lock_path: Path, exceptions_path: Path, fetch: Fetch = publish_times) -> int:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=MIN_AGE_DAYS)
    exceptions = load_exceptions(exceptions_path, now)
    packages, elsewhere = locked_packages(json.loads(lock_path.read_text()))
    failures = 0
    for where in elsewhere:
        print(f"FAIL {where}: not from {REGISTRY}")  # noqa: T201
        failures += 1

    def times(name: str) -> dict[str, str] | Exception:
        try:
            return fetch(name)
        except Exception as exc:  # noqa: BLE001
            return exc

    names = sorted({name for name, _ in packages})
    with ThreadPoolExecutor(max_workers=8) as pool:
        by_name = dict(zip(names, pool.map(times, names), strict=True))
    for name, version in packages:
        known = by_name[name]
        if isinstance(known, Exception):
            print(f"WARN {name}@{version}: could not query the registry ({known})")  # noqa: T201
            continue
        stamp = known.get(version)
        if stamp is None:
            print(f"FAIL {name}@{version}: the registry lists no publish time")  # noqa: T201
            failures += 1
            continue
        published = datetime.fromisoformat(stamp)
        if published <= cutoff:
            continue
        age = (now - published).days
        if (name.lower(), version) in exceptions:
            reason = exceptions[(name.lower(), version)]
            print(f"EXCEPTION {name}@{version} ({age} d old): {reason}")  # noqa: T201
            continue
        when = f"{published:%Y-%m-%d}"
        print(f"FAIL {name}@{version} published {when} ({age} d old, need {MIN_AGE_DAYS})")  # noqa: T201
        failures += 1
    print(f"npm lock age check: {len(packages)} package(s), {failures} failure(s)")  # noqa: T201
    return 1 if failures else 0


if __name__ == "__main__":
    lock = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "console" / "package-lock.json"
    exc = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "lock-exceptions.toml"
    raise SystemExit(main(lock, exc))

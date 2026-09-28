"""Local record store. Holds identities and timestamps only. Never tokens or keys."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from loginproof.config import OUT_DIR

RECORDS = OUT_DIR / "records.json"

EMPTY: dict[str, Any] = {"logins": [], "directories": {}, "device": {}}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load() -> dict[str, Any]:
    if not RECORDS.exists():
        return json.loads(json.dumps(EMPTY))
    return json.loads(RECORDS.read_text())


def save(data: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS.write_text(json.dumps(data, indent=2, sort_keys=True))


def add_login(record: dict[str, Any]) -> None:
    data = load()
    data["logins"].append({"ts": now(), **record})
    save(data)


def set_directory(provider: str, snapshot: dict[str, Any]) -> None:
    data = load()
    data["directories"][provider] = {"ts": now(), **snapshot}
    save(data)


def set_device(key: str, record: dict[str, Any]) -> None:
    data = load()
    data["device"][key] = {"ts": now(), **record}
    save(data)

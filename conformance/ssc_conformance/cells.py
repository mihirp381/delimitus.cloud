"""The cells of a nightly run (SSC-056): ``python -m ssc_conformance.cells <section>``.

``SSC_NIGHT_CELLS`` is a repository variable holding a JSON array, one object per cell, and this is
the one place it is read. Every nightly workflow gets its settings from here, as ``NAME=value``
lines for ``$GITHUB_ENV`` or ``$GITHUB_OUTPUT``, never from ad-hoc ``jq``. A missing or unknown key
fails with a message naming the cell and the key.

Per cell, all required unless marked: ``project`` (the cell project, also what the cell is called
on the page), ``label`` (shown on the page), ``agent_url``, ``app_url`` and ``gateway_url`` (the
``run.app`` URLs of the cell's agent, a probe app's and the gateway), ``range`` (the cell's address
range), ``org``, ``base`` (the apps base host), ``auth_url``, ``username`` (its Okta test admin),
``datagw_url`` and ``datagw_connection`` (optional, both or neither; the connection is a
``con_<20>`` id) and ``drill`` (optional): ``api_url``, ``app_id``, ``env_id`` and ``host`` of the
kill switch drill app. The test admin's password is a secret chosen by the cell's position:
``SSC_NIGHT_CELL1_PASSWORD`` for the first, ``SSC_NIGHT_CELL2_PASSWORD`` for the second.

Sections: ``plan`` (``count``, ``projects``, ``positions`` and ``peer``, for the job that fans
out), ``probes``, ``browser`` and ``drill`` (the environment of that job for the cell at
``--position N``, counted from 1 as the password secrets are). With two cells each is the other's
peer.
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

ENV: Final = "SSC_NIGHT_CELLS"
MAX_CELLS: Final = 2
REQUIRED: Final = (
    "project",
    "label",
    "agent_url",
    "app_url",
    "gateway_url",
    "range",
    "org",
    "base",
    "auth_url",
    "username",
)
OPTIONAL: Final = ("datagw_url", "datagw_connection", "drill")
DRILL_KEYS: Final = ("api_url", "app_id", "env_id", "host")
CONNECTION_ID: Final = re.compile(r"con_[a-z0-9]{20}")
URLS: Final = ("agent_url", "app_url", "gateway_url", "auth_url", "datagw_url")
SECTIONS: Final = ("plan", "probes", "browser", "drill")
BROWSER_APPS: Final = ("alpha", "bravo")


class CellsError(Exception):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Drill:
    api_url: str
    app_id: str
    env_id: str
    host: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Cell:
    project: str
    label: str
    agent_url: str
    app_url: str
    gateway_url: str
    range: str
    org: str
    base: str
    auth_url: str
    username: str
    datagw_url: str | None = None
    datagw_connection: str | None = None
    drill: Drill | None = None

    @property
    def hosts(self) -> list[str]:
        return [f"{app}.{self.base}" for app in BROWSER_APPS]


def _text(where: str, body: Mapping[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CellsError(f'{where}: missing "{key}"')
    if "\n" in value or "\r" in value:
        raise CellsError(f'{where}: "{key}" has a line break')
    return value.strip()


def _drill(where: str, raw: object) -> Drill:
    if not isinstance(raw, dict):
        raise CellsError(f'{where}: "drill" is not an object')
    body: dict[str, Any] = raw
    inside = f"{where}.drill"
    unknown = sorted(set(body) - set(DRILL_KEYS))
    if unknown:
        raise CellsError(f"{inside}: unknown key {unknown[0]!r}")
    values = {key: _text(inside, body, key) for key in DRILL_KEYS}
    _https(inside, "api_url", values["api_url"])
    return Drill(**values)


def _https(where: str, key: str, value: str) -> None:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise CellsError(f'{where}: "{key}" is not an https URL')


def _cell(index: int, raw: object) -> Cell:
    where = f"{ENV}[{index}]"
    if not isinstance(raw, dict):
        raise CellsError(f"{where}: not an object")
    body: dict[str, Any] = raw
    unknown = sorted(set(body) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise CellsError(f"{where}: unknown key {unknown[0]!r}")
    values = {key: _text(where, body, key) for key in REQUIRED}
    for key in URLS:
        if key in values:
            _https(where, key, values[key])
    optional: dict[str, Any] = {}
    for key in ("datagw_url", "datagw_connection"):
        if key in body:
            optional[key] = _text(where, body, key)
    if ("datagw_url" in optional) != ("datagw_connection" in optional):
        raise CellsError(f'{where}: set both "datagw_url" and "datagw_connection", or neither')
    if "datagw_url" in optional:
        _https(where, "datagw_url", optional["datagw_url"])
        if CONNECTION_ID.fullmatch(optional["datagw_connection"]) is None:
            raise CellsError(f'{where}: "datagw_connection" is not a con_<20> connection id')
    if "drill" in body:
        optional["drill"] = _drill(where, body["drill"])
    return Cell(**values, **optional)


def parse(text: str | None) -> list[Cell]:
    """The cells ``SSC_NIGHT_CELLS`` holds, or ``CellsError`` saying what is wrong with it."""
    if text is None or not text.strip():
        raise CellsError(f"{ENV} is not set")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise CellsError(f"{ENV} is not valid JSON ({type(exc).__name__})") from None
    if not isinstance(raw, list) or not raw:
        raise CellsError(f"{ENV} is not a non-empty array")
    if len(raw) > MAX_CELLS:
        raise CellsError(f"{ENV} has {len(raw)} cells; a night takes at most {MAX_CELLS}")
    cells = [_cell(i, item) for i, item in enumerate(raw)]
    projects = [c.project for c in cells]
    if len(set(projects)) != len(projects):
        raise CellsError(f"{ENV}: two cells name the same project")
    return cells


def peer_of(cells: Sequence[Cell], index: int) -> Cell | None:
    """The other cell of a two-cell night; none on a one-cell night."""
    return cells[(index + 1) % len(cells)] if len(cells) > 1 else None


def _auth(cell: Cell) -> dict[str, str]:
    return {
        "SSC_NIGHT_AUTH_URL": cell.auth_url,
        "SSC_NIGHT_ORG": cell.org,
        "SSC_NIGHT_USERNAME": cell.username,
    }


def section(cells: Sequence[Cell], name: str, index: int = 0) -> dict[str, str]:
    """The ``NAME=value`` lines of ``name`` for cell ``index``."""
    if name == "plan":
        return {
            "count": str(len(cells)),
            "projects": ",".join(c.project for c in cells),
            "positions": json.dumps(list(range(1, len(cells) + 1))),
            "peer": "true" if len(cells) > 1 else "false",
        }
    if not 0 <= index < len(cells):
        raise CellsError(f"--position {index + 1} is not one of the {len(cells)} cells")
    cell, peer = cells[index], peer_of(cells, index)
    if name == "probes":
        out = {
            "SSC_PROBE_PROJECT": cell.project,
            "SSC_PROBE_AGENT_URL": cell.agent_url,
            "SSC_PROBE_ORG_ID": cell.org,
            "SSC_PROBE_TLS_HOST": f"alpha.{cell.base}",
        }
        if peer is not None:
            out |= {
                "SSC_PROBE_PEER_APP_URL": peer.app_url,
                "SSC_PROBE_PEER_GATEWAY_URL": peer.gateway_url,
                "SSC_PROBE_PEER_RANGE": peer.range,
                "SSC_PROBE_PEER_PROJECT": peer.project,
            }
        if cell.datagw_url and cell.datagw_connection:
            out |= {
                "SSC_PROBE_DATAGW_URL": cell.datagw_url,
                "SSC_PROBE_DATAGW_CONNECTION": cell.datagw_connection,
            }
        return out
    if name == "browser":
        out = {
            "SSC_ISO_CELL1_BASE": cell.base,
            "SSC_ISO_AUTH_URL": cell.auth_url,
            "SSC_ISO_GATEWAY_RUN_APP": urlsplit(cell.gateway_url).netloc,
            "SSC_NIGHT_HOSTS": ",".join(cell.hosts),
            "SSC_NIGHT_PROJECT": cell.project,
            "SSC_NIGHT_PEER": "true" if peer is not None else "false",
            **_auth(cell),
        }
        if peer is not None:
            out["SSC_ISO_PEER_BASE"] = peer.base
        return out
    if name == "drill":
        if cell.drill is None:
            raise CellsError(f'{ENV}[{index}]: no "drill" object')
        d = cell.drill
        return {
            "SSC_DRILL_API_URL": d.api_url,
            "SSC_DRILL_APP_ID": d.app_id,
            "SSC_DRILL_ENV_ID": d.env_id,
            "SSC_DRILL_HOST": d.host,
            "SSC_DRILL_PROJECT": cell.project,
            "SSC_DRILL_ORG_ID": cell.org,
            "SSC_NIGHT_HOSTS": ",".join([*cell.hosts, d.host]),
            **_auth(cell),
        }
    raise CellsError(f"unknown section {name!r}")


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("section", choices=SECTIONS)
    parser.add_argument("--position", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        cells = parse((environ if environ is not None else os.environ).get(ENV))
        lines = section(cells, args.section, args.position - 1)
    except CellsError as exc:
        sys.stderr.write(f"cells: {exc}\n")
        return 1
    for name, value in lines.items():
        sys.stdout.write(f"{name}={value}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

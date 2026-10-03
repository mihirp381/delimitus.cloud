"""The migration ledgers in the source (SSC-043).

Stdlib only, no execution, like ``analyze``: the files of a stored bundle in, the migrations of
each common ledger tool out, named as the tool names them and in the order it applies them, so
the last name of each ledger is its latest:

- ``prisma``: ``<dir>/migrations/<name>/migration.sql`` beside ``migration_lock.toml`` or under a
  ``prisma`` folder; the folder name, sorted (Prisma starts each name with a timestamp).
- ``alembic``: ``<dir>/versions/*.py`` beside ``<dir>/env.py``; each file's ``revision``, after
  every ``down_revision`` it names (a file too large to read is named by its stem).
- ``django``: ``<app>/migrations/NNNN_<name>.py`` beside its ``__init__.py``, in a project with a
  ``manage.py``; ``<app>.NNNN_<name>``, sorted.
- ``drizzle``: ``meta/_journal.json``; each entry's ``tag``, in ``idx`` order.
- ``knex``: the ``.js``, ``.ts``, ``.cjs`` and ``.mjs`` files directly in the folder a ``knexfile``
  names as its migrations ``directory`` (``migrations`` beside it by default); the file name,
  sorted, as Knex records it.

Nothing under ``node_modules``, a virtual environment or ``__pycache__`` counts, and a name
longer than ``MAX_NAME`` is left out. A ledger with no migration is absent.
"""

import heapq
import json
import re
from collections.abc import Iterable, Mapping
from typing import Final, cast

LEDGERS: Final = ("prisma", "alembic", "django", "drizzle", "knex")
MAX_NAME: Final = 255

type Ledgers = dict[str, tuple[str, ...]]

_SKIP = re.compile(r"(?:^|/)(?:node_modules|\.venv|venv|site-packages|__pycache__)/")
_PRISMA = re.compile(r"^(?P<dir>(?:.*/)?migrations)/(?P<name>[^/]+)/migration\.sql$")
_PRISMA_HOME = re.compile(r"(?:^|/)prisma/migrations$")
_ALEMBIC = re.compile(r"^(?P<dir>(?:.*/)?)versions/(?P<stem>[^/]+)\.py$")
_DJANGO = re.compile(
    r"^(?P<dir>(?:.*/)?(?P<app>[A-Za-z_][A-Za-z0-9_]*)/migrations)/"
    r"(?P<name>[0-9]{4}_[A-Za-z0-9_]+)\.py$"
)
_DRIZZLE = re.compile(r"^(?:.*/)?meta/_journal\.json$")
_KNEXFILE = re.compile(r"^(?P<dir>(?:.*/)?)knexfile\.(?:js|ts|cjs|mjs)$")
_KNEX_DIRECTORY = re.compile(
    r"""\bmigrations\s*:\s*\{[^{}]*?\bdirectory\s*:\s*["'`]([^"'`]+)["'`]"""
)
_KNEX_SUFFIXES: Final = (".js", ".ts", ".cjs", ".mjs")
_REVISION = re.compile(r"""^revision\s*(?::[^=\n]*)?=\s*["']([^"']+)["']""", re.MULTILINE)
_DOWN_REVISION = re.compile(
    r"""^down_revision\s*(?::[^=\n]*)?=\s*(\([^)]*\)|\[[^\]]*\]|[^\n]*)""", re.MULTILINE
)
_QUOTED = re.compile(r"""["']([^"']+)["']""")


def ledgers(files: Mapping[str, bytes | None]) -> Ledgers:
    """Each ledger found in ``files`` (POSIX path, content or None) with its migrations."""
    paths = sorted(p for p in files if not _SKIP.search(p))
    found = {
        "prisma": _prisma(paths),
        "alembic": _alembic(paths, files),
        "django": _django(paths),
        "drizzle": _drizzle(paths, files),
        "knex": _knex(paths, files),
    }
    out: Ledgers = {}
    for ledger, names in found.items():
        kept = tuple(n for n in names if 0 < len(n) <= MAX_NAME)
        if kept:
            out[ledger] = kept
    return out


def _text(files: Mapping[str, bytes | None], path: str) -> str | None:
    data = files.get(path)
    return None if data is None else data.decode("utf-8", errors="replace")


def _prisma(paths: list[str]) -> list[str]:
    present = set(paths)
    names: set[str] = set()
    for path in paths:
        m = _PRISMA.match(path)
        if m is None:
            continue
        folder = m["dir"]
        if f"{folder}/migration_lock.toml" in present or _PRISMA_HOME.search(folder):
            names.add(m["name"])
    return sorted(names)


def _alembic(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    present = set(paths)
    parents: dict[str, frozenset[str]] = {}
    for path in paths:
        m = _ALEMBIC.match(path)
        if m is None or m["stem"] == "__init__" or f"{m['dir']}env.py" not in present:
            continue
        text = _text(files, path)
        revision = _REVISION.search(text) if text is not None else None
        if revision is None:
            parents.setdefault(m["stem"], frozenset())
            continue
        down = _DOWN_REVISION.search(text) if text is not None else None
        named = frozenset(_QUOTED.findall(down[1])) if down is not None else frozenset[str]()
        parents[revision[1]] = parents.get(revision[1], frozenset()) | named
    return _in_order(parents)


def _in_order(parents: Mapping[str, Iterable[str]]) -> list[str]:
    """Each revision after the revisions it names that are here; ties and cycles by name."""
    waiting: dict[str, int] = {}
    children: dict[str, list[str]] = {r: [] for r in parents}
    for revision, named in parents.items():
        known = {p for p in named if p in parents and p != revision}
        waiting[revision] = len(known)
        for parent in known:
            children[parent].append(revision)
    ready = [r for r, n in waiting.items() if n == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        revision = heapq.heappop(ready)
        order.append(revision)
        for child in children[revision]:
            waiting[child] -= 1
            if waiting[child] == 0:
                heapq.heappush(ready, child)
    placed = set(order)
    return order + sorted(r for r in parents if r not in placed)


def _django(paths: list[str]) -> list[str]:
    if not any(p == "manage.py" or p.endswith("/manage.py") for p in paths):
        return []
    present = set(paths)
    names: set[str] = set()
    for path in paths:
        m = _DJANGO.match(path)
        if m is not None and f"{m['dir']}/__init__.py" in present:
            names.add(f"{m['app']}.{m['name']}")
    return sorted(names)


def _drizzle(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    tagged: list[tuple[int, str]] = []
    for path in paths:
        if _DRIZZLE.match(path) is None:
            continue
        try:
            journal: object = json.loads(_text(files, path) or "")
        except ValueError:
            continue
        if not isinstance(journal, dict):
            continue
        entries = cast("dict[str, object]", journal).get("entries")
        if not isinstance(entries, list):
            continue
        for entry in cast("list[object]", entries):
            if not isinstance(entry, dict):
                continue
            fields = cast("dict[str, object]", entry)
            idx, tag = fields.get("idx"), fields.get("tag")
            if isinstance(idx, int) and not isinstance(idx, bool) and isinstance(tag, str):
                tagged.append((idx, tag))
    return list(dict.fromkeys(tag for _, tag in sorted(tagged)))


def _knex(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    folders: set[str] = set()
    for path in paths:
        m = _KNEXFILE.match(path)
        if m is None:
            continue
        text = _text(files, path) or ""
        named = _KNEX_DIRECTORY.findall(text) or ["migrations"]
        for directory in named:
            relative = directory.strip().removeprefix("./").strip("/")
            if relative and ".." not in relative.split("/"):
                folders.add(f"{m['dir']}{relative}/")
    names: set[str] = set()
    for path in paths:
        for folder in folders:
            rest = path.removeprefix(folder)
            if (
                rest != path
                and "/" not in rest
                and rest.endswith(_KNEX_SUFFIXES)
                and not rest.endswith(".d.ts")
            ):
                names.add(rest)
    return sorted(names)

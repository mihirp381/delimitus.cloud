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
  sorted, as Knex records it. Without a knexfile (B-10), code that calls ``migrate.latest(`` or
  ``migrate.up(`` on Knex (the file names ``knex``, or a ``package.json`` depends on it) points at
  the ``directory`` it passes, else ``migrations`` at the root.
- ``sequelize`` (B-10): the same files in the ``migrations-path`` of a ``.sequelizerc``, or in
  ``migrations`` beside it or beside a ``package.json`` that depends on ``sequelize`` or
  ``sequelize-cli``; the file name, sorted, as ``SequelizeMeta`` records it.
- ``typeorm`` (B-10): each ``class <Name> implements MigrationInterface`` in a ``.ts`` or ``.js``
  file; the class name, in the order of the timestamp that ends it, as TypeORM runs them.
- ``node-pg-migrate`` (B-10): beside a ``package.json`` that depends on it, the ``.js``, ``.ts``,
  ``.cjs``, ``.mjs`` and ``.sql`` files directly in the folder its scripts name with ``-m`` or
  ``--migrations-dir`` (``migrations`` by default); the name without its extension, sorted.
- ``flyway`` (B-10): every ``V<version>__<description>.sql``; the name without ``.sql``, in
  version order.

Nothing under ``node_modules``, a virtual environment, ``__pycache__`` or ``__MACOSX`` counts, nor
any path with a part starting ``._`` (macOS AppleDouble twins, B-9), and a name longer than
``MAX_NAME`` is left out. A ledger with no migration is absent.
"""

import heapq
import json
import re
from collections.abc import Iterable, Mapping
from typing import Final, cast

LEDGERS: Final = (
    "prisma",
    "alembic",
    "django",
    "drizzle",
    "knex",
    "sequelize",
    "typeorm",
    "node-pg-migrate",
    "flyway",
)
MAX_NAME: Final = 255

type Ledgers = dict[str, tuple[str, ...]]

_SKIP = re.compile(
    r"(?:^|/)(?:(?:node_modules|\.venv|venv|site-packages|__pycache__|__MACOSX)/|\._)"
)
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
_KNEX_CALL = re.compile(r"\.migrate\.(?:latest|up)\s*\(")
_CALL_DIRECTORY = re.compile(
    r"""\.migrate\.(?:latest|up)\s*\(\s*\{[^{}]*?\bdirectory\s*:\s*["'`]([^"'`]+)["'`]"""
)
_PACKAGE_JSON = re.compile(r"^(?P<dir>(?:.*/)?)package\.json$")
_SEQUELIZERC = re.compile(r"^(?P<dir>(?:.*/)?)\.sequelizerc$")
_SEQUELIZE_PATH = re.compile(
    r"""["']migrations-path["']\s*:\s*(?:path\.(?:resolve|join)\(\s*(?:__dirname\s*,\s*)?)?"""
    r"""((?:["'][^"'\n]+["']\s*,?\s*)+)"""
)
_TYPEORM = re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)\s+implements\s+MigrationInterface\b")
_TIMESTAMP = re.compile(r"(\d+)$")
_PG_MIGRATE_DIR = re.compile(r"(?:\s-m|--migrations-dir)(?:\s+|=)[\"']?([^\s\"']+)")
_PG_MIGRATE_SUFFIXES: Final = (*_KNEX_SUFFIXES, ".sql")
_FLYWAY = re.compile(r"(?:^|/)(?P<name>V(?P<version>\d+(?:[._]\d+)*)__[^/]+)\.sql$")
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
        "sequelize": _sequelize(paths, files),
        "typeorm": _typeorm(paths, files),
        "node-pg-migrate": _node_pg_migrate(paths, files),
        "flyway": _flyway(paths),
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
        folders |= _folders(m["dir"], named)
    if not folders:
        folders = _knex_called(paths, files)
    return sorted(_files_in(paths, folders, _KNEX_SUFFIXES))


def _knex_called(paths: list[str], files: Mapping[str, bytes | None]) -> set[str]:
    """The folders code without a knexfile migrates: ``migrate.latest(`` or ``migrate.up(`` in
    a file that names Knex, or in an app whose ``package.json`` depends on it."""
    depends = bool(_depending_on(paths, files, "knex"))
    folders: set[str] = set()
    for path in paths:
        if not _is_source(path):
            continue
        text = _text(files, path)
        if text is None or _KNEX_CALL.search(text) is None:
            continue
        if depends or "knex" in text:
            folders |= _folders("", _CALL_DIRECTORY.findall(text) or ["migrations"])
    return folders


def _sequelize(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    folders: set[str] = set()
    for path in paths:
        m = _SEQUELIZERC.match(path)
        if m is None:
            continue
        named = _SEQUELIZE_PATH.search(_text(files, path) or "")
        parts = _QUOTED.findall(named[1]) if named is not None else ["migrations"]
        folders |= _folders(m["dir"], ["/".join(p.strip("/") for p in parts)])
    for name in ("sequelize", "sequelize-cli"):
        folders |= _folders_beside(_depending_on(paths, files, name), "migrations")
    return sorted(_files_in(paths, folders, _KNEX_SUFFIXES))


def _typeorm(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    classes: set[str] = set()
    for path in paths:
        if not _is_source(path):
            continue
        text = _text(files, path)
        if text is not None and "MigrationInterface" in text:
            classes.update(_TYPEORM.findall(text))

    def when(name: str) -> tuple[int, str]:
        m = _TIMESTAMP.search(name)
        return (int(m[1]) if m is not None else 0, name)

    return sorted(classes, key=when)


def _node_pg_migrate(paths: list[str], files: Mapping[str, bytes | None]) -> list[str]:
    folders: set[str] = set()
    for folder, manifest in _depending_on(paths, files, "node-pg-migrate").items():
        scripts = manifest.get("scripts")
        named = [
            d
            for script in (
                cast("dict[str, object]", scripts) if isinstance(scripts, dict) else {}
            ).values()
            if isinstance(script, str) and "node-pg-migrate" in script
            for d in _PG_MIGRATE_DIR.findall(script)
        ]
        folders |= _folders(folder, named or ["migrations"])
    found = _files_in(paths, folders, _PG_MIGRATE_SUFFIXES)
    return sorted({name.rsplit(".", 1)[0] for name in found})


def _flyway(paths: list[str]) -> list[str]:
    versions: dict[str, tuple[int, ...]] = {}
    for path in paths:
        m = _FLYWAY.search(path)
        if m is not None:
            version = tuple(int(n) for n in re.split(r"[._]", m["version"]))
            versions[m["name"]] = version
    return sorted(versions, key=lambda name: (versions[name], name))


def _is_source(path: str) -> bool:
    return path.endswith(_KNEX_SUFFIXES) and not path.endswith(".d.ts")


def _depending_on(
    paths: list[str], files: Mapping[str, bytes | None], dependency: str
) -> dict[str, dict[str, object]]:
    """Each folder whose ``package.json`` lists ``dependency``, with that file's object."""
    out: dict[str, dict[str, object]] = {}
    for path in paths:
        m = _PACKAGE_JSON.match(path)
        if m is None:
            continue
        try:
            manifest: object = json.loads(_text(files, path) or "")
        except ValueError:
            continue
        if not isinstance(manifest, dict):
            continue
        fields = cast("dict[str, object]", manifest)
        for key in ("dependencies", "devDependencies"):
            listed = fields.get(key)
            if isinstance(listed, dict) and dependency in listed:
                out[m["dir"]] = fields
    return out


def _folders(base: str, named: Iterable[str]) -> set[str]:
    """``base`` joined to each relative folder ``named``; a name that leaves ``base`` is
    dropped."""
    out: set[str] = set()
    for directory in named:
        relative = directory.strip().removeprefix("./").strip("/")
        if relative and ".." not in relative.split("/"):
            out.add(f"{base}{relative}/")
    return out


def _folders_beside(found: Mapping[str, object], name: str) -> set[str]:
    return {f"{base}{name}/" for base in found}


def _files_in(paths: list[str], folders: set[str], suffixes: tuple[str, ...]) -> set[str]:
    """The names of the files with ``suffixes`` directly in one of ``folders``."""
    names: set[str] = set()
    for path in paths:
        for folder in folders:
            rest = path.removeprefix(folder)
            if (
                rest != path
                and "/" not in rest
                and rest.endswith(suffixes)
                and not rest.endswith(".d.ts")
            ):
                names.add(rest)
    return names

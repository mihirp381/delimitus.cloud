"""The offline rules behind ``ssc doctor``: plain file reads and regular expressions.

The causes come from the SSC-003 corpus (spikes/corpus20/RESULTS.md), both its runtime list and
its static list. Each rule is a pure function from a :class:`Tree` to findings. The checks are
heuristics, except the ones the platform also runs: SQLite on disk and the session framework use
``ssc_bundle.analyze`` and secrets use ``ssc_bundle.secrets``, over the files a deploy would pack.
Version drift is checked only where the lock file records the declared ranges as text
(``bun.lock``); other lock files are checked for the names they list. ``ssc.toml`` itself is
checked by the manifest loader (decision 013); the other rules read what they can from it even
when it is invalid.
"""

import json
import os
import re
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from ssc_bundle.analyze import MAX_ANALYZE_BYTES, analyze, sqlite_on_disk
from ssc_bundle.ignore import Ignore
from ssc_bundle.secrets import allowed_values, scan
from ssc_cli.doctor.finding import (
    EXTERNAL_SERVICE,
    LOCKFILE_STALE,
    MANIFEST_INVALID,
    MANIFEST_MISSING,
    NATIVE_LIBRARY,
    NO_START_COMMAND,
    NOT_SINGLE_APP,
    PORT_BINDING,
    PUBLIC_ENV_AT_BUILD,
    SECRET_IN_BUNDLE,
    SESSION_FRAMEWORK,
    STATE_SQLITE_EPHEMERAL,
    WRITES_HOME,
    Finding,
    finding,
)
from ssc_contracts.manifest import (
    MANIFEST_FILE,
    MAX_MANIFEST_BYTES,
    Manifest,
    ManifestError,
    default_manifest,
    is_session_app,
    load_manifest,
    session_framework,
)

SKIP_DIRS: Final = frozenset(
    {
        ".git",
        ".hg",
        ".ssc-dev",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        "dist",
        "build",
        "out",
        "coverage",
    }
)
MAX_FILE_BYTES: Final = 1 << 20
MAX_FILES: Final = 20_000
PY: Final = frozenset({".py"})
JS: Final = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"})
WEB: Final = JS | frozenset({".vue", ".svelte", ".html", ".astro"})
PY_MARKERS: Final = ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
PY_ENTRIES: Final = ("main.py", "app.py", "manage.py")
NODE_ENTRIES: Final = ("index.js", "index.mjs", "server.js", "main.js", "app.js")
OTHER_MARKERS: Final = ("go.mod", "Gemfile", "composer.json", "Cargo.toml", "deno.json")
PREBUILT_DIRS: Final = ("dist", "build", "out", "public")
SPA_BUILDERS: Final = ("vite", "react-scripts")
LOCAL_SPEC: Final = ("file:", "link:", "workspace:", "portal:")
NODE_LOCKS: Final = {
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "bun.lock": "bun",
    "bun.lockb": "bun",
}
PY_LOCKS: Final = {
    "poetry.lock": "poetry",
    "uv.lock": "uv",
    "Pipfile.lock": "pipenv",
    "pdm.lock": "pdm",
}
NATIVE_NODE: Final = frozenset({"bcrypt", "sharp", "canvas"})
NATIVE_PY: Final = frozenset({"psycopg2", "mysqlclient", "pycairo"})


@dataclass
class Tree:
    """The files under one folder, read lazily. Paths are POSIX and relative to ``root``."""

    root: Path
    files: frozenset[str]
    top_dirs: frozenset[str]
    _text: dict[str, str | None] = field(default_factory=dict[str, str | None])
    _packed: list[tuple[str, bytes | None]] | None = None

    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            self._text[rel] = _read_text(self.root / rel) if rel in self.files else None
        return self._text[rel]

    def data(self, rel: str, limit: int = -1) -> bytes | None:
        if rel not in self.files:
            return None
        try:
            with (self.root / rel).open("rb") as f:
                return f.read(limit)
        except OSError:
            return None

    def sources(self, suffixes: frozenset[str]) -> Iterator[tuple[str, str]]:
        for rel in sorted(self.files):
            if Path(rel).suffix in suffixes:
                text = self.text(rel)
                if text is not None:
                    yield rel, text

    def packed(self) -> list[tuple[str, bytes | None]]:
        """The files a deploy would pack, as the build reads them: content, or None if large."""
        if self._packed is None:
            ignore = Ignore.load(self.root)
            self._packed = []
            for rel in sorted(self.files):
                if ignore.reason(rel, is_dir=False) is None:
                    data = self.data(rel, MAX_ANALYZE_BYTES + 1)
                    small = data is not None and len(data) <= MAX_ANALYZE_BYTES
                    self._packed.append((rel, data if small else None))
        return self._packed


def load_tree(root: Path) -> Tree:
    files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if not path.is_symlink():
                files.add(path.relative_to(root).as_posix())
        if len(files) >= MAX_FILES:
            break
    top_dirs = frozenset(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
    return Tree(root=root, files=frozenset(files), top_dirs=top_dirs)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


# ── shared parsing ──────────────────────────────────────────────────────────


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _obj(value: object) -> dict[str, object]:
    """``value`` if it is a JSON object or TOML table, keeping only string keys; else empty."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in cast(dict[object, object], value).items() if isinstance(k, str)}


def _list(value: object) -> list[object]:
    return list(cast(list[object], value)) if isinstance(value, list) else []


def _dig(data: dict[str, object], *keys: str) -> object:
    value: object = data
    for key in keys:
        value = _obj(value).get(key)
    return value


def _package_json(t: Tree) -> dict[str, object] | None:
    text = t.text("package.json")
    if text is None:
        return None
    try:
        data: object = json.loads(text)
    except ValueError:
        return None
    return _obj(cast(object, data)) if isinstance(data, dict) else None


def _node_deps(pkg: dict[str, object]) -> dict[str, str]:
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in _obj(pkg.get(key)).items():
            deps[name] = spec if isinstance(spec, str) else ""
    return deps


def _toml(t: Tree, rel: str) -> dict[str, object] | None:
    text = t.text(rel)
    if text is None:
        return None
    try:
        return tomllib.loads(text.removeprefix("\ufeff"))
    except tomllib.TOMLDecodeError:
        return None


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _req_name(spec: str) -> str | None:
    m = _REQ_NAME.match(spec)
    return _norm(m.group(1)) if m else None


def _python_deps(t: Tree) -> dict[str, str]:
    """Declared Python dependency names (normalised) mapped to the file that declares them."""
    deps: dict[str, str] = {}
    py = _toml(t, "pyproject.toml") or {}
    for spec in _list(_dig(py, "project", "dependencies")):
        if isinstance(spec, str) and (name := _req_name(spec)):
            deps[name] = "pyproject.toml"
    for name in _obj(_dig(py, "tool", "poetry", "dependencies")):
        if name.lower() != "python":
            deps[_norm(name)] = "pyproject.toml"
    for name in _obj(_dig(_toml(t, "Pipfile") or {}, "packages")):
        deps[_norm(name)] = "Pipfile"
    for raw in (t.text("requirements.txt") or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-") and (name := _req_name(line)):
            deps.setdefault(name, "requirements.txt")
    return deps


def _manifest(t: Tree) -> dict[str, object]:
    return _toml(t, MANIFEST_FILE) or {}


def _manifest_start(t: Tree) -> str | None:
    start = _dig(_manifest(t), "runtime", "start")
    return start if isinstance(start, str) and start.strip() else None


def _procfile_web(t: Tree) -> str | None:
    for line in (t.text("Procfile") or "").splitlines():
        key, sep, command = line.partition(":")
        if sep and key.strip() == "web" and command.strip():
            return command.strip()
    return None


def _has_python_root(t: Tree) -> bool:
    return any(m in t.files for m in (*PY_MARKERS, *PY_ENTRIES))


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    return (
        any(p in {"test", "tests", "__tests__", "spec"} for p in parts[:-1])
        or ".test." in name
        or ".spec." in name
        or name.startswith("test_")
    )


def _call_args(text: str, open_paren: int) -> str:
    """The text between a call's parentheses, or up to 2000 characters if unbalanced."""
    depth = 0
    for i in range(open_paren, min(len(text), open_paren + 2000)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i]
    return text[open_paren + 1 : open_paren + 2000]


# ── 1. lock files ───────────────────────────────────────────────────────────


def _node_lock_lists(t: Tree, lock: str, name: str) -> bool:
    if lock == "bun.lockb":
        data = t.data(lock)
        return data is not None and name.encode() in data
    text = t.text(lock) or ""
    n = re.escape(name)
    pattern = {
        "package-lock.json": rf'"(?:[^"]*node_modules/)?{n}"\s*:',
        "yarn.lock": rf'(?m)(?:^|[\s",]){n}@',
        "pnpm-lock.yaml": rf"""(?m)(?:^|[\s'"/]){n}(?:@|'?\s*:)""",
        "bun.lock": rf'"{n}"',
    }[lock]
    return re.search(pattern, text) is not None


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _bun_lock_root(t: Tree) -> dict[str, str] | None:
    """The ranges ``bun.lock`` recorded from package.json, or None if it cannot be read."""
    try:
        data: object = json.loads(_TRAILING_COMMA.sub(r"\1", t.text("bun.lock") or ""))
    except ValueError:
        return None
    root = _obj(_dig(_obj(data), "workspaces", ""))
    return _node_deps(root) if root else None


def _python_lock_names(t: Tree, lock: str) -> set[str] | None:
    if lock == "Pipfile.lock":
        try:
            data: object = json.loads(t.text(lock) or "")
        except ValueError:
            return None
        default = _obj(data).get("default")
        if not isinstance(default, dict):
            return None
        return {_norm(k) for k in _obj(cast(object, default))}
    data = _toml(t, lock)
    if data is None:
        return None
    names = (_obj(p).get("name") for p in _list(data.get("package")))
    return {_norm(n) for n in names if isinstance(n, str)}


def lockfile_stale(t: Tree) -> list[Finding]:
    out: list[Finding] = []
    for locks, label in ((NODE_LOCKS, "JavaScript"), (PY_LOCKS, "Python")):
        present = sorted(lock for lock in locks if lock in t.files)
        tools = sorted({locks[lock] for lock in present})
        if len(tools) > 1:
            out.append(
                finding(
                    LOCKFILE_STALE,
                    present[0],
                    f"{label} lock files from more than one tool: {', '.join(present)}. "
                    "The build picks one, and it may not match your dependency list.",
                )
            )
    pkg = _package_json(t)
    if pkg is not None:
        wanted = [n for n, spec in _node_deps(pkg).items() if not spec.startswith(LOCAL_SPEC)]
        for lock in sorted(set(NODE_LOCKS) & t.files):
            recorded = _bun_lock_root(t) if lock == "bun.lock" else None
            if recorded is not None:
                declared = _node_deps(pkg)
                drift = sorted(
                    n
                    for n in declared.keys() | recorded.keys()
                    if declared.get(n) != recorded.get(n)
                )
                if drift:
                    out.append(_drift(lock, drift))
                continue
            missing = [n for n in wanted if not _node_lock_lists(t, lock, n)]
            if missing:
                out.append(_missing(lock, "package.json", missing))
    py_wanted = _python_deps(t)
    for lock in sorted(set(PY_LOCKS) & t.files):
        names = _python_lock_names(t, lock)
        if names is None:
            out.append(finding(LOCKFILE_STALE, lock, f"{lock} cannot be read."))
            continue
        declared = {n for n, src in py_wanted.items() if src != "requirements.txt"}
        missing = sorted(declared - names)
        if missing:
            out.append(_missing(lock, "your project file", missing))
    return out


def _shown(names: list[str]) -> str:
    return ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")


def _missing(lock: str, source: str, names: list[str]) -> Finding:
    return finding(
        LOCKFILE_STALE, lock, f"{lock} does not list {_shown(names)}, which {source} declares."
    )


def _drift(lock: str, names: list[str]) -> Finding:
    return finding(
        LOCKFILE_STALE,
        lock,
        f"{lock} records different dependencies from package.json for {_shown(names)}.",
    )


# ── 2. public build-time variables ──────────────────────────────────────────

_PUBLIC_ENV = re.compile(r"\b((?:VITE|NEXT_PUBLIC|REACT_APP)_[A-Z0-9_]*[A-Z0-9])\b")


def _declared_public_env(t: Tree) -> set[str]:
    """Names given a value in any ``[build.public_env.<environment>]`` table of ssc.toml."""
    tables = _obj(_dig(_manifest(t), "build", "public_env")).values()
    return {name for table in tables for name in _obj(table)}


def public_env_at_build(t: Tree) -> list[Finding]:
    declared = _declared_public_env(t)
    first: dict[str, tuple[str, int]] = {}
    for rel, text in t.sources(WEB):
        for m in _PUBLIC_ENV.finditer(text):
            name = m.group(1)
            if name not in declared and name not in first:
                first[name] = (rel, _line_of(text, m.start()))
    return [
        finding(
            PUBLIC_ENV_AT_BUILD,
            rel,
            f"{name} is read at build time but is not listed in ssc.toml, so the page gets an "
            "empty value.",
            line,
        )
        for name, (rel, line) in sorted(first.items())
    ]


# ── 3. start command and port ───────────────────────────────────────────────


def _uses_streamlit(t: Tree) -> bool:
    if "streamlit" in _python_deps(t):
        return True
    return any(
        re.search(r"(?m)^\s*(?:import|from)\s+streamlit\b", text) for _, text in t.sources(PY)
    )


def no_start_command(t: Tree) -> list[Finding]:
    if _procfile_web(t) or _manifest_start(t):
        return []
    if _has_python_root(t) and _uses_streamlit(t):
        return [
            finding(
                NO_START_COMMAND,
                ".",
                "This is a Streamlit app, and Streamlit apps need an explicit start command.",
            )
        ]
    pkg = _package_json(t)
    if pkg is not None:
        scripts = _obj(pkg.get("scripts"))
        main = pkg.get("main")
        deps = _node_deps(pkg)
        if (
            scripts.get("start")
            or (isinstance(main, str) and main.removeprefix("./") in t.files)
            or any(e in t.files for e in NODE_ENTRIES)
            or (scripts.get("build") and any(b in deps for b in SPA_BUILDERS))
        ):
            return []
        return [
            finding(
                NO_START_COMMAND,
                "package.json",
                "package.json has no start script, no main file and no known entry file.",
            )
        ]
    if _has_python_root(t) and not any(e in t.files for e in PY_ENTRIES):
        return [
            finding(
                NO_START_COMMAND,
                ".",
                "No Procfile, no start in ssc.toml, and no main.py or app.py to run.",
            )
        ]
    return []


_PY_SERVE = re.compile(r"\b(?:(?:app|application|server)\.run|uvicorn\.run|web\.run_app)\s*\(")
_PY_READS_PORT = re.compile(r"""["']PORT["']""")
_JS_LISTEN = re.compile(r"\.listen\s*\(")
_JS_READS_PORT = re.compile(r"""env(?:\.PORT\b|\[\s*["']PORT["']\s*\])""")
_LOCALHOST = re.compile(r"""["'](?:localhost|127\.0\.0\.1)["']""")
_HOST_KW = re.compile(r"\bhost\s*=")


def port_binding(t: Tree) -> list[Finding]:
    start = _procfile_web(t) or _manifest_start(t) or ""
    # A start command that passes $PORT decides the binding itself.
    out = [] if "PORT" in start else _python_port(t)
    return out + _js_port(t)


def _python_port(t: Tree) -> list[Finding]:
    out: list[Finding] = []
    for rel, text in t.sources(PY):
        if _is_test_path(rel):
            continue
        for m in _PY_SERVE.finditer(text):
            args = _call_args(text, m.end() - 1)
            # Flask and uvicorn listen on 127.0.0.1 unless told otherwise; aiohttp does not.
            default_local = not m.group(0).startswith("web.") and not _HOST_KW.search(args)
            problems = _port_problems(
                local=default_local or bool(_LOCALHOST.search(args)),
                reads_port=bool(_PY_READS_PORT.search(text)),
            )
            if problems:
                out.append(_port_finding(rel, text, m.start(), problems))
    return out


def _js_port(t: Tree) -> list[Finding]:
    out: list[Finding] = []
    for rel, text in t.sources(JS):
        if _is_test_path(rel) or ".config." in rel.rsplit("/", 1)[-1]:
            continue
        for m in _JS_LISTEN.finditer(text):
            problems = _port_problems(
                local=bool(_LOCALHOST.search(_call_args(text, m.end() - 1))),
                reads_port=bool(_JS_READS_PORT.search(text)),
            )
            if problems:
                out.append(_port_finding(rel, text, m.start(), problems))
    return out


def _port_problems(*, local: bool, reads_port: bool) -> list[str]:
    problems: list[str] = []
    if local:
        problems.append("it listens on localhost only")
    if not reads_port:
        problems.append("the port does not come from PORT")
    return problems


def _port_finding(rel: str, text: str, pos: int, problems: list[str]) -> Finding:
    return finding(
        PORT_BINDING,
        rel,
        f"The server here cannot be reached in its container: {' and '.join(problems)}.",
        _line_of(text, pos),
    )


# ── 4. external services ────────────────────────────────────────────────────

SERVICES: Final = (
    ("MongoDB", re.compile(r"^(?:mongodb|mongoose|pymongo|motor|mongoengine)$")),
    ("Supabase", re.compile(r"^(?:@supabase/.+|supabase)$")),
    ("Clerk", re.compile(r"^(?:@clerk/.+|clerk-backend-api)$")),
    ("Firebase", re.compile(r"^(?:firebase|firebase-admin|firebase-functions)$")),
)


def external_service(t: Tree) -> list[Finding]:
    declared: list[tuple[str, str]] = []
    pkg = _package_json(t)
    if pkg is not None:
        declared += [(n, "package.json") for n in _node_deps(pkg)]
    declared += list(_python_deps(t).items())
    out: list[Finding] = []
    for service, pattern in SERVICES:
        hits = sorted((src, n) for n, src in declared if pattern.match(n))
        if hits:
            src, name = hits[0]
            text = t.text(src) or ""
            pos = text.find(f'"{name}"') if src == "package.json" else text.find(name)
            out.append(
                finding(
                    EXTERNAL_SERVICE,
                    src,
                    f"The app depends on {service} ({name}), which SSC does not provide.",
                    _line_of(text, pos) if pos >= 0 else None,
                )
            )
    return out


# ── 5. writes to the home folder ────────────────────────────────────────────

_HOME = re.compile(
    r"""expanduser\s*\(|Path\.home\s*\(|os\.homedir\s*\(|process\.env\.HOME\b"""
    r"""|environ(?:\.get\s*\(\s*|\[\s*)["']HOME["']|getenv\s*\(\s*["']HOME["']"""
)


def writes_home(t: Tree) -> list[Finding]:
    out: list[Finding] = []
    for rel, text in t.sources(PY | JS):
        if _is_test_path(rel):
            continue
        m = _HOME.search(text)
        if m:
            out.append(
                finding(
                    WRITES_HOME,
                    rel,
                    "This file uses the home folder, which the app cannot rely on when it runs.",
                    _line_of(text, m.start()),
                )
            )
    return out


# ── 6. one app at the top of the folder ─────────────────────────────────────


def _app_root(t: Tree, prefix: str = "") -> bool:
    names = ("package.json", *PY_MARKERS, *PY_ENTRIES, "Procfile", *OTHER_MARKERS)
    return any(f"{prefix}{n}" in t.files for n in names)


def _workspace(t: Tree) -> Finding | None:
    pkg = _package_json(t)
    if pkg is not None and ("workspaces" in pkg or "pnpm-workspace.yaml" in t.files):
        return finding(
            NOT_SINGLE_APP,
            "package.json",
            "This folder is a workspace of several packages, not one app.",
        )
    if "lerna.json" in t.files:
        return finding(NOT_SINGLE_APP, "lerna.json", "This folder is a monorepo, not one app.")
    if _dig(_toml(t, "pyproject.toml") or {}, "tool", "uv", "workspace") is not None:
        return finding(
            NOT_SINGLE_APP,
            "pyproject.toml",
            "This folder is a uv workspace of several packages, not one app.",
        )
    return None


def not_single_app(t: Tree) -> list[Finding]:
    workspace = _workspace(t)
    if workspace is not None:
        return [workspace]
    if _app_root(t) or _manifest_start(t):
        return []
    subapps = sorted(d for d in t.top_dirs if d not in SKIP_DIRS and _app_root(t, f"{d}/"))
    if subapps:
        where = ", ".join(f"{d}/" for d in subapps)
        return [
            finding(
                NOT_SINGLE_APP, ".", f"Found apps in {where} but none at the top of the folder."
            )
        ]
    prebuilt = [d for d in PREBUILT_DIRS if d in t.top_dirs]
    if prebuilt or "index.html" in t.files:
        return [
            finding(
                NOT_SINGLE_APP,
                ".",
                "Only prebuilt files were found. SSC builds apps from source and does not host "
                "static sites.",
            )
        ]
    return [
        finding(
            NOT_SINGLE_APP,
            ".",
            "No app found: expected package.json, pyproject.toml, requirements.txt or a Procfile "
            "at the top of the folder.",
        )
    ]


# ── 7. the manifest ─────────────────────────────────────────────────────────


def manifest(t: Tree) -> list[Finding]:
    if MANIFEST_FILE not in t.files:
        return [
            finding(MANIFEST_MISSING, ".", "There is no ssc.toml, so the app gets the defaults.")
        ]
    # One byte over the limit is enough for the loader to refuse the size.
    data = t.data(MANIFEST_FILE, MAX_MANIFEST_BYTES + 1)
    if data is None:
        return [finding(MANIFEST_INVALID, MANIFEST_FILE, "ssc.toml cannot be read.")]
    try:
        load_manifest(data)
    except ManifestError as exc:
        return [finding(MANIFEST_INVALID, MANIFEST_FILE, str(p), p.line) for p in exc.problems]
    return []


def _loaded_manifest(t: Tree) -> Manifest:
    """The manifest the platform would use, or the defaults if it is missing or invalid."""
    data = t.data(MANIFEST_FILE, MAX_MANIFEST_BYTES + 1)
    if data is None:
        return default_manifest()
    try:
        return load_manifest(data)
    except ManifestError:
        return default_manifest()


def state_sqlite_ephemeral(t: Tree) -> list[Finding]:
    refusal = sqlite_on_disk(t.packed(), _loaded_manifest(t))
    if refusal is None:
        return []
    return [
        finding(
            STATE_SQLITE_EPHEMERAL,
            refusal.path,
            f"This file keeps SQLite on disk ({refusal.detail}). The file system is memory, so "
            "the data is lost when the app stops, and the build is refused.",
        )
    ]


def secret_in_bundle(t: Tree) -> list[Finding]:
    files = [(rel, data) for rel, data in t.packed() if data is not None]
    found = scan(files, allowed_values(_loaded_manifest(t)))
    return [
        finding(
            SECRET_IN_BUNDLE,
            f.path,
            f"This line holds what looks like a secret ({f.rule}), so the deploy is refused.",
            f.line,
        )
        for f in found
        if f.blocking
    ]


def native_library(t: Tree) -> list[Finding]:
    declared: list[tuple[str, str]] = []
    pkg = _package_json(t)
    if pkg is not None:
        declared += [(n, "package.json") for n in _node_deps(pkg) if n in NATIVE_NODE]
    declared += [(n, src) for n, src in _python_deps(t).items() if n in NATIVE_PY]
    out: list[Finding] = []
    for name, src in sorted(declared):
        text = t.text(src) or ""
        pos = text.find(f'"{name}"') if src == "package.json" else text.find(name)
        out.append(
            finding(
                NATIVE_LIBRARY,
                src,
                f"{name} is compiled for the machine it runs on. The build usually gets a "
                "prebuilt copy, but if none fits, it stops while compiling.",
                _line_of(text, pos) if pos >= 0 else None,
            )
        )
    return out


def session_framework_rule(t: Tree) -> list[Finding]:
    m = _loaded_manifest(t)
    framework = analyze(t.packed(), m).framework
    if not is_session_app(m.runtime, framework):
        return []
    name = session_framework(m.runtime.start) or framework
    where = MANIFEST_FILE if m.runtime.start or m.runtime.sessions else "."
    what = f"This is a {name.capitalize()} app" if name else "ssc.toml sets sessions = true"
    lost = (
        " Streamlit loses its session state then and the page reloads."
        if name == "streamlit"
        else ""
    )
    return [
        finding(
            SESSION_FRAMEWORK,
            where,
            f"{what}, so it runs as one instance and its connections drop at 60 minutes.{lost}",
        )
    ]


Rule = Callable[[Tree], Iterable[Finding]]

RULES: Final[tuple[Rule, ...]] = (
    lockfile_stale,
    public_env_at_build,
    no_start_command,
    port_binding,
    external_service,
    writes_home,
    not_single_app,
    manifest,
    state_sqlite_ephemeral,
    secret_in_bundle,
    native_library,
    session_framework_rule,
)

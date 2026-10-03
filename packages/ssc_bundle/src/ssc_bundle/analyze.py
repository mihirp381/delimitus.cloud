"""What the build reads from the source before Railpack runs (SSC-015).

Stdlib only, no execution: the files of a stored bundle and its manifest in, one ``Analysis``
out. A refusal stops the build with a code from ``ssc_contracts.build`` before any builder is
called; notices are warnings for the build log; ``framework`` is the session framework the app
uses (Streamlit, Gradio, Dash, Shiny), which makes it a session app (``is_session_app``) without
``sessions = true``.

Refused, in this order: Java and chat bots (``BUILD_UNSUPPORTED_RUNTIME``), a private package
registry (``BUILD_PRIVATE_REGISTRY``), SQLite on disk (``STATE_SQLITE_EPHEMERAL``) and an app
with nothing to start (``BUILD_NO_ENTRYPOINT``: only notebooks, or Streamlit or Shiny without a
start command). With ``postgres = true`` a ``sqlite:///`` URL is taken for a local fallback and
not refused; a ``sqlite3.connect`` call, a SQLite driver or a SQLite file always is. In-memory
SQLite (``:memory:``, ``mode=memory``) and test files are never refused. ``sqlite_on_disk`` is the
same SQLite rule alone, for ``ssc doctor`` and ``ssc deploy``. A Dockerfile is never used, and gets
a notice.
"""

import ipaddress
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import urlsplit

from ssc_contracts.build import (
    BUILD_NO_ENTRYPOINT,
    BUILD_PRIVATE_REGISTRY,
    BUILD_UNSUPPORTED_RUNTIME,
    STATE_SQLITE_EPHEMERAL,
)
from ssc_contracts.manifest import SESSION_FRAMEWORKS, Manifest

MAX_ANALYZE_BYTES: Final = 1024 * 1024
JAVA_FILES: Final = frozenset(
    {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}
)
BOT_PACKAGES: Final = frozenset(
    {
        "discord",
        "discord-py",
        "py-cord",
        "nextcord",
        "python-telegram-bot",
        "pytelegrambotapi",
        "aiogram",
        "discord.js",
        "telegraf",
        "node-telegram-bot-api",
        "grammy",
    }
)
SCHEDULER_PACKAGES: Final = frozenset(
    {"apscheduler", "schedule", "rocketry", "node-cron", "node-schedule", "cron", "agenda"}
)
KV_PACKAGES: Final = frozenset(
    {"redis", "ioredis", "@upstash/redis", "valkey", "pymemcache", "memcached", "keyv"}
)
SQLITE_NODE_PACKAGES: Final = frozenset({"better-sqlite3", "sqlite3", "sqlite"})
STARTLESS_FRAMEWORKS: Final = frozenset({"streamlit", "shiny"})
"""Frameworks that do nothing when run as ``python app.py``, Railpack's default."""
PRIVATE_HOST_LABELS: Final = frozenset(
    {"internal", "corp", "local", "lan", "intranet", "private", "artifactory", "nexus", "jfrog"}
)
PRIVATE_HOST_SUFFIXES: Final = (".pkg.dev", ".codeartifact.amazonaws.com", "pkgs.dev.azure.com")
DOCKERFILES: Final = frozenset({"dockerfile", "containerfile"})

_PY_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_PEP503 = re.compile(r"[-_.]+")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_REQ_INDEX = re.compile(r"^\s*(?:--index-url|--extra-index-url|-i|--find-links|-f)[\s=]+(\S+)")
_SQLITE_CONNECT = re.compile(
    r"""(?:sqlite3|aiosqlite)\.connect\s*\(\s*(?:[rbuf]?["']([^"']*)["'])?"""
)
_SQLITE_URL = re.compile(r"""sqlite(?:\+\w+)?:///([^"'\s)]*)""")
_JS_DATABASE = re.compile(r"""\bDatabase\s*\(\s*["'`]([^"'`]*)["'`]""")
_PRISMA_SQLITE = re.compile(r"""provider\s*=\s*["']sqlite["']""")
_DJANGO_SQLITE = re.compile(r"""django\.db\.backends\.sqlite3""")
_SQLITE_HEADER: Final = b"SQLite format 3\0"
_NPMRC_REGISTRY = re.compile(r"^\s*(?:@[\w.-]+:)?registry\s*=\s*(\S+)", re.MULTILINE)
_NPMRC_TOKEN = re.compile(r"_authToken\s*=|_auth\s*=|_password\s*=")
_YARN_REGISTRY = re.compile(r"^\s*npmRegistryServer:\s*[\"']?([^\"'\s]+)", re.MULTILINE)
_GIT_SSH = re.compile(r"^(?:git\+ssh://|ssh://|git@)")
_SUPABASE_AUTH = re.compile(r"\bsupabase\.auth\.|@supabase/auth-ui|@supabase/auth-helpers")
_PY_SERVE = re.compile(
    r"\b(?:(?:app|application|server|demo)\.(?:run|run_server|launch)|uvicorn\.run)\s*\(([^)]*)\)"
)
_JS_LISTEN = re.compile(r"\.listen\s*\(([^)]*)\)")
_LOCAL = re.compile(r"""["'](?:localhost|127\.0\.0\.1)["']""")
_PROCFILE_WEB = re.compile(r"^web:\s*\S", re.MULTILINE)
_PY: Final = (".py",)
_JS: Final = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")
_PUBLIC_NPM: Final = frozenset({"registry.npmjs.org", "registry.yarnpkg.com"})


@dataclass(frozen=True, slots=True)
class Refusal:
    code: str
    path: str
    detail: str
    """Log only: names the file and the signal, never a value from it."""


@dataclass(frozen=True, slots=True)
class Analysis:
    framework: str | None
    refusal: Refusal | None
    notices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Source:
    files: Mapping[str, bytes | None]

    def text(self, path: str) -> str | None:
        data = self.files.get(path)
        return None if data is None else data.decode("utf-8", errors="replace")

    def sources(self, suffixes: tuple[str, ...]) -> Iterable[tuple[str, str]]:
        for path in sorted(self.files):
            if path.endswith(suffixes) and "node_modules/" not in path:
                text = self.text(path)
                if text is not None:
                    yield path, text


def analyze(files: Iterable[tuple[str, bytes | None]], manifest: Manifest) -> Analysis:
    """Refusal, notices and framework of the app in ``files`` (POSIX path, content or None)."""
    src = _Source(dict(files))
    py, node = _python_deps(src), _node_deps(src)
    framework = _framework(src, py)
    refusal = (
        _unsupported(src, py | node)
        or _private_registry(src)
        or _sqlite(src, node, postgres=manifest.state.postgres)
        or _no_entrypoint(src, manifest, framework)
    )
    return Analysis(framework, refusal, _notices(src, py | node))


def sqlite_on_disk(files: Iterable[tuple[str, bytes | None]], manifest: Manifest) -> Refusal | None:
    """The ``STATE_SQLITE_EPHEMERAL`` refusal ``analyze`` would give these files, if any."""
    src = _Source(dict(files))
    return _sqlite(src, _node_deps(src), postgres=manifest.state.postgres)


def _unsupported(src: _Source, deps: frozenset[str]) -> Refusal | None:
    java = sorted(JAVA_FILES & src.files.keys())
    if java:
        return Refusal(BUILD_UNSUPPORTED_RUNTIME, java[0], "a Java build file")
    bots = sorted(BOT_PACKAGES & deps)
    if bots:
        return Refusal(BUILD_UNSUPPORTED_RUNTIME, ".", f"a chat bot library: {bots[0]}")
    return None


def _private_registry(src: _Source) -> Refusal | None:
    for path in sorted(src.files):
        name = path.rsplit("/", 1)[-1]
        text = src.text(path) if "/" not in path else None
        if text is None:
            continue
        urls: list[str] = []
        if name.startswith("requirements") and name.endswith(".txt"):
            urls = [m.group(1) for line in text.splitlines() if (m := _REQ_INDEX.match(line))]
        elif name == "pyproject.toml":
            urls = _toml_urls(text, ("tool", "poetry", "source"), ("tool", "uv", "index"))
        elif name == "Pipfile":
            urls = _toml_urls(text, ("source",))
        elif name == ".npmrc":
            if _NPMRC_TOKEN.search(text):
                return Refusal(BUILD_PRIVATE_REGISTRY, path, "a registry token")
            urls = [u for u in _NPMRC_REGISTRY.findall(text) if _host(u) not in _PUBLIC_NPM]
        elif name == ".yarnrc.yml":
            urls = [u for u in _YARN_REGISTRY.findall(text) if _host(u) not in _PUBLIC_NPM]
        elif name == "package.json":
            urls = [v for v in _node_specs(text) if _GIT_SSH.match(v)]
        private = [u for u in urls if _private_url(u)]
        if private:
            return Refusal(BUILD_PRIVATE_REGISTRY, path, f"a private index at {_host(private[0])}")
    return None


def _sqlite(src: _Source, node: frozenset[str], *, postgres: bool) -> Refusal | None:
    for path, data in sorted(src.files.items()):
        if _is_test(path):
            continue
        if path.lower().endswith((".sqlite", ".sqlite3")) or (
            data is not None and data.startswith(_SQLITE_HEADER)
        ):
            return Refusal(STATE_SQLITE_EPHEMERAL, path, "a SQLite database file")
    for path, text in src.sources(_PY + _JS + (".prisma",)):
        if not _is_test(path):
            detail = _sqlite_use(
                path, text, node_sqlite=bool(node & SQLITE_NODE_PACKAGES), postgres=postgres
            )
            if detail:
                return Refusal(STATE_SQLITE_EPHEMERAL, path, detail)
    return None


def _sqlite_use(path: str, text: str, *, node_sqlite: bool, postgres: bool) -> str | None:
    if any(not _in_memory(m.group(1)) for m in _SQLITE_CONNECT.finditer(text)):
        return "a SQLite connect on a file"
    if not postgres and any(
        m.group(1) and not _in_memory(m.group(1)) for m in _SQLITE_URL.finditer(text)
    ):
        return "a sqlite:/// file URL"
    if _PRISMA_SQLITE.search(text) or _DJANGO_SQLITE.search(text):
        return "a SQLite database setting"
    if node_sqlite and path.endswith(_JS):
        if any(m.group(1) and not _in_memory(m.group(1)) for m in _JS_DATABASE.finditer(text)):
            return "a SQLite driver on a file"
    return None


def _in_memory(name: str | None) -> bool:
    return name is not None and (":memory:" in name or "mode=memory" in name)


def _no_entrypoint(src: _Source, manifest: Manifest, framework: str | None) -> Refusal | None:
    if manifest.runtime.start or _PROCFILE_WEB.search(src.text("Procfile") or ""):
        return None
    if framework in STARTLESS_FRAMEWORKS:
        return Refusal(BUILD_NO_ENTRYPOINT, ".", f"a {framework} app without a start command")
    names = src.files.keys()
    notebooks = any(p.endswith(".ipynb") for p in names)
    if notebooks and not any(p.endswith(_PY + _JS) or p.endswith(".html") for p in names):
        return Refusal(BUILD_NO_ENTRYPOINT, ".", "notebooks and no script to serve")
    return None


def _framework(src: _Source, py: frozenset[str]) -> str | None:
    found = set(SESSION_FRAMEWORKS & py)
    for _, text in src.sources(_PY):
        found.update(SESSION_FRAMEWORKS & set(_PY_IMPORT.findall(text)))
    return next((n for n in ("streamlit", "gradio", "shiny", "dash") if n in found), None)


def _notices(src: _Source, deps: frozenset[str]) -> tuple[str, ...]:
    notes: list[str] = []
    code = list(src.sources(_PY + _JS))
    if any(_SUPABASE_AUTH.search(t) for _, t in code) or any(
        d.startswith("@supabase/auth") for d in deps
    ):
        notes.append("SUPABASE_AUTH")
    if any(_binds_localhost(p, t) for p, t in code if not _is_test(p)):
        notes.append("LOCALHOST_BIND")
    if SCHEDULER_PACKAGES & deps:
        notes.append("IN_PROCESS_SCHEDULER")
    if KV_PACKAGES & deps:
        notes.append("KV_STORE")
    if any(p.rsplit("/", 1)[-1].lower() in DOCKERFILES for p in src.files):
        notes.append("DOCKERFILE_IGNORED")
    return tuple(notes)


def _binds_localhost(path: str, text: str) -> bool:
    if path.endswith(_JS):
        return any(_LOCAL.search(m.group(1)) for m in _JS_LISTEN.finditer(text))
    for m in _PY_SERVE.finditer(text):
        args = m.group(1)
        if _LOCAL.search(args) or ("host" not in args and "server_name" not in args):
            return True
    return False


def _python_deps(src: _Source) -> frozenset[str]:
    names: set[str] = set()
    for path in src.files:
        if "/" in path:
            continue
        text = src.text(path) or ""
        if path.startswith("requirements") and path.endswith(".txt"):
            for line in text.splitlines():
                m = _REQ_NAME.match(line)
                if m and not line.lstrip().startswith("-"):
                    names.add(m.group(1))
        elif path == "pyproject.toml":
            doc = _toml(text)
            project = _obj(doc.get("project"))
            for spec in _list(project.get("dependencies")):
                m = _REQ_NAME.match(str(spec))
                if m:
                    names.add(m.group(1))
            names.update(_obj(_obj(_obj(doc.get("tool")).get("poetry")).get("dependencies")))
        elif path == "Pipfile":
            names.update(_obj(_toml(text).get("packages")))
    return frozenset(_PEP503.sub("-", n.lower()) for n in names)


def _node_deps(src: _Source) -> frozenset[str]:
    pkg = _json(src.text("package.json") or "")
    return frozenset(_obj(pkg.get("dependencies")))


def _node_specs(text: str) -> list[str]:
    pkg = _json(text)
    deps = {**_obj(pkg.get("dependencies")), **_obj(pkg.get("devDependencies"))}
    return [str(v) for v in deps.values()]


def _toml_urls(text: str, *paths: tuple[str, ...]) -> list[str]:
    doc = _toml(text)
    urls: list[str] = []
    for path in paths:
        node: object = doc
        for key in path:
            node = _obj(node).get(key)
        urls += [str(_obj(entry).get("url") or "") for entry in _list(node)]
    return [u for u in urls if u]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _private_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or "${" in url:
        return True
    if not host or "." not in host:
        return bool(host)
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    labels = set(host.split("."))
    return bool(labels & PRIVATE_HOST_LABELS) or host.endswith(PRIVATE_HOST_SUFFIXES)


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(("tests/", "test/"))
        or "/tests/" in path
        or "/test/" in path
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        or name == "conftest.py"
    )


def _toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _json(text: str) -> dict[str, Any]:
    try:
        value: object = json.loads(text)
    except ValueError:
        return {}
    return _obj(value)


def _obj(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []

"""SSC-015: what the build reads from the source before Railpack runs."""

import pytest

from ssc_bundle.analyze import Analysis, analyze, package_needs, sqlite_on_disk
from ssc_contracts.build import FIX_ITS, NOTICES, SQLITE_ON_DISK
from ssc_contracts.manifest import Manifest, is_session_app


def manifest(**tables: object) -> Manifest:
    return Manifest.model_validate({"schema": "ssc/v1", **tables})


def run(files: dict[str, str | bytes], m: Manifest | None = None) -> Analysis:
    encoded = [(p, v.encode() if isinstance(v, str) else v) for p, v in files.items()]
    return analyze(encoded, m or manifest())


def code(files: dict[str, str | bytes], m: Manifest | None = None) -> str | None:
    refusal = run(files, m).refusal
    return None if refusal is None else refusal.code


START = {"runtime": {"start": "python app.py"}}


@pytest.mark.parametrize(
    "files",
    [
        {"app.py": "import sqlite3\ndb = sqlite3.connect('data.db')\n"},
        {"app.py": "import aiosqlite\nasync with aiosqlite.connect(PATH) as db: ...\n"},
        {"app.py": "engine = create_engine('sqlite:///app.db')\n"},
        {"data/app.sqlite3": b"SQLite format 3\x00rest"},
        {"seed.db": b"SQLite format 3\x00rest"},
        {"prisma/schema.prisma": 'datasource db {\n  provider = "sqlite"\n}\n'},
        {"site/settings.py": "ENGINE = 'django.db.backends.sqlite3'\n"},
        {
            "package.json": '{"dependencies": {"better-sqlite3": "11.0.0"}}',
            "server.js": "const db = new Database('app.db');\n",
        },
    ],
)
def test_sqlite_on_disk_is_refused_with_the_postgres_fix(files: dict[str, str | bytes]) -> None:
    assert code(files) == "STATE_SQLITE_EPHEMERAL"
    assert FIX_ITS["STATE_SQLITE_EPHEMERAL"] == SQLITE_ON_DISK
    assert "[state]\npostgres = true" in SQLITE_ON_DISK


@pytest.mark.parametrize(
    "files",
    [
        {"app.py": "db = sqlite3.connect(':memory:')\n"},
        {"app.py": "db = sqlite3.connect('file::memory:?cache=shared', uri=True)\n"},
        {"app.py": "db = sqlite3.connect('file:x?mode=memory', uri=True)\n"},
        {"app.py": "engine = create_engine('sqlite:///:memory:')\n"},
        {"tests/test_db.py": "db = sqlite3.connect('t.db')\n"},
        {"tests/fixtures/sample.sqlite": b"SQLite format 3\x00rest"},
        {"src/test_seed.db": b"SQLite format 3\x00rest"},
        {"app.py": "# a note about sqlite\nprint('hi')\n"},
        {"server.js": "const db = new Database('app.db');\n"},
    ],
)
def test_memory_test_and_unrelated_sqlite_pass(files: dict[str, str | bytes]) -> None:
    assert code(files, manifest(**START)) is None


def test_sqlite_on_disk_alone_gives_what_analyze_gives() -> None:
    files = [
        ("pom.xml", b"<project/>"),
        ("app.py", b"import sqlite3\ndb = sqlite3.connect('data.db')\n"),
    ]
    refusal = sqlite_on_disk(files, manifest())
    assert refusal is not None
    assert (refusal.code, refusal.path) == ("STATE_SQLITE_EPHEMERAL", "app.py")
    assert analyze(files[1:], manifest()).refusal == refusal
    assert analyze(files, manifest()).refusal != refusal
    assert sqlite_on_disk([("app.py", b"print(1)\n")], manifest()) is None


def test_a_sqlite_url_is_a_fallback_once_postgres_is_on() -> None:
    files: dict[str, str | bytes] = {
        "app.py": "url = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')\n"
    }
    assert code(files) == "STATE_SQLITE_EPHEMERAL"
    assert code(files, manifest(state={"postgres": True})) is None
    connect: dict[str, str | bytes] = {"app.py": "sqlite3.connect('x.db')\n"}
    assert code(connect, manifest(state={"postgres": True})) == "STATE_SQLITE_EPHEMERAL"


@pytest.mark.parametrize(
    "files",
    [
        {"requirements.txt": "--index-url https://artifactory.internal.example/simple\nflask\n"},
        {"requirements.txt": "-i https://user:pw@pypi.example.com/simple\nflask\n"},
        {"requirements.txt": "--extra-index-url http://10.0.0.5/simple\nflask\n"},
        {
            "pyproject.toml": '[[tool.poetry.source]]\nname = "corp"\n'
            'url = "https://pypi.corp.example.com/simple"\n'
        },
        {"pyproject.toml": '[[tool.uv.index]]\nurl = "https://us-python.pkg.dev/p/r/simple"\n'},
        {"Pipfile": '[[source]]\nurl = "https://nexus.example.com/simple"\n'},
        {".npmrc": "//registry.npmjs.org/:_authToken=${NPM_TOKEN}\n"},
        {".npmrc": "@acme:registry=https://npm.internal/\n"},
        {".yarnrc.yml": "npmRegistryServer: https://jfrog.example.com/npm\n"},
        {"package.json": '{"dependencies": {"ui": "git+ssh://git@github.com/acme/ui.git"}}'},
    ],
)
def test_a_private_registry_is_refused(files: dict[str, str | bytes]) -> None:
    assert code(files, manifest(**START)) == "BUILD_PRIVATE_REGISTRY"


@pytest.mark.parametrize(
    "files",
    [
        {"requirements.txt": "--index-url https://pypi.org/simple\nflask\n"},
        {"requirements.txt": "--extra-index-url https://download.pytorch.org/whl/cpu\ntorch\n"},
        {".npmrc": "registry=https://registry.npmjs.org/\n"},
        {"package.json": '{"dependencies": {"ui": "github:acme/ui"}}'},
    ],
)
def test_public_indexes_pass(files: dict[str, str | bytes]) -> None:
    assert code(files, manifest(**START)) is None


def test_the_refusal_never_carries_a_credential() -> None:
    refusal = run({"requirements.txt": "-i https://bob:hunter2@pypi.example.com/simple\n"}).refusal
    assert refusal is not None
    assert "hunter2" not in refusal.detail
    assert "bob" not in refusal.detail


@pytest.mark.parametrize(
    "files",
    [
        {"pom.xml": "<project/>"},
        {"requirements.txt": "discord.py==2.4.0\n", "bot.py": "import discord\n"},
        {"package.json": '{"dependencies": {"telegraf": "4.16.3"}}'},
    ],
)
def test_java_and_chat_bots_are_unsupported(files: dict[str, str | bytes]) -> None:
    assert code(files, manifest(**START)) == "BUILD_UNSUPPORTED_RUNTIME"


def test_streamlit_without_a_start_has_no_entrypoint_and_with_one_builds() -> None:
    files: dict[str, str | bytes] = {
        "requirements.txt": "streamlit==1.38.0\n",
        "app.py": "import streamlit as st\nst.title('x')\n",
    }
    analysis = run(files)
    assert analysis.framework == "streamlit"
    assert analysis.refusal is not None and analysis.refusal.code == "BUILD_NO_ENTRYPOINT"
    start = manifest(runtime={"start": "streamlit run app.py --server.port $PORT"})
    assert run(files, start).refusal is None
    assert run({**files, "Procfile": "web: streamlit run app.py\n"}).refusal is None


def test_notebooks_alone_have_no_entrypoint() -> None:
    assert code({"analysis.ipynb": "{}", "requirements.txt": "pandas\n"}) == "BUILD_NO_ENTRYPOINT"
    assert code({"analysis.ipynb": "{}", "app.py": "print(1)\n"}) is None


@pytest.mark.parametrize(
    ("files", "framework"),
    [
        ({"requirements.txt": "dash==2.18.1\n", "app.py": "x = 1\n"}, "dash"),
        ({"app.py": "from dash import Dash\n"}, "dash"),
        ({"pyproject.toml": '[project]\ndependencies = ["gradio>=4"]\n'}, "gradio"),
        ({"app.py": "from shiny import App\n"}, "shiny"),
        ({"app.py": "from flask import Flask\n"}, None),
    ],
)
def test_the_session_framework_is_detected(
    files: dict[str, str | bytes], framework: str | None
) -> None:
    analysis = run(files, manifest(**START))
    assert analysis.framework == framework
    m = manifest(**START)
    assert is_session_app(m.runtime, analysis.framework) is (framework is not None)
    assert m.runtime.sessions is False


@pytest.mark.parametrize(
    ("files", "notice"),
    [
        ({"src/auth.js": "await supabase.auth.signInWithPassword(form)\n"}, "SUPABASE_AUTH"),
        ({"app.py": "app.run(host='127.0.0.1', port=5000)\n"}, "LOCALHOST_BIND"),
        ({"app.py": "app.run()\n"}, "LOCALHOST_BIND"),
        ({"server.js": "app.listen(3000, 'localhost')\n"}, "LOCALHOST_BIND"),
        ({"requirements.txt": "apscheduler==3.10.4\n"}, "IN_PROCESS_SCHEDULER"),
        ({"package.json": '{"dependencies": {"ioredis": "5.4.1"}}'}, "KV_STORE"),
        ({"Dockerfile": "FROM python:3.12\n"}, "DOCKERFILE_IGNORED"),
    ],
)
def test_notices_warn_without_refusing(files: dict[str, str | bytes], notice: str) -> None:
    analysis = run(files, manifest(**START))
    assert analysis.refusal is None
    assert notice in analysis.notices
    assert notice in NOTICES


def test_a_server_bound_to_all_interfaces_gets_no_notice() -> None:
    files: dict[str, str | bytes] = {
        "app.py": "app.run(host='0.0.0.0', port=int(os.environ['PORT']))\n",
        "server.js": "app.listen(process.env.PORT)\n",
    }
    assert run(files, manifest(**START)).notices == ()


def test_unreadable_large_files_are_skipped() -> None:
    assert analyze([("big.bin", None), ("app.py", b"print(1)\n")], manifest(**START)) == Analysis(
        None, None, ()
    )


def test_every_refusal_code_has_a_fix_it() -> None:
    for c in (
        "BUILD_DEPENDENCY_UNRESOLVED",
        "BUILD_PRIVATE_REGISTRY",
        "BUILD_NO_ENTRYPOINT",
        "BUILD_EXITED_NONZERO",
        "BUILD_UNSUPPORTED_RUNTIME",
        "SECRET_IN_BUNDLE",
        "STATE_SQLITE_EPHEMERAL",
        "ADD_APPROVED_PACKAGE",
    ):
        assert FIX_ITS[c]


@pytest.mark.parametrize(
    ("files", "path", "named"),
    [
        ({"requirements.txt": "pytesseract==0.3.10\n"}, ".", "pytesseract needs tesseract-ocr"),
        (
            {"package.json": '{"dependencies": {"node-tesseract-ocr": "2.2.1"}}'},
            ".",
            "node-tesseract-ocr needs tesseract-ocr",
        ),
        (
            {"railpack.json": '{"buildAptPackages": ["libxmlsec1-dev"]}'},
            "railpack.json",
            "railpack.json asks for libxmlsec1-dev",
        ),
        (
            {"railpack.json": '{"deploy": {"aptPackages": ["wkhtmltopdf"]}}'},
            "railpack.json",
            "railpack.json asks for wkhtmltopdf",
        ),
    ],
)
def test_an_unlisted_system_package_stops_the_build(
    files: dict[str, str | bytes], path: str, named: str
) -> None:
    refusal = run(files, manifest(**START)).refusal
    assert refusal is not None
    assert (refusal.code, refusal.path) == ("ADD_APPROVED_PACKAGE", path)
    assert refusal.detail.startswith(named)
    assert "SSC support" in FIX_ITS["ADD_APPROVED_PACKAGE"]


def test_listed_system_packages_go_to_the_build() -> None:
    files: dict[str, str | bytes] = {
        "requirements.txt": "pdf2image==1.17.0\nWeasyPrint==62.0\n",
        "railpack.json": '{"deploy": {"aptPackages": ["fonts-liberation"]}}',
    }
    analysis = run(files, manifest(**START))
    assert analysis.refusal is None
    assert analysis.system_packages == (
        "fonts-dejavu-core",
        "fonts-liberation",
        "libharfbuzz-subset0",
        "libpango-1.0-0",
        "libpangoft2-1.0-0",
        "poppler-utils",
    )
    assert run({"app.py": "print(1)\n"}, manifest(**START)).system_packages == ()


def test_package_needs_names_each_dependency_listed_or_not() -> None:
    files = [
        ("requirements.txt", b"pdf2image\npytesseract\n"),
        ("railpack.json", b'{"buildAptPackages": ["poppler-utils"]}'),
    ]
    assert [(n.path, n.by, n.package, n.listed) for n in package_needs(files)] == [
        (".", "pdf2image", "poppler-utils", True),
        (".", "pytesseract", "tesseract-ocr", False),
        ("railpack.json", "railpack.json", "poppler-utils", True),
    ]

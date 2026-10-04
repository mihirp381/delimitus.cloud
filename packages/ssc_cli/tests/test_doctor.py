"""``ssc doctor``: each rule fires on its synthetic fixture, and only there."""

import json
from pathlib import Path
from typing import get_args

import pytest

from ssc_cli.doctor import run_doctor
from ssc_cli.doctor.finding import FIX, SEVERITY, DoctorCode
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import DoctorResult
from ssc_contracts.manifest import MAX_MANIFEST_BYTES, ManifestError, load_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor"
CORPUS = Path(__file__).resolve().parents[3] / "spikes" / "corpus20" / "RESULTS.md"
CODES = sorted(SEVERITY)


def codes(root: Path) -> list[str]:
    return [f.code for f in run_doctor(root)]


MANIFEST = 'schema = "ssc/v1"\n'


def make(root: Path, files: dict[str, str | None]) -> Path:
    """Write ``files`` under ``root``, with a minimal ssc.toml unless it is given (None: none)."""
    for rel, text in {"ssc.toml": MANIFEST, **files}.items():
        if text is None:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_every_code_has_a_fixture_and_a_fix():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == sorted([*CODES, "clean"])
    assert set(FIX) == set(SEVERITY) == set(get_args(DoctorCode))


@pytest.mark.parametrize("code", CODES)
def test_doctor_top_six(cli, code):
    folder = FIXTURES / code
    assert codes(folder) == [code]
    r = cli("doctor", str(folder), "--json")
    result = DoctorResult.model_validate(r.json())
    assert [f.code for f in result.findings] == [code]
    blocking = SEVERITY[code] == "block"
    assert result.blocking is blocking
    assert r.code == (ExitCode.BLOCKED if blocking else ExitCode.OK)


def test_clean_fixture_passes(cli):
    assert codes(FIXTURES / "clean") == []
    r = cli("doctor", str(FIXTURES / "clean"))
    assert r.code == 0
    assert "No problems found" in r.stdout


def test_findings_point_at_the_line(cli):
    r = cli("doctor", str(FIXTURES / "PORT_BINDING"), "--json")
    (f,) = r.json()["findings"]
    assert (f["path"], f["line"]) == ("app.py", 12)
    assert f["fix"] == FIX["PORT_BINDING"]


def test_human_output_names_code_place_and_fix(cli):
    r = cli("doctor", str(FIXTURES / "PORT_BINDING"))
    assert r.code == ExitCode.BLOCKED
    assert "BLOCK  PORT_BINDING  app.py:12" in r.stdout
    assert "Fix: " in r.stdout
    assert "1 blocking, 0 warnings." in r.stdout


def test_missing_folder_is_a_usage_error(cli, tmp_path):
    r = cli("doctor", str(tmp_path / "nope"), "--json")
    assert r.code == ExitCode.USAGE
    assert r.stdout == ""


def test_blocking_findings_come_first(tmp_path):
    make(
        tmp_path,
        {
            "requirements.txt": "flask\npymongo\n",
            "app.py": "from flask import Flask\napp = Flask(__name__)\napp.run()\n",
        },
    )
    assert codes(tmp_path) == ["PORT_BINDING", "EXTERNAL_SERVICE"]


# ── lock files ──────────────────────────────────────────────────────────────

PKG = json.dumps({"scripts": {"start": "node server.js"}, "dependencies": {"express": "^5"}})
SERVER = "require('express')().listen(process.env.PORT)\n"


def test_lock_files_from_two_tools(tmp_path):
    lock = json.dumps({"packages": {"node_modules/express": {}}})
    make(
        tmp_path,
        {"package.json": PKG, "server.js": SERVER, "package-lock.json": lock, "yarn.lock": ""},
    )
    found = run_doctor(tmp_path)
    assert [f.code for f in found] == ["LOCKFILE_STALE", "LOCKFILE_STALE"]
    assert "more than one tool" in found[0].message


BUN_LOCK = """{
  "lockfileVersion": 1,
  "workspaces": {
    "": {
      "name": "x",
      "dependencies": {
        "express": "%s",
      },
    },
  },
  "packages": {
    "express": ["express@5.1.0", "", {}, "sha512-x"],
  },
}
"""


@pytest.mark.parametrize(("spec", "stale"), [("^5", False), ("^4", True)])
def test_bun_lock_records_the_declared_ranges(tmp_path, spec, stale):
    make(tmp_path, {"package.json": PKG, "server.js": SERVER, "bun.lock": BUN_LOCK % spec})
    found = run_doctor(tmp_path)
    assert [(f.code, f.path) for f in found] == ([("LOCKFILE_STALE", "bun.lock")] if stale else [])
    if stale:
        assert "express" in found[0].message


def test_local_packages_are_not_expected_in_the_lock(tmp_path):
    pkg = json.dumps(
        {
            "scripts": {"start": "node server.js"},
            "dependencies": {"express": "^5", "shared": "workspace:*", "lib": "file:../lib"},
        }
    )
    lock = json.dumps({"packages": {"node_modules/express": {}}})
    make(tmp_path, {"package.json": pkg, "server.js": SERVER, "package-lock.json": lock})
    assert codes(tmp_path) == []


def test_uv_lock_missing_a_declared_dependency(tmp_path):
    make(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = ["fastapi>=0.1", "Jinja2"]\n',
            "uv.lock": '[[package]]\nname = "fastapi"\nversion = "1"\n',
            "main.py": "print('hi')\n",
        },
    )
    (f,) = run_doctor(tmp_path)
    assert (f.code, f.path) == ("LOCKFILE_STALE", "uv.lock")
    assert "jinja2" in f.message


def test_unreadable_lock_file(tmp_path):
    make(
        tmp_path,
        {"pyproject.toml": '[project]\nname = "x"\n', "uv.lock": "[[", "main.py": ""},
    )
    assert codes(tmp_path) == ["LOCKFILE_STALE"]


# ── public build-time variables ─────────────────────────────────────────────

VITE_PKG = json.dumps({"scripts": {"build": "vite build"}, "devDependencies": {"vite": "^7"}})


@pytest.mark.parametrize(
    "tables",
    [
        '[build.public_env.preview]\nVITE_API_URL = "https://a"\n'
        '[build.public_env.prod]\nVITE_API_URL = "https://b"\n',
        '[build.public_env.prod]\nVITE_API_URL = "https://b"\n',
    ],
)
def test_public_env_declared_in_the_manifest_is_fine(tmp_path, tables):
    make(
        tmp_path,
        {
            "package.json": VITE_PKG,
            "src/main.js": "fetch(import.meta.env.VITE_API_URL)\n",
            "ssc.toml": MANIFEST + tables,
        },
    )
    assert codes(tmp_path) == []


def test_public_env_in_another_form_is_refused_and_not_counted(tmp_path):
    make(
        tmp_path,
        {
            "package.json": VITE_PKG,
            "src/main.js": "fetch(import.meta.env.VITE_API_URL)\n",
            "ssc.toml": MANIFEST + '[build]\npublic_env = ["VITE_API_URL"]\n',
        },
    )
    assert codes(tmp_path) == ["MANIFEST_INVALID", "PUBLIC_ENV_AT_BUILD"]


def test_each_public_name_is_reported_once(tmp_path):
    src = "a(import.meta.env.VITE_A)\nb(import.meta.env.VITE_A)\nc(process.env.NEXT_PUBLIC_B)\n"
    make(tmp_path, {"package.json": VITE_PKG, "src/main.js": src})
    found = run_doctor(tmp_path)
    assert [(f.code, f.line) for f in found] == [
        ("PUBLIC_ENV_AT_BUILD", 1),
        ("PUBLIC_ENV_AT_BUILD", 3),
    ]
    assert found[0].message.startswith("VITE_A ")
    assert found[1].message.startswith("NEXT_PUBLIC_B ")


# ── start command and port ──────────────────────────────────────────────────


def test_flask_on_all_interfaces_reading_port_passes(tmp_path):
    app = (
        "import os\nfrom flask import Flask\napp = Flask(__name__)\n"
        'app.run(host="0.0.0.0", port=int(os.environ["PORT"]))\n'
    )
    make(tmp_path, {"requirements.txt": "flask\n", "app.py": app})
    assert codes(tmp_path) == []


def test_flask_on_localhost_reading_port_still_blocks(tmp_path):
    app = 'import os\napp.run(host="127.0.0.1", port=int(os.environ["PORT"]))\n'
    make(tmp_path, {"requirements.txt": "flask\n", "app.py": app})
    (f,) = run_doctor(tmp_path)
    assert f.code == "PORT_BINDING"
    assert "localhost only" in f.message
    assert "PORT" not in f.message


def test_procfile_passing_port_decides_the_binding(tmp_path):
    make(
        tmp_path,
        {
            "requirements.txt": "uvicorn\n",
            "main.py": "import uvicorn\nuvicorn.run(app)\n",
            "Procfile": "web: uvicorn main:app --host 0.0.0.0 --port $PORT\n",
        },
    )
    assert codes(tmp_path) == []


def test_streamlit_with_a_procfile_passes(tmp_path):
    make(
        tmp_path,
        {
            "requirements.txt": "streamlit\n",
            "app.py": "import streamlit as st\n",
            "Procfile": "web: streamlit run app.py --server.port $PORT --server.address 0.0.0.0\n",
        },
    )
    assert codes(tmp_path) == ["SESSION_FRAMEWORK"]


def test_node_listen_on_localhost(tmp_path):
    make(
        tmp_path,
        {
            "package.json": json.dumps({"scripts": {"start": "node server.js"}}),
            "server.js": "app.listen(process.env.PORT, 'localhost')\n",
            "server.test.js": "app.listen(3000)\n",
        },
    )
    assert [(f.code, f.path) for f in run_doctor(tmp_path)] == [("PORT_BINDING", "server.js")]


def test_package_json_without_any_entry(tmp_path):
    make(tmp_path, {"package.json": json.dumps({"name": "x"})})
    assert codes(tmp_path) == ["NO_START_COMMAND"]


def test_vite_app_with_a_build_script_has_a_start(tmp_path):
    make(tmp_path, {"package.json": VITE_PKG, "index.html": "<div id=app></div>"})
    assert codes(tmp_path) == []


# ── one app ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "files",
    [
        {"dist/index.html": "<p>hi</p>"},
        {"index.html": "<p>hi</p>"},
        {"README.md": "nothing here"},
        {"package.json": json.dumps({"workspaces": ["a", "b"]})},
        {"pyproject.toml": '[tool.uv.workspace]\nmembers = ["a"]\n'},
        {"lerna.json": "{}"},
        {"api/requirements.txt": "flask\n", "web/package.json": "{}"},
    ],
)
def test_not_single_app(tmp_path, files):
    make(tmp_path, files)
    assert codes(tmp_path) == ["NOT_SINGLE_APP"]


def test_go_app_counts_as_one_app(tmp_path):
    make(tmp_path, {"go.mod": "module x\n", "main.go": "package main\n"})
    assert codes(tmp_path) == []


def test_dependency_folders_are_not_scanned(tmp_path):
    make(
        tmp_path,
        {
            "package.json": PKG,
            "server.js": SERVER,
            "node_modules/x/index.js": "require('os').homedir(); app.listen(80, 'localhost')\n",
            ".venv/lib/y.py": "from pathlib import Path\nPath.home()\n",
        },
    )
    assert codes(tmp_path) == []


# ── the manifest ────────────────────────────────────────────────────────────


def manifest_findings(root: Path) -> list[tuple[str, int | None, str]]:
    return [(f.code, f.line, f.message) for f in run_doctor(root) if f.code.startswith("MANIFEST")]


def test_invalid_manifest_lists_every_refusal_from_the_loader(tmp_path):
    text = MANIFEST + '\n[runtime]\nhelth_path = "/"\n\n[state]\nredis = true\n'
    make(tmp_path, {"package.json": PKG, "server.js": SERVER, "ssc.toml": text})
    with pytest.raises(ManifestError) as refused:
        load_manifest(text)
    assert manifest_findings(tmp_path) == [
        ("MANIFEST_INVALID", p.line, str(p)) for p in refused.value.problems
    ]
    messages = [m for _, _, m in manifest_findings(tmp_path)]
    assert messages[0].startswith("ssc.toml:4:1: runtime.helth_path: unknown key")
    assert messages[1].startswith("ssc.toml:7:1: state.redis: ")
    assert "STATE_KV_UNSUPPORTED" in messages[1]


def test_state_as_a_string_is_refused(tmp_path):
    make(tmp_path, {"package.json": PKG, "server.js": SERVER, "ssc.toml": 'state = "postgres"\n'})
    found = manifest_findings(tmp_path)
    assert [code for code, _, _ in found] == ["MANIFEST_INVALID", "MANIFEST_INVALID"]
    assert any(m.startswith("ssc.toml:1:1: schema: ") for _, _, m in found)
    assert any("postgres = true" in m for _, _, m in found)


def test_invalid_manifest_human_output(cli, tmp_path):
    text = 'schema = "ssc/v1"\n[runtime]\nport = "80"\nsessions = "yes"\n'
    make(tmp_path, {"package.json": PKG, "server.js": SERVER, "ssc.toml": text})
    r = cli("doctor", str(tmp_path))
    assert r.code == ExitCode.BLOCKED
    assert "BLOCK  MANIFEST_INVALID  ssc.toml:3\n       ssc.toml:3:1: runtime.port: " in r.stdout
    assert "ssc.toml:4:1: runtime.sessions: " in r.stdout
    assert r.stdout.count("Fix: ") == 1
    assert "2 blocking, 0 warnings." in r.stdout


@pytest.mark.parametrize(
    ("data", "field"),
    [
        (b'schema = "ssc/v1"\n# caf\xe9\n', "(file)"),
        (b'schema = "ssc/v1"\n' + b"#" * (MAX_MANIFEST_BYTES + 10), "(file)"),
        (b'schema = "ssc/v1\n', "syntax"),
        (b'schema = "ssc/v2"\n', "schema"),
    ],
)
def test_manifest_the_loader_cannot_read(tmp_path, data, field):
    make(tmp_path, {"package.json": PKG, "server.js": SERVER, "ssc.toml": None})
    (tmp_path / "ssc.toml").write_bytes(data)
    ((code, _, message),) = manifest_findings(tmp_path)
    assert code == "MANIFEST_INVALID"
    assert f": {field}: " in message


def test_manifest_with_a_bom_is_read_by_every_rule(tmp_path):
    start = '[runtime]\nstart = "streamlit run app.py --server.port $PORT"\n'
    make(
        tmp_path,
        {
            "requirements.txt": "streamlit\n",
            "app.py": "import streamlit as st\n",
            "ssc.toml": None,
        },
    )
    (tmp_path / "ssc.toml").write_text("\ufeff" + MANIFEST + start)
    assert [(f.code, f.path) for f in run_doctor(tmp_path)] == [("SESSION_FRAMEWORK", "ssc.toml")]


def test_missing_manifest_is_not_reported_for_a_folder_that_is_not_one_app(tmp_path):
    make(tmp_path, {"api/requirements.txt": "flask\n", "web/package.json": "{}", "ssc.toml": None})
    assert codes(tmp_path) == ["NOT_SINGLE_APP"]


RUNTIME_SIX = {
    "lock file does not match the dependency list": "LOCKFILE_STALE",
    "public variable read at build time": "PUBLIC_ENV_AT_BUILD",
    "no way to start the app": "NO_START_COMMAND",
    "database or login service SSC does not provide": "EXTERNAL_SERVICE",
    "writes to the home folder": "WRITES_HOME",
    "not a single app at the top of the folder": "NOT_SINGLE_APP",
}
STATIC_SIX = {
    "needs build-time public variable": "PUBLIC_ENV_AT_BUILD",
    "uses SQLite on disk": "STATE_SQLITE_EPHEMERAL",
    "needs Supabase auth": "EXTERNAL_SERVICE",
    "binds to localhost only": "PORT_BINDING",
    "hard-coded key or password": "SECRET_IN_BUNDLE",
    "needs a native library": "NATIVE_LIBRARY",
}


@pytest.mark.parametrize(
    ("cause", "code"), [*RUNTIME_SIX.items(), *STATIC_SIX.items()], ids=lambda v: str(v)
)
def test_each_ssc_003_cause_has_a_code_that_catches_it(cause, code):
    assert codes(FIXTURES / code) == [code], cause


def test_the_static_six_are_the_corpus_buckets():
    text = CORPUS.read_text().split("## Ranked causes, static scan", 1)[1].split("\n\n")[1]
    rows = [line.split("|")[1].strip() for line in text.splitlines()[2:]]
    assert rows == list(STATIC_SIX)


def test_supabase_auth_is_caught_as_an_external_service(tmp_path):
    pkg = json.loads(VITE_PKG) | {"dependencies": {"@supabase/supabase-js": "^2"}}
    make(tmp_path, {"package.json": json.dumps(pkg), "index.html": "<div></div>"})
    assert codes(tmp_path) == ["EXTERNAL_SERVICE"]


FLASK_OK = 'import os\napp.run(host="0.0.0.0", port=int(os.environ["PORT"]))\n'


@pytest.mark.parametrize(
    "files",
    [
        {"db.py": "import sqlite3\nconn = sqlite3.connect(DB_PATH)\n"},
        {"db.py": "engine = create_engine('sqlite:///app.db')\n"},
        {"data/app.sqlite3": ""},
    ],
)
def test_sqlite_on_disk_blocks(cli, tmp_path, files):
    make(tmp_path, {"requirements.txt": "flask\n", "app.py": FLASK_OK, **files})
    (f,) = run_doctor(tmp_path)
    assert (f.code, f.severity, f.path) == ("STATE_SQLITE_EPHEMERAL", "block", next(iter(files)))
    assert "[state]\npostgres = true" in f.fix
    assert cli("doctor", str(tmp_path)).code == ExitCode.BLOCKED


@pytest.mark.parametrize(
    "files",
    [
        {"db.py": "conn = sqlite3.connect(':memory:')\n"},
        {"db.py": "conn = sqlite3.connect('file::memory:?cache=shared', uri=True)\n"},
        {"db.py": "engine = create_engine('sqlite:///:memory:')\n"},
        {"tests/test_db.py": "conn = sqlite3.connect('t.db')\n"},
        {"tests/fixtures/sample.sqlite": ""},
        {"notes.md": "We moved off sqlite3.connect('old.db') last year.\n"},
    ],
)
def test_in_memory_and_test_sqlite_pass(tmp_path, files):
    make(tmp_path, {"requirements.txt": "flask\n", "app.py": FLASK_OK, **files})
    assert codes(tmp_path) == []


def test_a_sqlite_url_is_a_local_fallback_once_postgres_is_on(tmp_path):
    db = "url = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')\n"
    files = {"requirements.txt": "flask\n", "app.py": FLASK_OK, "db.py": db}
    make(tmp_path, files)
    assert codes(tmp_path) == ["STATE_SQLITE_EPHEMERAL"]
    make(tmp_path, {**files, "ssc.toml": MANIFEST + "[state]\npostgres = true\n"})
    assert codes(tmp_path) == []


def test_a_file_left_out_by_sscignore_is_not_read(tmp_path):
    make(
        tmp_path,
        {
            "requirements.txt": "flask\n",
            "app.py": FLASK_OK,
            "local.sqlite": "",
            ".sscignore": "*.sqlite\n",
        },
    )
    assert codes(tmp_path) == []


def test_a_secret_is_reported_by_rule_and_line_without_its_value(tmp_path):
    key = "AKIA" + "QWERTYUIOPASDFGH"
    make(
        tmp_path, {"package.json": PKG, "server.js": SERVER, "config.js": f"\nconst k = '{key}'\n"}
    )
    (f,) = run_doctor(tmp_path)
    assert (f.code, f.path, f.line) == ("SECRET_IN_BUNDLE", "config.js", 2)
    assert "aws_access_key" in f.message
    assert key[:4] not in f.message
    assert "ssc secret set" in f.fix


def test_a_secret_in_an_env_file_or_an_ignored_file_is_not_reported(tmp_path):
    key = "AKIA" + "QWERTYUIOPASDFGH"
    make(
        tmp_path,
        {
            "package.json": PKG,
            "server.js": SERVER,
            ".env": f"KEY={key}\n",
            "scratch/k.js": f"'{key}'\n",
            ".sscignore": "scratch/\n",
        },
    )
    assert codes(tmp_path) == []


@pytest.mark.parametrize(
    ("requirement", "found"), [("psycopg2==2.9.9", True), ("psycopg2-binary", False)]
)
def test_python_native_library_is_a_note(tmp_path, requirement, found):
    make(tmp_path, {"requirements.txt": f"flask\n{requirement}\n", "app.py": FLASK_OK})
    expected = [("NATIVE_LIBRARY", "info", "requirements.txt", 2)] if found else []
    assert [(f.code, f.severity, f.path, f.line) for f in run_doctor(tmp_path)] == expected


def test_a_streamlit_folder_gets_session_framework_and_still_deploys(cli):
    folder = FIXTURES / "SESSION_FRAMEWORK"
    r = cli("doctor", str(folder))
    assert r.code == ExitCode.OK
    assert "INFO   SESSION_FRAMEWORK  ." in r.stdout
    assert "one instance" in r.stdout
    assert "connections drop at 60 minutes" in r.stdout
    assert "Streamlit loses its session state then and the page reloads." in r.stdout
    assert "0 blocking, 0 warnings, 1 note." in r.stdout
    assert cli("doctor", str(folder), "--json").json()["blocking"] is False


@pytest.mark.parametrize(
    ("files", "name"),
    [
        (
            {"app.py": "import gradio as gr\ndemo.launch()\n", "requirements.txt": "gradio\n"},
            "Gradio",
        ),
        ({"app.py": "from dash import Dash\n", "requirements.txt": "dash\n"}, "Dash"),
        ({"app.py": "from shiny import App\n", "requirements.txt": "shiny\n"}, "Shiny"),
    ],
)
def test_each_session_framework_is_seen_as_the_build_sees_it(tmp_path, files, name):
    start = '[runtime]\nstart = "python app.py"\n'
    make(tmp_path, {**files, "ssc.toml": MANIFEST + start})
    (f,) = [f for f in run_doctor(tmp_path) if f.code == "SESSION_FRAMEWORK"]
    assert f.message.startswith(f"This is a {name} app,")
    assert "Streamlit" not in f.message


def test_sessions_true_is_a_session_app_and_flask_is_not(tmp_path):
    files = {"requirements.txt": "flask\n", "app.py": FLASK_OK}
    make(tmp_path, files)
    assert codes(tmp_path) == []
    make(tmp_path, {**files, "ssc.toml": MANIFEST + "[runtime]\nsessions = true\n"})
    (f,) = run_doctor(tmp_path)
    assert (f.code, f.path) == ("SESSION_FRAMEWORK", "ssc.toml")
    assert f.message.startswith("ssc.toml sets sessions = true,")


def test_a_streamlit_app_without_a_start_command_gets_only_the_block(tmp_path):
    make(tmp_path, {"requirements.txt": "streamlit\n", "app.py": "import streamlit as st\n"})
    assert codes(tmp_path) == ["NO_START_COMMAND"]


@pytest.mark.parametrize(
    ("files", "path", "said"),
    [
        (
            {"requirements.txt": "flask\npytesseract\n"},
            ".",
            "pytesseract needs the system package tesseract-ocr",
        ),
        (
            {"railpack.json": '{"buildAptPackages": ["libldap2-dev"]}'},
            "railpack.json",
            "railpack.json asks for the system package libldap2-dev",
        ),
    ],
)
def test_a_system_package_off_the_platform_list_blocks_as_the_build_does(
    tmp_path, files, path, said
):
    make(tmp_path, {"requirements.txt": "flask\n", "app.py": FLASK_OK, **files})
    (f,) = [f for f in run_doctor(tmp_path) if f.code == "ADD_APPROVED_PACKAGE"]
    assert (f.severity, f.path) == ("block", path)
    assert f.message.startswith(said)
    assert "SSC support" in f.fix


def test_a_listed_system_package_is_not_reported(tmp_path):
    make(tmp_path, {"requirements.txt": "flask\npdf2image\n", "app.py": FLASK_OK})
    assert codes(tmp_path) == []

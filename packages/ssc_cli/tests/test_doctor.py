"""``ssc doctor``: each rule fires on its synthetic fixture, and only there."""

import json
from pathlib import Path

import pytest

from ssc_cli.doctor import run_doctor
from ssc_cli.doctor.finding import FIX, SEVERITY
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import DoctorResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "doctor"
CODES = sorted(SEVERITY)


def codes(root: Path) -> list[str]:
    return [f.code for f in run_doctor(root)]


def make(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_every_code_has_a_fixture_and_a_fix():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) == sorted([*CODES, "clean"])
    assert set(FIX) == set(SEVERITY)


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
    "manifest",
    [
        '[build.public_env.preview]\nVITE_API_URL = "https://a"\n'
        '[build.public_env.prod]\nVITE_API_URL = "https://b"\n',
        '[build.public_env]\nVITE_API_URL = "https://a"\n',
        '[build]\npublic_env = ["VITE_API_URL"]\n',
    ],
)
def test_public_env_declared_in_the_manifest_is_fine(tmp_path, manifest):
    make(
        tmp_path,
        {
            "package.json": VITE_PKG,
            "src/main.js": "fetch(import.meta.env.VITE_API_URL)\n",
            "ssc.toml": manifest,
        },
    )
    assert codes(tmp_path) == []


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
    assert codes(tmp_path) == []


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

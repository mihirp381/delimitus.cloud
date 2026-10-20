"""SSC-015: the Cloud Build config, its step scripts run locally, gitleaks, and status mapping."""

import base64
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from ssc_agent.__main__ import (
    BUILD_ENV,
    BUILDKIT_ENV,
    ENV,
    POOL_ENV,
    ConfigError,
    build_config_from_env,
    cell_from_env,
)
from ssc_agent.cloud_build import (
    APP_USER,
    BUILD_DRIVER_ERROR,
    BUILD_TIMED_OUT,
    BUILDKIT_IMAGE,
    DEPENDENCY_UNRESOLVED,
    HOST_NOT_ALLOWED,
    PRIVATE_REGISTRY,
    SCRIPTS,
    STEP_IDS,
    CellBuildConfig,
    build_config,
    gitleaks_config,
    pooled_scripts,
    status_of,
)
from ssc_contracts.egress import BUILD_HOSTS, BUILD_NO_PROXY_VALUE
from ssc_shared.build import CellBuild, Failed, Running, Succeeded

PROJECT = "ssc-c-test"
REPO = f"us-central1-docker.pkg.dev/{PROJECT}/ssc-apps/apps"
TOOLS = "us-central1-docker.pkg.dev/ssc-platform/tools/ssc-build-tools@sha256:" + "a" * 64
FRONTEND = "ghcr.io/railwayapp/railpack-frontend@sha256:" + "f" * 64
CELL = CellBuildConfig(
    project=PROJECT,
    region="us-central1",
    image_repository=REPO,
    service_account=f"ssc-build@{PROJECT}.iam.gserviceaccount.com",
    tools_image=TOOLS,
    frontend_image=FRONTEND,
)
BUILD_ID = "bld_" + "0" * 20
URL = "https://blobs.test/v1/blobs/bundles/o/a/sha256/x.tar.gz?m=GET&sig=s"
FIXTURES = Path(__file__).resolve().parents[3] / "conformance" / "build_fixtures"
LOGS = Path(__file__).resolve().parent / "build_logs"
needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks missing")
needs_railpack = pytest.mark.skipif(shutil.which("railpack") is None, reason="railpack missing")


def cell_build(**overrides: Any) -> CellBuild:
    fields: dict[str, Any] = {
        "build_id": BUILD_ID,
        "bundle_url": URL,
        "bundle_sha256": "b" * 64,
        "public_env": {"VITE_API": "https://api.example.com"},
        "start": "gunicorn app:server --bind 0.0.0.0:$PORT",
    }
    return CellBuild(**{**fields, **overrides})


def step_env(config: dict[str, Any], step: str) -> dict[str, str]:
    (found,) = [s for s in config["steps"] if s["id"] == step]
    return dict(e.replace("$$", "$").split("=", 1) for e in found["env"])


def run_step(
    step: str, workspace: Path, env: dict[str, str], path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    script = SCRIPTS[step].replace("/workspace", str(workspace))
    search = os.pathsep.join(([str(path)] if path else []) + [os.environ["PATH"]])
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        env={"PATH": search, "HOME": str(workspace), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def fake_docker(bin_dir: Path, *, log: Path | None = None, exit_code: int = 0) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    out = bin_dir / "docker-args"
    tool = bin_dir / "docker"
    created = bin_dir / "buildx-create-args"
    body = f'if [ "$1 $2" = "buildx create" ]; then printf "%s\\n" "$@" > {created}; exit 0; fi\n'
    body += f'printf "%s\\n" "$@" > {out}\n'
    if log is not None:
        body += f"cat {log}\n"
    tool.write_text(f"#!/bin/bash\n{body}exit {exit_code}\n")
    tool.chmod(0o755)
    return out


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


# ── the build resource ──────────────────────────────────────────────────────


def test_the_build_runs_as_ssc_build_on_the_tools_image_and_pushes_one_image() -> None:
    config = build_config(CELL, cell_build())
    assert "source" not in config
    assert config["serviceAccount"] == (
        f"projects/{PROJECT}/serviceAccounts/ssc-build@{PROJECT}.iam.gserviceaccount.com"
    )
    assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert config["images"] == [f"{REPO}:{BUILD_ID}"]
    assert config["tags"] == ["ssc-build", BUILD_ID]
    assert [s["id"] for s in config["steps"]] == list(STEP_IDS)
    assert {s["name"] for s in config["steps"]} == {TOOLS}
    assert [s["waitFor"] for s in config["steps"]] == [["-"], *[[p] for p in STEP_IDS[:-1]]]
    assert step_env(config, "build")["SSC_FRONTEND"] == FRONTEND
    assert step_env(config, "harden") == {
        "SSC_APP_USER": APP_USER,
        "SSC_IMAGE": f"{REPO}:{BUILD_ID}",
    }


def test_the_bundle_url_is_the_only_storage_the_build_sees() -> None:
    text = repr(build_config(CELL, cell_build()))
    assert "gs://" not in text
    assert "storage.googleapis.com" not in text
    assert text.count(URL) == 1
    assert step_env(build_config(CELL, cell_build()), "fetch")["SSC_BUNDLE_URL"] == URL


def test_every_dollar_is_escaped_from_cloud_build_substitution() -> None:
    config = build_config(CELL, cell_build(start="node server.js --port $PORT"))
    for step in config["steps"]:
        for text in [*step["args"], *step["env"]]:
            assert "$" not in text.replace("$$", "")
    assert step_env(config, "plan")["SSC_START"] == "node server.js --port $PORT"


def test_public_values_reach_the_plan_as_nul_separated_pairs() -> None:
    env = {"VITE_API": "https://api.example.com", "VITE_NOTE": "a=b c"}
    config = build_config(CELL, cell_build(public_env=env))
    pairs = base64.b64decode(step_env(config, "plan")["SSC_PUBLIC_ENV"]).decode()
    assert pairs == "VITE_API=https://api.example.com\0VITE_NOTE=a=b c\0"


@pytest.mark.parametrize("field", ["tools_image", "frontend_image"])
def test_images_must_be_pinned_by_digest(field: str) -> None:
    with pytest.raises(ValueError, match="pinned"):
        CellBuildConfig(
            **{
                "project": PROJECT,
                "region": "us-central1",
                "image_repository": REPO,
                "service_account": CELL.service_account,
                "tools_image": TOOLS,
                "frontend_image": FRONTEND,
                field: "ghcr.io/railwayapp/railpack-frontend:latest",
            }
        )


def test_build_env_is_all_or_none() -> None:
    cell = cell_from_env({name: f"value-{field}" for field, name in ENV.items()})
    assert build_config_from_env({}, cell) is None
    full = {
        "SSC_BUILD_SA": CELL.service_account,
        "SSC_BUILD_TOOLS_IMAGE": TOOLS,
        "SSC_BUILD_FRONTEND_IMAGE": FRONTEND,
    }
    assert set(full) == set(BUILD_ENV.values())
    config = build_config_from_env(full, cell)
    assert config is not None
    assert config.image_repository == cell.image_repository
    with pytest.raises(ConfigError, match="SSC_BUILD_FRONTEND_IMAGE"):
        build_config_from_env(
            {k: v for k, v in full.items() if k != "SSC_BUILD_FRONTEND_IMAGE"}, cell
        )
    with pytest.raises(ConfigError, match="pinned"):
        build_config_from_env({**full, "SSC_BUILD_TOOLS_IMAGE": "debian:13"}, cell)


# ── status ──────────────────────────────────────────────────────────────────


def failure(detail: str, failed_step: str | None = None) -> dict[str, Any]:
    steps = [{"id": s, "status": "FAILURE" if s == failed_step else "SUCCESS"} for s in STEP_IDS]
    return {"id": "x", "status": "FAILURE", "failureInfo": {"detail": detail}, "steps": steps}


@pytest.mark.parametrize(
    ("exit_code", "code"),
    [
        (10, "SECRET_IN_BUNDLE"),
        (11, "BUILD_DEPENDENCY_UNRESOLVED"),
        (12, "BUILD_PRIVATE_REGISTRY"),
        (13, "BUILD_NO_ENTRYPOINT"),
        (14, "BUILD_EXITED_NONZERO"),
        (16, "BUILD_HOST_NOT_ALLOWED"),
    ],
)
def test_a_step_exit_code_is_the_reason(exit_code: int, code: str) -> None:
    detail = (
        'Build step failure: build step 3 "build" failed: '
        f"step exited with non-zero status: {exit_code}"
    )
    status = status_of(failure(detail))
    assert isinstance(status, Failed)
    assert status.code == code


@pytest.mark.parametrize(
    ("step", "code"),
    [
        ("build", "BUILD_EXITED_NONZERO"),
        ("fetch", BUILD_DRIVER_ERROR),
        ("harden", BUILD_DRIVER_ERROR),
    ],
)
def test_an_unknown_exit_falls_back_on_the_failing_step(step: str, code: str) -> None:
    status = status_of(failure("step exited with non-zero status: 137", step))
    assert isinstance(status, Failed)
    assert status.code == code


def test_running_success_timeout_and_cancel() -> None:
    for state in ("QUEUED", "WORKING", "PENDING"):
        assert status_of({"status": state}) == Running()
    digest = "sha256:" + "c" * 64
    ok = {"status": "SUCCESS", "id": "x", "results": {"images": [{"name": "i", "digest": digest}]}}
    assert status_of(ok, "i") == Succeeded(image_digest=digest, scan_refs=("gitleaks:x",))
    no_image = status_of({"status": "SUCCESS", "id": "x"})
    assert isinstance(no_image, Failed) and no_image.code == BUILD_DRIVER_ERROR
    timed_out = status_of({"status": "TIMEOUT", "id": "x"})
    assert isinstance(timed_out, Failed) and timed_out.code == BUILD_TIMED_OUT
    for state in ("CANCELLED", "EXPIRED", "INTERNAL_ERROR"):
        ended = status_of({"status": state, "id": "x"})
        assert isinstance(ended, Failed) and ended.code == BUILD_DRIVER_ERROR


# ── the step scripts, run under bash ────────────────────────────────────────


def tar_gz(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_fetch_checks_the_sha256_before_unpacking(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(tar_gz({"app.py": "print(1)\n"}))
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    url = f"file://{bundle}"
    good = run_step("fetch", tmp_path / "w1", {"SSC_BUNDLE_URL": url, "SSC_BUNDLE_SHA256": sha})
    assert good.returncode == 0, good.stderr
    assert (tmp_path / "w1" / "src" / "app.py").read_text() == "print(1)\n"
    bad = run_step("fetch", tmp_path / "w2", {"SSC_BUNDLE_URL": url, "SSC_BUNDLE_SHA256": "0" * 64})
    assert bad.returncode == 1
    assert not (tmp_path / "w2" / "src" / "app.py").exists()


@needs_gitleaks
def test_scan_stops_the_build_on_cs_flask_hello(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "cs-flask-hello", tmp_path / "src")
    (tmp_path / ".ssc").mkdir()
    env = step_env(build_config(CELL, cell_build()), "scan")
    result = run_step("scan", tmp_path, env)
    assert result.returncode == 10, result.stdout + result.stderr
    assert "FAKEPASSWORD" not in result.stdout + result.stderr


@needs_gitleaks
@pytest.mark.parametrize(
    "fixture", ["cs-fastapi-hello", "cs-express-hello", "cn-adopt-supabase-migrations", "dash-app"]
)
def test_scan_passes_clean_fixtures(tmp_path: Path, fixture: str) -> None:
    shutil.copytree(FIXTURES / fixture, tmp_path / "src")
    (tmp_path / ".ssc").mkdir()
    result = run_step("scan", tmp_path, step_env(build_config(CELL, cell_build()), "scan"))
    assert result.returncode == 0, result.stdout + result.stderr


def jwt(role: str) -> str:
    def part(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    payload = f'{{"iss":"supabase","ref":"abcdefghijklmnopqrst","role":"{role}","iat":1700000000}}'
    return f"{part('{"alg":"HS256","typ":"JWT"}')}.{part(payload)}.{part('s' * 32)}"


def gitleaks(src: Path, config: str) -> int:
    cfg = src.parent / "gitleaks.toml"
    cfg.write_text(config)
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gitleaks",
            "dir",
            str(src),
            "--config",
            str(cfg),
            "--redact",
            "--no-banner",
            "--max-decode-depth",
            "2",
            "--exit-code",
            "10",
        ],
        capture_output=True,
        check=False,
    ).returncode


@needs_gitleaks
@pytest.mark.parametrize(
    "planted",
    [
        lambda: f'SUPABASE_KEY = "{jwt("service_role")}"\n',
        lambda: f'KEY = "sb_secret_{"Q7" * 12}"\n',
        lambda: 'DB = "postgresql://app:' + "Zx9" * 6 + '@db.prod.example.com:5432/app"\n',
        lambda: 'AWS = "AKIA' + "QWERTYUIOPASDFGH" + '"\n',
    ],
)
def test_a_planted_secret_blocks_the_scan(tmp_path: Path, planted: Any) -> None:
    write_tree(tmp_path / "src", {"app.py": planted()})
    assert gitleaks(tmp_path / "src", gitleaks_config([])) == 10


@needs_gitleaks
def test_public_and_placeholder_values_pass_the_scan(tmp_path: Path) -> None:
    maps_key = "AIza" + "SyBq7Xk2Lm9Pz4Rt8Wv1Nc6Hd3Fg5Js0Ue"
    write_tree(
        tmp_path / "src",
        {
            "src/db.js": f'export const anon = "{jwt("anon")}";\n',
            "src/maps.js": f'export const key = "{maps_key}";\n',
            "app.py": 'DB = "postgres://app:password@localhost:5432/app"\n'
            'DEV = "postgresql://postgres:postgres@db.example.com/app"\n'
            'TPL = "postgres://app:${DB_PASSWORD}@db.example.com/app"\n',
        },
    )
    assert gitleaks(tmp_path / "src", gitleaks_config([maps_key])) == 0
    assert gitleaks(tmp_path / "src", gitleaks_config([])) == 10


@needs_railpack
@pytest.mark.parametrize(
    ("fixture", "start", "exit_code"),
    [
        ("cs-fastapi-hello", None, 0),
        ("cs-express-hello", None, 0),
        ("cs-static-html", None, 0),
        ("cs-notebook-trivial", None, 13),
        ("cn-shared-warehouse-tables", None, 13),
        ("cn-shared-warehouse-tables", "streamlit run app/main.py --server.port $PORT", 0),
        ("dash-app", "gunicorn app:server --bind 0.0.0.0:$PORT", 0),
    ],
)
def test_plan_runs_railpack_and_fails_without_a_start(
    tmp_path: Path, fixture: str, start: str | None, exit_code: int
) -> None:
    shutil.copytree(
        FIXTURES / fixture, tmp_path / "src", ignore=shutil.ignore_patterns("Dockerfile")
    )
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    env = step_env(build_config(CELL, cell_build(start=start)), "plan")
    result = run_step("plan", tmp_path, env)
    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code == 0:
        assert (tmp_path / ".ssc" / "plan.json").is_file()
        assert (tmp_path / ".ssc" / "secrets" / "VITE_API").read_text() == "https://api.example.com"


@needs_railpack
def test_plan_installs_the_listed_packages_in_the_build_and_the_image(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "cs-fastapi-hello", tmp_path / "src")
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    build = cell_build(start=None, system_packages=("poppler-utils", "fonts-dejavu-core"))
    env = step_env(build_config(CELL, build), "plan")
    assert env["SSC_APT_PACKAGES"] == "fonts-dejavu-core poppler-utils"
    result = run_step("plan", tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    plan = (tmp_path / ".ssc" / "plan.json").read_text()
    assert plan.count("apt-get install -y fonts-dejavu-core poppler-utils") == 2
    # BuildKit fails the build on a secret the plan names and the build step does not mount.
    secrets = tmp_path / ".ssc" / "secrets"
    assert set(json.loads(plan)["secrets"]) <= {p.name for p in secrets.iterdir()}
    packages = (secrets / "RAILPACK_BUILD_APT_PACKAGES").read_text()
    assert packages == "fonts-dejavu-core poppler-utils"


def test_a_build_without_packages_asks_railpack_for_none() -> None:
    assert step_env(build_config(CELL, cell_build()), "plan")["SSC_APT_PACKAGES"] == ""


@pytest.mark.parametrize(
    ("log", "exit_code"),
    [
        ("npm-unresolvable.log", 11),
        ("pip-private-index.log", 12),
        (None, 14),
    ],
)
def test_build_classifies_a_failed_build_log(
    tmp_path: Path, log: str | None, exit_code: int
) -> None:
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    (tmp_path / ".ssc" / "secrets" / "VITE_API").write_text("https://api.example.com")
    (tmp_path / "src").mkdir()
    args = fake_docker(tmp_path / "bin", log=LOGS / log if log else None, exit_code=1)
    env = step_env(build_config(CELL, cell_build()), "build")
    result = run_step("build", tmp_path, env, tmp_path / "bin")
    assert result.returncode == exit_code, result.stdout + result.stderr
    passed = args.read_text().splitlines()
    assert passed[:3] == ["buildx", "build", "--load"]
    assert f"BUILDKIT_SYNTAX={FRONTEND}" in passed
    assert f"id=VITE_API,src={tmp_path}/.ssc/secrets/VITE_API" in passed
    assert passed[-1] == str(tmp_path / "src")


def test_build_runs_in_its_own_pinned_buildkit(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    fake_docker(tmp_path / "bin")
    env = step_env(build_config(CELL, cell_build()), "build")
    assert env["SSC_BUILDKIT"] == BUILDKIT_IMAGE
    result = run_step("build", tmp_path, env, tmp_path / "bin")
    assert result.returncode == 0, result.stdout + result.stderr
    created = (tmp_path / "bin" / "buildx-create-args").read_text().splitlines()
    assert "--driver" in created and "docker-container" in created
    assert f"image={BUILDKIT_IMAGE}" in created


def test_a_builder_that_will_not_start_is_the_platforms_fault(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/bash\nexit 1\n")
    (bin_dir / "docker").chmod(0o755)
    result = run_step(
        "build", tmp_path, step_env(build_config(CELL, cell_build()), "build"), bin_dir
    )
    assert result.returncode == 15
    detail = 'Build step failure: build step 3 "x" failed: step exited with non-zero status: 15'
    failed = status_of({"id": "b1", "status": "FAILURE", "failureInfo": {"detail": detail}})
    assert isinstance(failed, Failed)
    assert failed.code == BUILD_DRIVER_ERROR


def test_build_passes_and_a_log_classifies_both_patterns() -> None:
    npm = (LOGS / "npm-unresolvable.log").read_text()
    pip = (LOGS / "pip-private-index.log").read_text()
    grep = shutil.which("grep") or "grep"
    for text, pattern, hit in (
        (npm, DEPENDENCY_UNRESOLVED, True),
        (npm, PRIVATE_REGISTRY, False),
        (pip, PRIVATE_REGISTRY, True),
    ):
        found = (
            subprocess.run(  # noqa: S603
                [grep, "-Eqi", pattern], input=text, text=True, check=False
            ).returncode
            == 0
        )
        assert found is hit, pattern


def test_harden_runs_the_image_as_10001_with_home_in_tmp(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    args = fake_docker(bin_dir)
    env = step_env(build_config(CELL, cell_build()), "harden")
    result = run_step("harden", tmp_path, env, bin_dir)
    assert result.returncode == 0, result.stderr
    dockerfile = (tmp_path / ".ssc" / "harden" / "Dockerfile").read_text()
    assert dockerfile == "FROM ssc-app:built\nUSER 10001:10001\nENV HOME=/tmp\n"
    assert args.read_text().splitlines()[-3:] == [
        "--tag",
        f"{REPO}:{BUILD_ID}",
        str(tmp_path / ".ssc" / "harden"),
    ]


# ── the build network (SSC-076) ─────────────────────────────────────────────

POOL = f"projects/{PROJECT}/locations/us-central1/workerPools/ssc-builds"
PROXY = "10.20.4.10:3129"
PROXY_SECRET = f"projects/{PROJECT}/secrets/ssc-build-proxy-credential/versions/latest"
MIRROR = "us-central1-docker.pkg.dev/ssc-platform-0/ssc-platform/buildkit@sha256:" + "c" * 64
POOLED = dataclasses.replace(
    CELL, worker_pool=POOL, proxy=PROXY, proxy_secret=PROXY_SECRET, buildkit_image=MIRROR
)
PROXY_TOKEN = "proxy-token-" + "t" * 32  # noqa: S105  (a test credential)


def script_of(config: dict[str, Any], step: str) -> str:
    (found,) = [s for s in config["steps"] if s["id"] == step]
    return found["args"][1].replace("$$", "$")


def run_script(
    script: str, workspace: Path, env: dict[str, str], path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    search = os.pathsep.join(([str(path)] if path else []) + [os.environ["PATH"]])
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script.replace("/workspace", str(workspace))],  # noqa: S607
        env={"PATH": search, "HOME": str(workspace), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_without_a_pool_the_build_is_what_it_was() -> None:
    config = build_config(CELL, cell_build())
    assert "availableSecrets" not in config
    assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert [script_of(config, s) for s in STEP_IDS] == [SCRIPTS[s] for s in STEP_IDS]
    for step in config["steps"]:
        assert "secretEnv" not in step
        assert not any(e.startswith(("SSC_BUILD_", "SSC_HOST_")) for e in step["env"])
    assert step_env(config, "build")["SSC_BUILDKIT"] == BUILDKIT_IMAGE
    assert CELL.buildkit_image == BUILDKIT_IMAGE


def test_a_pooled_build_runs_in_the_pool_and_only_plan_and_build_get_the_token() -> None:
    config = build_config(POOLED, cell_build())
    plain = build_config(CELL, cell_build())
    assert config["options"] == {"logging": "CLOUD_LOGGING_ONLY", "pool": {"name": POOL}}
    assert config["availableSecrets"] == {
        "secretManager": [{"versionName": PROXY_SECRET, "env": "SSC_BUILD_PROXY_TOKEN"}]
    }
    by_id = {s["id"]: s for s in config["steps"]}
    assert {s for s, step in by_id.items() if "secretEnv" in step} == {"plan", "build"}
    assert by_id["plan"]["secretEnv"] == by_id["build"]["secretEnv"] == ["SSC_BUILD_PROXY_TOKEN"]
    for step in ("plan", "build"):
        env = step_env(config, step)
        assert env["SSC_BUILD_PROXY"] == PROXY
        assert env["SSC_BUILD_NO_PROXY"] == BUILD_NO_PROXY_VALUE
    for step in ("fetch", "scan", "harden"):
        assert by_id[step] == next(s for s in plain["steps"] if s["id"] == step)
    build_env = step_env(config, "build")
    assert build_env["SSC_BUILDKIT"] == MIRROR
    assert build_env["SSC_HOST_NOT_ALLOWED"] == HOST_NOT_ALLOWED
    assert build_env["SSC_BUILD_HOSTS"].split() == list(BUILD_HOSTS)
    assert "proxy-token" not in json.dumps(config)
    assert {
        k: v for k, v in config.items() if k not in {"options", "availableSecrets", "steps"}
    } == {k: v for k, v in plain.items() if k not in {"options", "steps"}}


def test_every_dollar_of_a_pooled_build_is_escaped_from_cloud_build_substitution() -> None:
    config = build_config(POOLED, cell_build(start="node server.js --port $PORT"))
    for step in config["steps"]:
        for text in [*step["args"], *step["env"]]:
            assert "$" not in text.replace("$$", "")
    assert script_of(config, "plan") == pooled_scripts()["plan"]
    assert script_of(config, "build") == pooled_scripts()["build"]


@pytest.mark.parametrize(
    "change",
    [
        {"worker_pool": None},
        {"proxy": None},
        {"proxy_secret": None},
        {"worker_pool": "ssc-builds"},
        {"proxy": "http://10.20.4.10:3129"},
        {"proxy_secret": "ssc-build-proxy-credential"},
        {"buildkit_image": "moby/buildkit:v0.33.1"},
    ],
)
def test_a_pool_needs_all_three_settings_well_formed(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="together|not a|pinned"):
        dataclasses.replace(POOLED, **change)


def test_pool_env_is_all_or_none_and_the_buildkit_mirror_is_read() -> None:
    cell = cell_from_env({name: f"value-{field}" for field, name in ENV.items()})
    build = {
        "SSC_BUILD_SA": CELL.service_account,
        "SSC_BUILD_TOOLS_IMAGE": TOOLS,
        "SSC_BUILD_FRONTEND_IMAGE": FRONTEND,
    }
    pool = {
        "SSC_BUILD_POOL": POOL,
        "SSC_BUILD_PROXY": PROXY,
        "SSC_BUILD_PROXY_SECRET": PROXY_SECRET,
    }
    assert set(pool) == set(POOL_ENV.values())
    config = build_config_from_env({**build, **pool, BUILDKIT_ENV: MIRROR}, cell)
    assert config is not None
    assert (config.worker_pool, config.proxy, config.proxy_secret) == (POOL, PROXY, PROXY_SECRET)
    assert config.buildkit_image == MIRROR
    unpooled = build_config_from_env(build, cell)
    assert unpooled is not None
    assert unpooled.worker_pool is None
    assert unpooled.buildkit_image == BUILDKIT_IMAGE
    with pytest.raises(ConfigError, match="SSC_BUILD_PROXY_SECRET"):
        build_config_from_env({**build, **pool, "SSC_BUILD_PROXY_SECRET": ""}, cell)
    with pytest.raises(ConfigError, match="not a proxy"):
        build_config_from_env({**build, **pool, "SSC_BUILD_PROXY": "proxy"}, cell)


@needs_railpack
def test_a_pooled_plan_hands_railpack_the_proxy_as_secrets_only(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "cs-fastapi-hello", tmp_path / "src")
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    config = build_config(POOLED, cell_build(start=None))
    env = step_env(config, "plan") | {"SSC_BUILD_PROXY_TOKEN": PROXY_TOKEN}
    result = run_script(script_of(config, "plan"), tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert PROXY_TOKEN not in result.stdout + result.stderr
    plan_text = (tmp_path / ".ssc" / "plan.json").read_text()
    info_text = (tmp_path / ".ssc" / "info.json").read_text()
    assert PROXY_TOKEN not in plan_text
    assert PROXY_TOKEN not in info_text
    plan = json.loads(plan_text)
    names = {"HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy"}
    assert names <= set(plan["secrets"])
    assert not names & set(plan["deploy"].get("variables", {}))
    for step in plan["steps"]:
        assert not names & set(step.get("variables") or {})
    secrets = tmp_path / ".ssc" / "secrets"
    # Every name is a file; on a case-insensitive disk HTTP_PROXY and http_proxy are one.
    assert all((secrets / name).is_file() for name in plan["secrets"])
    url = f"http://build:{PROXY_TOKEN}@{PROXY}"
    assert (secrets / "HTTPS_PROXY").read_text() == url
    assert (secrets / "http_proxy").read_text() == url
    assert (secrets / "no_proxy").read_text() == BUILD_NO_PROXY_VALUE


def test_a_pooled_build_gives_buildkit_the_proxy(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    fake_docker(tmp_path / "bin")
    config = build_config(POOLED, cell_build())
    env = step_env(config, "build") | {"SSC_BUILD_PROXY_TOKEN": PROXY_TOKEN}
    result = run_script(script_of(config, "build"), tmp_path, env, tmp_path / "bin")
    assert result.returncode == 0, result.stdout + result.stderr
    created = (tmp_path / "bin" / "buildx-create-args").read_text().splitlines()
    url = f"http://build:{PROXY_TOKEN}@{PROXY}"
    assert f"image={MIRROR}" in created
    assert f"env.HTTPS_PROXY={url}" in created
    assert f"env.HTTP_PROXY={url}" in created
    assert f'"env.NO_PROXY={BUILD_NO_PROXY_VALUE}"' in created


REFUSALS = {
    "curl": "curl: (56) CONNECT tunnel failed, response 403",
    "curl-old": "curl: (56) Received HTTP code 403 from proxy after CONNECT",
    "git": "fatal: unable to access 'https://gitlab.com/x/y.git/': CONNECT tunnel failed, "
    "response 403",
    "pip": "WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, "
    "status=None)) after connection broken by 'ProxyError('Cannot connect to proxy.', "
    "OSError('Tunnel connection failed: 403 Forbidden'))': /simple/requests/",
    "uv": "  Caused by: client error (Connect)\n  Caused by: tunnel error: unsuccessful",
    "mise": "mise ERROR error sending request for url (https://example.com/x): unsuccessful tunnel",
    "yarn": 'error An unexpected error occurred: "https://example.com/x.tgz: tunneling socket '
    'could not be established, statusCode=403".',
    "apt": "E: Failed to fetch http://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/"
    "InRelease  403  Forbidden [IP: 10.20.4.10 3129]",
    "buildkit": "ERROR: failed to solve: quay.io/x/y:1: failed to resolve source metadata for "
    'quay.io/x/y:1: failed to do request: Head "https://quay.io/v2/x/y/manifests/1": Forbidden',
    "plain-http": "The cell's build network refused 1.1.1.1: a build reaches only ...",
    "npm": "npm error code E403\nnpm error 403 403 Forbidden - GET https://example.com/left-pad",
}


@pytest.mark.parametrize(("tool", "line"), REFUSALS.items(), ids=REFUSALS.keys())
def test_a_pooled_build_classifies_the_proxy_s_refusal_first(
    tmp_path: Path, tool: str, line: str
) -> None:
    del tool
    (tmp_path / "src").mkdir()
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    log = tmp_path / "refused.log"
    log.write_text(f"#9 [build 2/3] RUN npm ci\n#9 1.2 {line}\n#9 ERROR: exit code: 1\n")
    fake_docker(tmp_path / "bin", log=log, exit_code=1)
    config = build_config(POOLED, cell_build())
    env = step_env(config, "build") | {"SSC_BUILD_PROXY_TOKEN": PROXY_TOKEN}
    result = run_script(script_of(config, "build"), tmp_path, env, tmp_path / "bin")
    assert result.returncode == 16, result.stdout + result.stderr  # noqa: PLR2004


@pytest.mark.parametrize(
    ("log", "exit_code"),
    [
        ("npm-unresolvable.log", 11),
        ("pip-private-index.log", 12),
        (None, 14),
    ],
)
def test_a_pooled_build_still_classifies_the_other_failures(
    tmp_path: Path, log: str | None, exit_code: int
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    fake_docker(tmp_path / "bin", log=LOGS / log if log else None, exit_code=1)
    config = build_config(POOLED, cell_build())
    env = step_env(config, "build") | {"SSC_BUILD_PROXY_TOKEN": PROXY_TOKEN}
    result = run_script(script_of(config, "build"), tmp_path, env, tmp_path / "bin")
    assert result.returncode == exit_code, result.stdout + result.stderr


def test_npm_s_403_from_a_listed_registry_is_not_the_proxy_s(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    log = tmp_path / "npm-403.log"
    log.write_text(
        "npm error code E403\n"
        "npm error 403 403 Forbidden - GET https://registry.npmjs.org/@acme%2fprivate\n"
    )
    fake_docker(tmp_path / "bin", log=log, exit_code=1)
    config = build_config(POOLED, cell_build())
    env = step_env(config, "build") | {"SSC_BUILD_PROXY_TOKEN": PROXY_TOKEN}
    result = run_script(script_of(config, "build"), tmp_path, env, tmp_path / "bin")
    assert result.returncode == 12, result.stdout + result.stderr  # noqa: PLR2004


def test_an_unpooled_build_never_classifies_a_proxy_refusal(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".ssc" / "secrets").mkdir(parents=True)
    log = tmp_path / "refused.log"
    log.write_text(REFUSALS["curl"] + "\n")
    fake_docker(tmp_path / "bin", log=log, exit_code=1)
    env = step_env(build_config(CELL, cell_build()), "build")
    result = run_step("build", tmp_path, env, tmp_path / "bin")
    assert result.returncode == 14, result.stdout + result.stderr  # noqa: PLR2004


@pytest.mark.parametrize(
    "line",
    [
        "WARNING: Retrying after connection broken by 'ProxyError('Cannot connect to proxy.', "
        "NewConnectionError('Failed to establish a new connection: [Errno 111]'))'",
        "curl: (7) Failed to connect to 10.20.4.10 port 3129: Connection refused",
        "npm error 403 403 Forbidden - GET https://registry.npmjs.org/@acme%2fprivate",
        "ERROR: Could not find a version that satisfies the requirement nopkg==9.9",
    ],
)
def test_a_proxy_outage_or_a_registry_s_refusal_is_not_a_refused_host(line: str) -> None:
    grep = shutil.which("grep") or "grep"
    found = subprocess.run(  # noqa: S603
        [grep, "-Eqi", HOST_NOT_ALLOWED], input=line, text=True, check=False
    )
    assert found.returncode == 1

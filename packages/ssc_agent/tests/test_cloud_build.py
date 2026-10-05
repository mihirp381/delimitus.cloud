"""SSC-015: the Cloud Build config, its step scripts run locally, gitleaks, and status mapping."""

import base64
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from ssc_agent.__main__ import BUILD_ENV, ENV, ConfigError, build_config_from_env, cell_from_env
from ssc_agent.cloud_build import (
    APP_USER,
    BUILD_DRIVER_ERROR,
    BUILD_TIMED_OUT,
    BUILDKIT_IMAGE,
    DEPENDENCY_UNRESOLVED,
    PRIVATE_REGISTRY,
    SCRIPTS,
    STEP_IDS,
    CellBuildConfig,
    build_config,
    gitleaks_config,
    status_of,
)
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
    assert sorted(p.name for p in (tmp_path / ".ssc" / "secrets").iterdir()) == ["VITE_API"]


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

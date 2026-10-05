import json
import os
import stat
from pathlib import Path

import pytest
from conftest import FakeRun, ok

from proofrun import common
from proofrun.common import (
    CommandError,
    CookieError,
    CookieJar,
    FencedError,
    Outcome,
    StateFile,
    StateMismatchError,
)


def test_the_fence_refuses_a_value_containing_the_fenced_id(fake_digest: str) -> None:
    assert common.fenced(f"--project={fake_digest.upper()}")
    assert not common.fenced("--project=ssc-c-cellone01")
    with pytest.raises(FencedError):
        common.fence("ok", f"gs://{fake_digest}-x")


def test_the_fence_refuses_settings_but_ignores_unrelated_variables(fake_digest: str) -> None:
    common.fence_environ({"HOME": fake_digest})
    with pytest.raises(FencedError):
        common.fence_environ({"SSC_PROBE_PROJECT": fake_digest})


def test_run_command_is_fenced_before_anything_runs(fake_digest: str) -> None:
    with pytest.raises(FencedError):
        common.run_command(["gcloud", f"--project={fake_digest}"])


def test_the_real_fence_never_trips_on_the_kit_defaults() -> None:
    for value in ("ssc-c-<label>", "ssc-gateway", "us-central1", "delimitusapps"):
        assert not common.fenced(value)


def test_names_follow_the_product() -> None:
    env = "env_" + "a" * 20
    assert common.app_service(env) == "ssc-a-" + "a" * 20
    assert common.database_name(env) == "app_" + "a" * 20
    assert common.run_url("ssc-gateway", "123") == "https://ssc-gateway-123.us-central1.run.app"
    assert common.www_host("pg01.cellone01.delimitusapps.com") == "www.cellone01.delimitusapps.com"
    with pytest.raises(ValueError):
        common.app_service("env_short")
    with pytest.raises(ValueError):
        common.run_url("x", "12a")


def test_parse_time_takes_nanoseconds_and_z() -> None:
    t = common.parse_time("2026-10-03T12:00:01.123456789Z")
    assert t.microsecond == 123456
    assert t.utcoffset() is not None


def test_median() -> None:
    assert common.median([]) is None
    assert common.median([3.0, 1.0, 2.0]) == 2.0
    assert common.median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_gcloud_json_adds_the_format_and_keeps_only_the_last_error_line() -> None:
    run = FakeRun([(["describe"], ok({"a": 1}))])
    assert common.gcloud_json(run, "run", "services", "describe", "x") == {"a": 1}
    assert run.calls[0][-2:] == ["--format=json", "--quiet"]
    failing = FakeRun([(["describe"], common.Done(1, "", "line one\nERROR: denied\n"))])
    with pytest.raises(CommandError, match="ERROR: denied"):
        common.gcloud_json(failing, "run", "services", "describe", "x")


def test_a_failed_ssc_command_reports_its_error_not_a_uv_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    error = {"error": {"title": "No API token.", "detail": "Run `ssc login`."}}
    warning = "warning: `VIRTUAL_ENV=.venv` does not match the project environment path\n"
    run = FakeRun([(["status"], common.Done(3, json.dumps(error), warning))])
    with pytest.raises(CommandError, match=r"No API token\. Run `ssc login`\."):
        common.ssc_json(run, "status", "a")


def test_app_environment_reads_ssc_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    env = {"id": "env_" + "b" * 20, "name": "preview", "url": "https://a.cell.example.com"}
    run = FakeRun([(["status", "a"], ok({"environments": [env]}))])
    assert common.app_environment(run, "a", "preview") == env
    assert run.calls[0] == ["ssc", "status", "a", "--json"]
    with pytest.raises(CommandError, match="no production"):
        common.app_environment(run, "a", "production")


def test_state_file_resumes_only_the_same_run(tmp_path: Path) -> None:
    store = StateFile(tmp_path / "s.json")
    first = store.resume({"a": 1})
    first["samples"] = [1]
    store.save(first)
    assert store.resume({"a": 1})["samples"] == [1]
    with pytest.raises(StateMismatchError):
        store.resume({"a": 2})
    assert not (tmp_path / "s.json.tmp").exists()


def test_cookie_jar_keeps_values_private(tmp_path: Path) -> None:
    jar = CookieJar(tmp_path / "jar.json")
    jar.put("App.Cell.Example.com", "v1.s1." + "x" * 40, "browser")
    assert stat.S_IMODE(os.stat(jar.path).st_mode) == 0o600
    cookie = jar.get("app.cell.example.com")
    assert cookie.source == "browser"
    listing = jar.listing()
    assert listing[0][:2] == ("app.cell.example.com", "browser")
    assert "x" * 40 not in json.dumps(listing)
    headers = common.session_headers(cookie)
    assert headers["Cookie"].startswith("__Host-ssc-session=v1.s1.")
    assert not any(k.lower().startswith("sec-fetch") for k in headers)


def test_cookie_jar_keys_a_url_copied_from_the_browser_by_its_host(tmp_path: Path) -> None:
    jar = CookieJar(tmp_path / "jar.json")
    jar.put("https://App.Cell.Example.com/health?x=1", "v1.s1." + "x" * 40, "browser")
    assert [h for h, *_ in jar.listing()] == ["app.cell.example.com"]
    assert jar.get("app.cell.example.com").host == "app.cell.example.com"
    assert jar.get("https://app.cell.example.com/").host == "app.cell.example.com"


def test_cookie_jar_refusals(tmp_path: Path) -> None:
    jar = CookieJar(tmp_path / "jar.json")
    with pytest.raises(CookieError):
        jar.put("h", "short", "browser")
    with pytest.raises(CookieError):
        jar.put("h", "x" * 20, "stolen")
    with pytest.raises(CookieError, match="no session cookie"):
        jar.get("h")
    jar.put("h", "x" * 20, "sealed")
    os.chmod(jar.path, 0o644)
    with pytest.raises(CookieError, match="readable by others"):
        jar.get("h")


def test_emit_prints_the_final_line_and_keeps_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "r"
    assert common.emit(Outcome("T5", "3 s", True, ["a"]), results) == 0
    assert common.emit(Outcome("T5", "4 s", False), results) == 1
    assert common.emit(Outcome("T5", "n/a", None), results) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["a", "T5 3 s PASS"]
    assert out[-1] == "T5 n/a INCOMPLETE"
    history = json.loads((results / "t5.json").read_text())
    assert [h["verdict"] for h in history] == ["PASS", "FAIL", "INCOMPLETE"]


def test_emit_writes_where_the_environment_says_at_the_time(isolated: Path) -> None:
    real = common.KIT / "results" / "t1.json"
    before = real.read_bytes() if real.exists() else None
    common.emit(Outcome("T1", "x", True))
    assert (isolated / "results" / "t1.json").exists()
    assert (real.read_bytes() if real.exists() else None) == before

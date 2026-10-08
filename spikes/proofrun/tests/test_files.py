import argparse
import json
import subprocess
import tomllib
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, answer, ok

from proofrun import files
from proofrun.common import KIT, REPO, CookieError, CookieJar, Done, Fetched, emit

ENV_A = "env_" + "a" * 20
ENV_B = "env_" + "b" * 20
HOST = "pfiles.cellone01.example.com"
OPERATOR_LOGIN = "operatorlogin123"
SIGNATURE = "deadbeefcafe0123"
APP = KIT / "apps" / "files"


def goog_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%dT%H%M%SZ")


def signed(epoch: float, env: str = ENV_A, label: str = "cellone01", expires: int = 600) -> str:
    query = urllib.parse.urlencode(
        {
            "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
            "X-Goog-Credential": f"ssc-data@ssc-c-{label}.iam.gserviceaccount.com/"
            "20270115/auto/storage/goog4_request",
            "X-Goog-Date": goog_date(epoch),
            "X-Goog-Expires": str(expires),
            "X-Goog-SignedHeaders": "host",
            "response-content-disposition": 'attachment; filename="x.bin"',
            "X-Goog-Signature": SIGNATURE,
        }
    )
    return f"https://storage.googleapis.com/ssc-c-{label}-cell/files/{env}/ga42/x.bin?{query}"


def loop_line(epoch: float, result: str) -> str:
    return f"LOOP at={datetime.fromtimestamp(epoch, UTC):%Y-%m-%dT%H:%M:%SZ} result={result}"


# --- pure functions ---------------------------------------------------------------------------


def test_swap_env_changes_only_the_environment_segment() -> None:
    url = signed(1_800_000_000)
    edited = files.swap_env(url, ENV_A, ENV_B)
    assert f"/files/{ENV_B}/ga42/x.bin?" in edited
    assert ENV_A not in edited
    assert edited.split("?", 1)[1] == url.split("?", 1)[1]
    assert edited.split("/files/", 1)[0] == url.split("/files/", 1)[0]
    with pytest.raises(ValueError, match="no /files/"):
        files.swap_env(url, ENV_B, ENV_A)


def test_credential_ok_names_the_cells_data_account() -> None:
    url = signed(1_800_000_000)
    assert files.credential_ok(url, "cellone01")
    assert not files.credential_ok(url, "celltwo02")
    assert not files.credential_ok(signed(1_800_000_000, label="evil"), "cellone01")
    assert not files.credential_ok("https://storage.googleapis.com/x?a=b", "cellone01")


def test_expiry_epoch_is_the_signing_date_plus_the_lifetime() -> None:
    assert files.expiry_epoch(signed(1_800_000_000)) == 1_800_000_600
    assert files.expiry_epoch(signed(1_800_000_000, expires=60)) == 1_800_000_060
    with pytest.raises(ValueError, match="X-Goog-Date"):
        files.expiry_epoch("https://storage.googleapis.com/x?X-Goog-Date=2027")


def test_strip_signature_and_link_meta_drop_the_signature() -> None:
    url = signed(1_800_000_000)
    stripped = files.strip_signature(url)
    assert SIGNATURE not in stripped
    assert "X-Goog-Signature" not in stripped
    assert "X-Goog-Credential=" in stripped
    assert files.strip_signature("https://h/p?a=b") == "https://h/p?a=b"
    meta = files.link_meta(url)
    assert meta["path"] == f"/ssc-c-cellone01-cell/files/{ENV_A}/ga42/x.bin"
    assert meta["expires"] == "600"
    assert meta["date"] == "20270115T080000Z"
    assert meta["credential"] is not None
    assert SIGNATURE not in json.dumps(meta)


def test_parse_loop_reads_a_stream_line_or_a_log_text() -> None:
    parsed = files.parse_loop("LOOP at=2027-01-15T08:00:03Z result=APP_NOT_ACTIVE")
    assert parsed is not None
    assert parsed.result == "APP_NOT_ACTIVE"
    assert parsed.at == datetime(2027, 1, 15, 8, 0, 3, tzinfo=UTC)
    prefixed = files.parse_loop("2027 stdout LOOP at=2027-01-15T08:00:04Z result=ok")
    assert prefixed is not None
    assert prefixed.result == "ok"
    assert files.parse_loop("FILES op=put name=x result=ok") is None
    assert files.parse_loop("LOOP at=not-a-time result=ok") is None


def test_first_not_active_picks_the_earliest_refusal() -> None:
    def at(sec: int, result: str) -> files.LoopLine:
        return files.LoopLine(datetime(2027, 1, 15, 8, 0, sec, tzinfo=UTC), result)

    lines = [at(1, "ok"), at(5, "APP_NOT_ACTIVE"), at(3, "APP_NOT_ACTIVE")]
    found = files.first_not_active(lines)
    assert found is not None
    assert found.at.second == 3
    assert files.first_not_active([at(1, "ok"), at(2, "FILES_UNAVAILABLE")]) is None
    assert files.first_not_active([]) is None


def test_log_loop_lines_keep_loop_lines_since_the_start() -> None:
    page = {
        "lines": [
            {"text": "FILES op=put name=x result=ok"},
            {"text": loop_line(1_800_000_000 - 100, "APP_NOT_ACTIVE")},
            {"text": loop_line(1_800_000_010, "APP_NOT_ACTIVE")},
        ],
        "cursor": None,
    }
    since = datetime.fromtimestamp(1_800_000_000, UTC)
    assert [x.result for x in files.log_loop_lines(page, since)] == ["APP_NOT_ACTIVE"]
    assert len(files.log_loop_lines(page)) == 2
    assert files.log_loop_lines({"lines": []}, since) == []


def test_header_ignores_case() -> None:
    assert (
        files.header({"content-disposition": "attachment"}, "Content-Disposition") == "attachment"
    )
    assert files.header({}, "Content-Disposition") == ""


# --- the command against a fake cell ----------------------------------------------------------


class Cell:
    """One cell: the control API, the app's public host, Cloud Storage and the CLI."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.payload = b""
        self.active = True
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []
        self.commands: list[list[str]] = []
        self.prefix_status = 403
        self.old_link_status = 200
        self.put_refusals = 0
        self.stream_lines: list[str] = []
        self.stream_error: Exception | None = None
        self.disable = Done(0, json.dumps({"state": "done"}), "")
        self.enable = Done(0, json.dumps({"state": "done"}), "")
        self.logs: list[dict[str, Any]] = [{"lines": []}]
        self.log_reads = 0
        self.stream_urls: list[str] = []

    def run(self, argv: Any, *, cwd: Any = None, env: Any = None) -> Done:
        self.commands.append(list(argv))
        if "-c" in argv:
            return Done(0, f"https://api.example.com\n{OPERATOR_LOGIN}", "")
        if "disable" in argv:
            self.active = False
            return self.disable
        if "enable" in argv:
            if self.enable.returncode == 0:
                self.active = True
            return self.enable
        if "logs" in argv:
            page = self.logs[min(self.log_reads, len(self.logs) - 1)]
            self.log_reads += 1
            return ok(page)
        raise AssertionError(argv)

    def http(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Fetched:
        self.calls.append((method, url, headers))
        if url.startswith("https://api.example.com"):
            return self.control(url)
        if url.startswith("https://storage.googleapis.com"):
            return self.storage(url)
        assert url.startswith(f"https://{HOST}/")
        route = urllib.parse.urlsplit(url).path
        if route == "/files/put":
            return self.put(body or b"")
        if route == "/files/link":
            return answer(200, {"url": signed(self.clock()), "expires_at": "2027-01-15T08:10:00Z"})
        if route == "/files/delete":
            return answer(200, {"ok": True})
        raise AssertionError(url)

    def control(self, url: str) -> Fetched:
        if url.endswith("/v1/apps"):
            return answer(200, {"apps": [{"id": "app_1", "slug": "pfiles"}]})
        assert url.endswith("/v1/apps/app_1")
        return answer(
            200,
            {
                "environments": [
                    {"id": ENV_A, "name": "preview", "url": f"https://{HOST}"},
                    {"id": ENV_B, "name": "prod", "url": None},
                ]
            },
        )

    def put(self, body: bytes) -> Fetched:
        if not self.active or self.put_refusals:
            self.put_refusals = max(0, self.put_refusals - 1)
            return answer(200, {"ok": False, "code": "APP_NOT_ACTIVE", "message": "no"})
        self.payload = body
        return answer(200, {"ok": True, "name": "x", "size": len(body)})

    def storage(self, url: str) -> Fetched:
        if f"/files/{ENV_B}/" in url:
            if self.prefix_status == 403:
                return answer(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
            return Fetched(self.prefix_status, 0.1, None, self.payload, {})
        if self.clock() >= files.expiry_epoch(url):
            return answer(400, b"<Error><Code>ExpiredToken</Code></Error>")
        headers = {"content-disposition": 'attachment; filename="x.bin"'}
        return Fetched(self.old_link_status, 0.1, None, self.payload, headers)

    def stream(self, url: str, headers: Mapping[str, str], timeout: float) -> Iterator[str]:
        self.stream_urls.append(url)
        if self.stream_error is not None:
            raise self.stream_error
        yield from self.stream_lines


@pytest.fixture(autouse=True)
def cookie_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    CookieJar().put(HOST, "v1." + "c" * 30, "browser")


def args(**extra: Any) -> argparse.Namespace:
    base = {"app": "pfiles", "label": "cellone01", "env": "preview"}
    return argparse.Namespace(**{**base, "wait_expiry": False, "disable": False, **extra})


def go(cell: Cell, clock: Clock, **extra: Any) -> Any:
    return files.run(args(**extra), cell.run, cell.http, cell.stream, clock.sleep, clock)


def statuses(out: Any) -> dict[str, str]:
    return {c["number"]: c["status"] for c in out.data["checks"]}


def test_plain_run_passes_checks_1_2_3_and_6() -> None:
    clock = Clock()
    cell = Cell(clock)
    out = go(cell, clock)
    assert out.passed is True
    assert statuses(out) == {
        "1": "PASS",
        "2": "PASS",
        "3": "PASS",
        "4": "not run",
        "5": "not run",
        "6": "PASS",
    }
    assert any("expires_at 2027-01-15T08:10:00Z" in line for line in out.lines)
    assert not clock.slept
    assert [c for c in cell.commands if "disable" in c or "enable" in c] == []
    assert out.final_line().startswith("GA-4.2 1 PASS, 2 PASS, 3 PASS, 4 not run")


def test_the_app_gets_the_cookie_and_storage_does_not() -> None:
    clock = Clock()
    cell = Cell(clock)
    go(cell, clock)
    for _method, url, headers in cell.calls:
        if url.startswith(f"https://{HOST}"):
            assert headers["Cookie"].startswith("__Host-ssc-session=")
        elif url.startswith("https://storage.googleapis.com"):
            assert "Cookie" not in headers
        else:
            assert headers["Authorization"] == f"Bearer {OPERATOR_LOGIN}"
    puts = [c for c in cell.calls if "/files/put" in c[1]]
    assert puts[0][1].split("?", 1)[1].startswith("name=ga42/")


def test_a_missing_cookie_stops_the_run() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.http("GET", f"https://{HOST}/files/link?op=get&name=x", {}, None, 1.0)
    jar = CookieJar()
    jar.path.unlink()
    with pytest.raises(CookieError):
        go(cell, clock)


def test_a_missing_environment_url_stops_the_run() -> None:
    clock = Clock()
    cell = Cell(clock)
    with pytest.raises(files.CommandError, match="has no URL yet"):
        files.run(args(env="prod"), cell.run, cell.http, cell.stream, clock.sleep, clock)


def test_prefix_fetch_answering_200_fails_check_3() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.prefix_status = 200
    out = go(cell, clock)
    assert out.passed is False
    assert statuses(out)["3"] == "FAIL"
    assert statuses(out)["2"] == "PASS"


def test_a_link_signed_by_another_account_fails_check_2() -> None:
    clock = Clock()
    cell = Cell(clock)
    out = files.run(args(label="celltwo02"), cell.run, cell.http, cell.stream, clock.sleep, clock)
    assert out.passed is False
    assert statuses(out)["2"] == "FAIL"


def test_wait_expiry_sleeps_until_30_s_past_and_expects_expired_token() -> None:
    clock = Clock()
    cell = Cell(clock)
    out = go(cell, clock, wait_expiry=True)
    assert out.passed is True
    assert statuses(out)["4"] == "PASS"
    assert clock.slept == [630.0]


def test_wait_expiry_fails_when_the_link_still_works() -> None:
    clock = Clock()
    cell = Cell(clock)
    # the fake sleep does nothing, so the link has not expired when it is fetched again
    out = files.run(
        args(wait_expiry=True), cell.run, cell.http, cell.stream, lambda _s: None, clock
    )
    assert statuses(out)["4"] == "FAIL"
    assert out.passed is False


def test_disable_drill_sees_the_refusal_in_the_stream() -> None:
    clock = Clock()
    cell = Cell(clock)
    now = clock()
    cell.stream_lines = [loop_line(now + 1, "ok"), loop_line(now + 4, "APP_NOT_ACTIVE")]
    out = go(cell, clock, disable=True)
    assert out.passed is True, out.lines
    found = statuses(out)
    assert found["5a"] == found["5b"] == found["5c"] == "PASS"
    kill = [c for c in cell.commands if "disable" in c or "enable" in c]
    assert [c[-3] for c in kill] == ["disable", "enable"]
    assert cell.log_reads == 0
    assert out.data["loop_lines"] == cell.stream_lines
    a = next(line for line in out.lines if line.startswith("5a"))
    assert "4.0 s later (includes clock skew)" in a
    b = next(line for line in out.lines if line.startswith("5b"))
    assert "answered 200" in b
    assert "seconds=90" in cell.stream_urls[0]
    assert cell.stream_urls[0].startswith(f"https://{HOST}/files/loop?")


def test_disable_drill_reads_the_logs_when_the_stream_shows_nothing() -> None:
    clock = Clock()
    cell = Cell(clock)
    now = clock()
    cell.logs = [
        {"lines": [{"text": loop_line(now + 2, "ok")}]},
        {"lines": [{"text": loop_line(now + 6, "APP_NOT_ACTIVE")}]},
    ]
    out = go(cell, clock, disable=True)
    assert out.passed is True, out.lines
    assert cell.log_reads == 2
    assert clock.slept == [1.0] * 20 + [15.0]
    logs = next(c for c in cell.commands if "logs" in c)
    assert logs[2:] == ["pfiles", "--env", "preview", "--source", "app", "--since", "10m", "--json"]
    assert any("APP_NOT_ACTIVE" in line for line in out.data["log_lines"])


def test_disable_drill_is_incomplete_when_no_line_can_be_read() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.stream_error = OSError("HTTP 302")
    out = go(cell, clock, disable=True)
    assert out.passed is None
    assert statuses(out)["5a"] == "INCOMPLETE"
    assert cell.log_reads == 3
    assert clock.slept.count(15.0) == 2
    assert out.final_line().endswith("INCOMPLETE")
    assert [c[-3] for c in cell.commands if "enable" in c] == ["enable"]


def test_disable_drill_fails_when_the_loop_never_saw_a_refusal() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.stream_lines = [loop_line(clock() + 1, "ok")]
    out = go(cell, clock, disable=True)
    assert out.passed is False
    assert statuses(out)["5a"] == "FAIL"


def test_an_old_link_that_stops_working_fails_the_disclosure_check() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.stream_lines = [loop_line(clock() + 1, "APP_NOT_ACTIVE")]
    cell.old_link_status = 403
    out = go(cell, clock, disable=True)
    assert statuses(out)["5b"] == "FAIL"
    assert any("answered 403" in line for line in out.lines)


def test_enable_runs_when_disable_fails_and_reports_a_failed_enable() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.disable = Done(1, "", "boom")
    cell.enable = Done(1, "", "stuck")
    out = go(cell, clock, disable=True)
    assert out.passed is False
    assert "undo: ssc enable pfiles" in out.lines
    assert [c[-3] for c in cell.commands if "enable" in c] == ["enable"]
    assert statuses(out)["5a"] == "FAIL"
    assert statuses(out)["5c"] == "FAIL"
    assert cell.log_reads == 0


def test_a_put_that_is_refused_just_after_enable_is_retried() -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.stream_lines = [loop_line(clock() + 1, "APP_NOT_ACTIVE")]

    original = cell.run

    def run(argv: Any, *, cwd: Any = None, env: Any = None) -> Done:
        done = original(argv, cwd=cwd, env=env)
        if "enable" in argv:
            cell.put_refusals = 2
        return done

    out = files.run(args(disable=True), run, cell.http, cell.stream, clock.sleep, clock)
    assert out.passed is True
    assert any("ok on try 3" in line for line in out.lines)
    assert clock.slept == [1.0] * 20 + [5.0, 5.0]


def test_the_token_and_the_signature_are_never_printed_or_saved(isolated: Path) -> None:
    clock = Clock()
    cell = Cell(clock)
    cell.stream_lines = [loop_line(clock() + 1, "APP_NOT_ACTIVE")]
    out = go(cell, clock, disable=True)
    emit(out)
    everything = "\n".join(out.lines) + json.dumps(out.data)
    results = isolated / "results"
    saved = sorted(results.glob("files-pfiles-*.json"))
    assert len(saved) == 1
    assert (results / "ga-4.2.json").exists()
    everything += saved[0].read_text() + (results / "ga-4.2.json").read_text()
    for secret in (OPERATOR_LOGIN, SIGNATURE, "X-Goog-Signature", "https://storage.googleapis.com"):
        assert secret not in everything
    assert "X-Goog-Credential" not in everything.replace("credential", "")
    assert json.loads(saved[0].read_text())["link"]["expires"] == "600"


def test_the_arguments_parse() -> None:
    parser = argparse.ArgumentParser()
    files.add_arguments(parser)
    parsed = parser.parse_args(["--app", "pfiles", "--label", "cellone01", "--disable"])
    assert (parsed.env, parsed.wait_expiry, parsed.disable) == ("preview", False, True)
    with pytest.raises(SystemExit):
        parser.parse_args(["--app", "pfiles", "--label", "x", "--env", "dev"])


# --- the fixture app ----------------------------------------------------------------------------


def test_the_fixture_manifest_asks_for_files_and_a_health_path() -> None:
    manifest = tomllib.loads((APP / "ssc.toml").read_text())
    assert manifest["schema"] == "ssc/v1"
    assert manifest["files"] == {}
    assert manifest["runtime"]["health_path"] == "/health"
    assert "$PORT" in manifest["runtime"]["start"]


def test_the_fixture_manifest_loads_with_the_products_loader() -> None:
    code = (
        "import sys\n"
        "from ssc_contracts.manifest import load_manifest, uses_files\n"
        "m = load_manifest(open(sys.argv[1], 'rb').read())\n"
        "print(uses_files(m), m.runtime.health_path)\n"
    )
    done = subprocess.run(  # noqa: S603
        ["uv", "run", "python", "-c", code, str(APP / "ssc.toml")],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if "No module named" in done.stderr or "Failed to spawn" in done.stderr:
        pytest.skip("ssc_contracts cannot be imported at the repository root")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True /health"


def test_the_fixture_vendors_the_helper_unchanged() -> None:
    source = REPO / "packages" / "ssc_app" / "src" / "ssc_app"
    for name in ("files.py", "workload.py"):
        assert (APP / "ssc_app" / name).read_bytes() == (source / name).read_bytes()
    assert "import" not in (APP / "ssc_app" / "__init__.py").read_text()
    assert (APP / ".python-version").read_text().strip() == "3.14"

"""GA-6.1 live revoke and drain kit, offline: a fake control API, a fake probe app and its hold
stream, fake ``gcloud`` reads. The clock moves only when the code sleeps; the hold's stream runs
in the kit's own thread and ends once the fake API saw the delete."""

import argparse
import json
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import Clock

from proofrun import drain
from proofrun.__main__ import PROOFS, parser
from proofrun.common import CommandError, CookieJar, Done, Fetched

SLUG, LABEL, HOSTNAME = "pegress", "proofcell01", "www.cloudflare.com"
APP_HOST = f"{SLUG}--preview.{LABEL}.delimitusapps.com"
BASE = f"https://{APP_HOST}"
API = "https://api.example.test"
TOKEN = "operator-token-value"
COOKIE = "c" * 40
ORG = "org_" + "o" * 20
ENV = "env_" + "e" * 20
CREDENTIAL_USER = f"{ENV}.cred_secretish"
T0 = datetime(2026, 10, 8, 20, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def epoch(seconds: float) -> float:
    return at(seconds).timestamp()


def stamp(seconds: float) -> str:
    return at(seconds).isoformat().replace("+00:00", "Z")


class World:
    """The cell as the kit sees it, with what it did in ``log``."""

    def __init__(self) -> None:
        self.listed = [HOSTNAME]
        self.log: list[str] = []
        self.deleted = threading.Event()
        self.delete_status = 200
        self.put_status = 200
        self.put_keeps_off = False
        self.cut: float | None = 9.6
        self.end_reason = "closed"
        self.stream_breaks = False
        self.latest = 3.4
        self.latest_fails = False
        self.listener: float | None = 4.5
        self.refused_status = 403
        self.app_line = True
        self.access_line = True
        self.interrupt_after_delete = False
        self.headers: list[Mapping[str, str]] = []

    # gcloud and ssc
    def run(self, argv: Any, *, cwd: Any = None, env: Any = None) -> Done:
        argv = list(argv)
        if "-c" in argv:
            return Done(0, f"{API}\n{TOKEN}", "")
        if "status" in argv:
            environment = {"name": "preview", "id": ENV, "url": BASE + "/"}
            return Done(0, json.dumps({"environments": [environment]}), "")
        if "whoami" in argv:
            return Done(0, json.dumps({"org_id": ORG, "role": "admin"}), "")
        if "describe" in argv:
            self.log.append("latest")
            assert f"gs://ssc-c-{LABEL}-cell/snapshots/{ORG}/latest.json" in argv
            if self.latest_fails:
                return Done(1, "", "ERROR: denied")
            return Done(0, json.dumps({"update_time": stamp(self.latest)}), "")
        if "logging" in argv:
            assert f"--project=ssc-c-{LABEL}" in argv
            return Done(0, json.dumps(self.entries(argv[3])), "")
        raise AssertionError(argv)

    def entries(self, query: str) -> list[dict[str, Any]]:
        if drain.LISTENER_LINE in query:
            if self.listener is None:
                return []
            return [
                {"timestamp": stamp(-60), "textPayload": "INFO:x:egress listener written"},
                {
                    "timestamp": stamp(self.listener),
                    "textPayload": "INFO:x:egress listener written",
                },
                {"timestamp": stamp(20.0), "textPayload": "INFO:x:egress listener written"},
            ]
        if "gce_instance" in query:
            assert f"{HOSTNAME}:443" in query and ENV in query
            if not self.access_line:
                return []
            check = {"at": stamp(-2), "status": 200, "ms": 180, "flags": "-"}
            held = {"at": stamp(-1), "status": "200", "ms": "10600", "flags": "DC"}
            refused = {"at": stamp(10), "status": 403, "ms": 1, "flags": "-"}
            return [
                {"jsonPayload": {**line, "user": CREDENTIAL_USER, "authority": f"{HOSTNAME}:443"}}
                for line in (check, refused)
            ] + [
                {
                    "textPayload": json.dumps(
                        {**held, "user": CREDENTIAL_USER, "authority": f"{HOSTNAME}:443"}
                    )
                }
            ]
        assert "cloud_run_revision" in query and "ssc-a-" + "e" * 20 in query
        if not self.app_line or self.cut is None:
            return []
        hold = {"run": "r1", "reason": self.end_reason, "alive": 12, "at": epoch(self.cut)}
        return [{"jsonPayload": {"message": "egress hold end", "hold": hold}}]

    # the control API and the app
    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> Fetched:
        self.headers.append(headers)
        parts = urllib.parse.urlsplit(url)
        if url.startswith(API):
            return self.control(method, parts, body)
        assert url.startswith(BASE) and method == "GET"
        assert headers["Cookie"].endswith(COOKIE)
        query = urllib.parse.parse_qs(parts.query)
        assert parts.path == "/egress" and query["host"] == [HOSTNAME]
        self.log.append("egress")
        if self.deleted.is_set() and self.interrupt_after_delete:
            raise KeyboardInterrupt
        status = self.refused_status if self.deleted.is_set() else 200
        body_out = {"host": HOSTNAME, "proxy_status": status, "status": None, "error": None}
        return Fetched(200, 0.1, None, json.dumps(body_out).encode())

    def control(self, method: str, parts: Any, body: bytes | None) -> Fetched:
        assert parts.path.startswith("/v1/")
        if parts.path == "/v1/egress" and method == "GET":
            hosts = [{"host": h} for h in self.listed]
            return Fetched(200, 0.1, None, json.dumps({"hosts": hosts}).encode())
        if parts.path == f"/v1/egress/hosts/{HOSTNAME}" and method == "DELETE":
            self.log.append("DELETE")
            if self.delete_status != 200:
                return Fetched(self.delete_status, 0.1, None, b"{}")
            self.listed.remove(HOSTNAME)
            self.deleted.set()
            return Fetched(200, 0.1, None, b'{"hosts": []}')
        if parts.path == f"/v1/egress/hosts/{HOSTNAME}" and method == "PUT":
            self.log.append("PUT")
            assert json.loads(body or b"") == {}
            if self.put_status == 200 and not self.put_keeps_off:
                self.listed.append(HOSTNAME)
            return Fetched(self.put_status, 0.1, None, b"{}")
        if parts.path == "/v1/audit":
            query = urllib.parse.parse_qs(parts.query)
            assert query["action"] == ["org.updated"]
            assert query["target_kind"] == ["egress_host"]
            assert query["target_id"] == [HOSTNAME]
            return Fetched(200, 0.1, None, json.dumps({"events": self.audit()}).encode())
        raise AssertionError(f"{method} {parts.path}")

    def audit(self) -> list[dict[str, Any]]:
        target = {"kind": "egress_host", "id": HOSTNAME}
        events = []
        if "DELETE" in self.log and self.delete_status == 200:
            events.append(
                {"seq": 41, "action": "org.updated", "target": target, "before": {"host": HOSTNAME}}
            )
        if "PUT" in self.log:
            events.append(
                {"seq": 42, "action": "org.updated", "target": target, "after": {"host": HOSTNAME}}
            )
        return list(reversed(events))

    def stream(self, url: str, headers: Mapping[str, str], timeout: float) -> Iterator[str]:
        parts = urllib.parse.urlsplit(url)
        assert url.startswith(BASE) and parts.path == "/hold"
        query = urllib.parse.parse_qs(parts.query)
        assert query["host"] == [HOSTNAME] and query["seconds"] == ["90"]
        assert query["run"] == ["r1"]
        assert headers["Cookie"].endswith(COOKIE)
        yield json.dumps({"event": "open", "proxy_status": 200, "at": epoch(-1)})
        for n in range(1, 4):
            yield json.dumps({"event": "alive", "n": n, "bytes": 300, "at": epoch(-1 + n / 10)})
        yield ""
        if not self.deleted.wait(5.0):
            return
        if self.stream_breaks:
            raise OSError("connection reset")
        if self.cut is None:
            return
        end = {"event": "end", "reason": self.end_reason, "alive": 12, "at": epoch(self.cut)}
        yield json.dumps(end)


def args(**over: Any) -> argparse.Namespace:
    base = {
        "app": SLUG,
        "label": LABEL,
        "env": "preview",
        "host": HOSTNAME,
        "org": None,
        "project": None,
        "hold_seconds": 90,
    }
    return argparse.Namespace(**{**base, **over})


@pytest.fixture
def world() -> World:
    CookieJar().put(APP_HOST, COOKIE, "browser")
    return World()


def go(world: World, **over: Any) -> Any:
    clock = Clock()
    return drain.run(
        args(**over),
        run=world.run,
        send=world.send,
        stream=world.stream,
        sleep=clock.sleep,
        clock=clock,
        wall=lambda: T0,
        run_id=lambda: "r1",
    )


def text_of(outcome: Any) -> str:
    return "\n".join(outcome.lines) + json.dumps(outcome.data) + outcome.number


def test_drain_is_a_kit_command() -> None:
    assert PROOFS["drain"] is drain
    parsed = parser().parse_args(["drain", "--app", SLUG, "--label", LABEL])
    assert (parsed.env, parsed.host, parsed.hold_seconds) == ("preview", HOSTNAME, 90)
    assert parsed.org is None and parsed.project is None


def test_a_clean_run_passes_and_puts_the_host_back(world: World) -> None:
    outcome = go(world)
    assert outcome.passed is True, outcome.lines
    assert outcome.proof == "GA-6.1"
    data = outcome.data
    assert data["compile_s"] == pytest.approx(3.4)
    assert data["poll_s"] == pytest.approx(1.1)
    assert data["drain_s"] == pytest.approx(5.1)
    assert data["proxy_s"] == pytest.approx(6.2)
    assert data["refused"] == 403
    assert (data["removal_seq"], data["readd_seq"]) == (41, 42)
    assert data["cut_source"] == "stream" and data["cut_reason"] == "closed"
    assert data["restored"] is True
    assert world.listed == [HOSTNAME]
    assert world.log == ["egress", "DELETE", "egress", "latest", "PUT"]
    assert outcome.lines[-1] == f"host back on the allowlist: yes ({HOSTNAME})"
    (row,) = [line for line in outcome.lines if line.startswith("| GA-6.1")]
    assert "**PASS**" in row and "audit seq 41 (removed), 42 (re-added)" in row
    assert "new CONNECT 403" in row and "host back: yes" in row
    assert any("flags DC, 10600 ms" in line for line in outcome.lines)


def test_neither_the_token_nor_the_proxy_user_is_shown(world: World) -> None:
    outcome = go(world)
    text = text_of(outcome)
    assert TOKEN not in text
    assert CREDENTIAL_USER not in text
    assert COOKIE not in text
    api_headers = [h for h in world.headers if "Authorization" in h]
    assert api_headers and all(h["Authorization"] == f"Bearer {TOKEN}" for h in api_headers)
    assert all("Authorization" not in h for h in world.headers if "Cookie" in h)


def test_a_host_not_on_the_list_changes_nothing(world: World) -> None:
    world.listed = []
    outcome = go(world)
    assert outcome.passed is None
    assert outcome.number == "the host is not on the allowlist"
    assert "DELETE" not in world.log and "PUT" not in world.log


def test_a_host_that_does_not_tunnel_first_changes_nothing(world: World) -> None:
    world.refused_status = 403
    world.deleted.set()
    outcome = go(world)
    assert outcome.passed is None
    assert "does not tunnel before the change" in outcome.number
    assert "DELETE" not in world.log


def test_a_refused_delete_puts_nothing_back(world: World) -> None:
    world.delete_status = 403
    outcome = go(world)
    assert outcome.passed is None
    assert outcome.number == "the host was not removed"
    assert "PUT" not in world.log
    assert "DELETE answered 403; nothing removed" in outcome.lines
    assert outcome.lines[-1] == f"host back on the allowlist: not removed ({HOSTNAME})"


def test_an_error_after_the_delete_still_puts_the_host_back(world: World) -> None:
    world.latest_fails = True
    outcome = go(world)
    assert world.log[-1] == "PUT"
    assert outcome.data["restored"] is True
    assert outcome.passed is None
    assert any("note: gcloud storage objects" in line for line in outcome.lines)


def test_ctrl_c_after_the_delete_still_puts_the_host_back(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.interrupt_after_delete = True
    with pytest.raises(KeyboardInterrupt):
        go(world)
    assert world.log[-1] == "PUT"
    assert world.listed == [HOSTNAME]
    assert "host back on the allowlist: yes" in capsys.readouterr().out


def test_a_host_not_put_back_says_so_loudly_and_exits_1(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.put_status = 503
    with pytest.raises(SystemExit) as stopped:
        go(world)
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert f"host back on the allowlist: NO ({HOSTNAME})" in out
    assert "GA-6.1" in out and "PASS" in out


def test_a_put_that_answers_200_but_leaves_the_host_off_is_not_restored(world: World) -> None:
    world.put_keeps_off = True
    with pytest.raises(SystemExit):
        go(world)


def test_a_tunnel_held_to_its_end_fails(world: World) -> None:
    world.end_reason = "max"
    outcome = go(world)
    assert outcome.passed is False
    assert "FAIL: open tunnel cut: ended max (stream)" in outcome.lines


def test_a_slow_cut_fails(world: World) -> None:
    world.cut = 3.4 + 8.5
    outcome = go(world)
    assert outcome.passed is False
    assert "FAIL: cut within 8 s of the snapshot: 8.50 s" in outcome.lines


def test_a_cut_before_the_new_listener_is_not_the_revoke(world: World) -> None:
    world.cut = 4.0
    outcome = go(world)
    assert outcome.passed is False
    assert any(line.startswith("FAIL: cut by the first listener") for line in outcome.lines)


def test_a_new_tunnel_that_is_let_through_fails(world: World) -> None:
    world.refused_status = 200
    outcome = go(world)
    assert outcome.passed is False
    assert "FAIL: new CONNECT refused 403: proxy answered 200" in outcome.lines


def test_missing_proxy_lines_are_incomplete_after_the_wait(world: World) -> None:
    world.listener = None
    world.access_line = False
    outcome = go(world)
    assert outcome.passed is None
    assert "note: no proxy listener line after the delete in Cloud Logging" in outcome.lines
    assert "note: no proxy access line for the held tunnel in Cloud Logging" in outcome.lines
    assert world.log[-1] == "PUT"


def test_a_broken_stream_takes_the_cut_from_the_app_log(world: World) -> None:
    world.stream_breaks = True
    outcome = go(world)
    assert outcome.passed is True, outcome.lines
    assert outcome.data["cut_source"] == "app log"
    assert outcome.data["e2e_s"] is None
    assert any("the hold's stream failed" in line for line in outcome.lines)


def test_another_org_is_refused_before_anything(world: World) -> None:
    with pytest.raises(CommandError, match="not the CLI login's org"):
        go(world, org="org_" + "x" * 20)
    assert world.log == []


def test_log_payload_reads_every_shape() -> None:
    assert drain.log_payload({"jsonPayload": {"a": 1}}) == {"a": 1}
    assert drain.log_payload({"jsonPayload": {"message": '{"a": 2}'}}) == {"a": 2}
    assert drain.log_payload({"jsonPayload": {"message": "egress hold end"}}) == {
        "message": "egress hold end"
    }
    assert drain.log_payload({"textPayload": 'prefix {"a": 3}'}) == {"a": 3}
    assert drain.log_payload({"textPayload": "INFO:x:no json"}) is None
    assert drain.log_payload({}) is None


def test_tunnel_line_picks_the_held_tunnel_and_keeps_no_user() -> None:
    entries = [
        {"jsonPayload": {"authority": f"{HOSTNAME}:443", "status": 200, "ms": 150, "at": stamp(0)}},
        {
            "jsonPayload": {
                "authority": f"{HOSTNAME}:443",
                "status": 403,
                "ms": 99999,
                "at": stamp(1),
            }
        },
        {"jsonPayload": {"authority": "example.com:443", "status": 200, "ms": 9e5, "at": stamp(2)}},
        {
            "jsonPayload": {
                "authority": f"{HOSTNAME}:443",
                "status": "200",
                "ms": "10600",
                "flags": "DC",
                "at": stamp(3),
                "user": CREDENTIAL_USER,
            }
        },
    ]
    line = drain.tunnel_line(entries, HOSTNAME)
    assert line is not None
    assert (line.status, line.ms, line.flags) == (200, 10600, "DC")
    assert line.ended == at(13.6)
    assert CREDENTIAL_USER not in line.describe()
    assert drain.tunnel_line([], HOSTNAME) is None


def test_first_listener_is_the_first_at_or_after_the_change() -> None:
    entries = [
        {"timestamp": stamp(-1), "textPayload": "egress listener written"},
        {"timestamp": stamp(7), "jsonPayload": {"message": "egress listener written"}},
        {"timestamp": stamp(4), "textPayload": "INFO:ssc_egress.runner:egress listener written"},
        {"timestamp": stamp(2), "textPayload": "something else"},
    ]
    assert drain.first_listener(entries, T0) == at(4)
    assert drain.first_listener(entries, at(10)) is None


def test_audit_rows_find_the_removal_and_the_readd() -> None:
    target = {"kind": "egress_host", "id": HOSTNAME}
    events = [
        {"seq": 9, "action": "org.updated", "target": target, "after": {"h": 1}},
        {"seq": 8, "action": "org.updated", "target": target, "before": {"h": 1}},
        {"seq": 7, "action": "org.updated", "target": {"kind": "warm", "id": "x"}, "before": {}},
        {"seq": 6, "action": "app.created", "target": target, "before": {"h": 1}},
    ]
    removal, readd = drain.audit_rows(events, HOSTNAME)
    assert removal is not None and removal["seq"] == 8
    assert readd is not None and readd["seq"] == 9
    assert drain.audit_rows([], HOSTNAME) == (None, None)


def test_a_hold_that_never_comes_up_changes_nothing(world: World) -> None:
    def refused(url: str, headers: Mapping[str, str], timeout: float) -> Iterator[str]:
        yield json.dumps({"event": "end", "reason": "proxy_403", "alive": 0, "at": epoch(0)})

    world.stream = refused  # type: ignore[method-assign]
    outcome = go(world)
    assert outcome.passed is None
    assert outcome.number == "the hold did not come up"
    assert "DELETE" not in world.log


def test_an_audit_log_that_cannot_be_read_is_not_a_fail(world: World) -> None:
    original = world.control

    def refuse_audit(method: str, parts: Any, body: bytes | None) -> Fetched:
        if parts.path == "/v1/audit":
            return Fetched(503, 0.1, None, b"{}")
        return original(method, parts, body)

    world.control = refuse_audit  # type: ignore[method-assign]
    outcome = go(world)
    assert outcome.passed is None
    assert "not read: audit row: org.updated egress_host seq None" in outcome.lines
    assert world.log[-1] == "PUT"

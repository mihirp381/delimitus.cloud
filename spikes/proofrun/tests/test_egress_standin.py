"""T6's stand-in job (``standins/egress/probe.py``): its targets, the address it reads from an
answer, and the stage it reports when a target never answers."""

import importlib.util
import socket
from types import ModuleType

import pytest

from proofrun.common import KIT


def load() -> ModuleType:
    path = KIT / "standins" / "egress" / "probe.py"
    spec = importlib.util.spec_from_file_location("egress_standin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load()


def test_target_parts() -> None:
    assert probe.target("one.one.one.one@1.1.1.1/cdn-cgi/trace") == (
        "one.one.one.one",
        "1.1.1.1",
        "/cdn-cgi/trace",
    )
    assert probe.target("ifconfig.me@192.0.2.7") == ("ifconfig.me", "192.0.2.7", "/")
    for bad in ("1.1.1.1", "host@", "host@not-an-address"):
        with pytest.raises(ValueError):
            probe.target(bad)


def test_seen_address_reads_cloudflare_trace_and_bare_answers() -> None:
    assert probe.seen_address(b"fl=1\nh=1.1.1.1\nip=34.1.2.3\nts=1\n") == "34.1.2.3"
    assert probe.seen_address(b"34.1.2.3\n") == "34.1.2.3"
    assert probe.seen_address(b"<html>nope</html>") is None


def test_a_refused_connection_is_reported_at_the_connect_stage() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    report = probe.ask("localhost@127.0.0.1", timeout=1.0, port=free)
    assert report["stage"] == "connect"
    assert "error" in report
    assert "ip" not in report


def test_nat_defaults_to_cloudflare_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(probe, "ask", lambda t: asked.append(t) or {"stage": "connect"})
    report = probe.nat([])
    assert asked == [probe.TRACE]
    assert report == {"mode": "nat", "targets": {probe.TRACE: {"stage": "connect"}}}

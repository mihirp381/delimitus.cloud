import argparse
import ast
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FakeHttp, FakeRun, answer, ok

from proofrun import __main__ as cli
from proofrun import t11, t12
from proofrun.common import KIT, REPO, CookieJar, Done, FencedError, StateFile


def test_t11_classifies_each_refusal() -> None:
    assert t11.classify({}) == "network"
    assert t11.classify({"status": 403}) == "iam"
    assert t11.classify({"status": 401}) == "iam"
    assert t11.classify({"status": 404}) == "not_found"
    assert t11.classify({"status": 200}) == "allowed"
    assert t11.classify({"status": 500}) == "http 500"


def test_t11_verdict_passes_only_on_two_iam_refusals_and_names_a_breach() -> None:
    refused = {"status": 403, "reason": "PERMISSION_DENIED"}
    assert t11.verdict({"secret": refused, "bucket": refused}).passed is True
    breach = t11.verdict({"secret": {"status": 200}, "bucket": refused})
    assert breach.passed is False
    assert breach.lines[-1].startswith("BREACH")
    assert t11.verdict({"secret": {"status": 404}, "bucket": refused}).passed is False
    assert (
        t11.verdict({"secret": {"error": "timeout"}, "bucket": refused}).data["kinds"]["secret"]
        == "network"
    )


def test_t11_asks_the_app_about_cell_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    env = {"id": "env_" + "a" * 20, "name": "preview", "url": "https://api.cellone01.example.com"}
    run = FakeRun([(["status"], ok({"environments": [env]}))])
    CookieJar().put("api.cellone01.example.com", "v1." + "c" * 30, "browser")
    refused = {"status": 403, "reason": "denied"}
    http = FakeHttp([("/deny?", answer(200, {"secret": refused, "bucket": refused}))])
    args = argparse.Namespace(
        app="api", peer_label="celltwo02", secret=t11.PROBE_SECRET, env="preview"
    )
    assert t11.run(args, run, http).passed is True
    url = http.calls[0][0]
    assert "project=ssc-c-celltwo02" in url
    assert "bucket=ssc-c-celltwo02-cell" in url
    with pytest.raises(SystemExit):
        t11.run(argparse.Namespace(**{**vars(args), "peer_label": "Bad"}), run, http)


def at(hour: int, minute: int = 0, day: int = 3) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC).timestamp()


def test_t12_verdict_same_day_next_day_and_waiting() -> None:
    readings = [
        {"at": at(9), "linked": 5, "lifecycle": "ACTIVE"},
        {"at": at(10), "linked": 5, "lifecycle": "DELETE_REQUESTED"},
        {"at": at(10, 40), "linked": 4, "lifecycle": "DELETE_REQUESTED"},
    ]
    good = t12.verdict(readings, 4)
    assert good.passed is True
    assert good.data["minutes"] == 40
    assert t12.verdict(readings[:1], 4).passed is None
    assert t12.verdict(readings[:2], 4).passed is None
    late = [*readings[:2], {"at": at(1, day=4), "linked": 5, "lifecycle": "DELETE_REQUESTED"}]
    assert t12.verdict(late, 4).passed is False
    next_day = [*readings[:2], {"at": at(2, day=4), "linked": 4, "lifecycle": "DELETE_REQUESTED"}]
    assert t12.verdict(next_day, 4).passed is False


def test_t12_reading_counts_enabled_links_and_survives_an_unreadable_project() -> None:
    linked = [{"billingEnabled": True}, {"billingEnabled": False}, {"billingEnabled": True}]
    run = FakeRun(
        [
            (["billing", "projects", "list"], ok(linked)),
            (["projects", "describe"], ok({"lifecycleState": "DELETE_REQUESTED"})),
        ]
    )
    assert t12.reading(run, "AAAAAA-BBBBBB-CCCCCC", "ssc-c-celltwo02", 1.0) == {
        "at": 1.0,
        "linked": 2,
        "lifecycle": "DELETE_REQUESTED",
    }
    gone = FakeRun(
        [
            (["billing", "projects", "list"], ok(linked)),
            (["projects", "describe"], Done(1, "", "ERROR: 403")),
        ]
    )
    assert t12.reading(gone, "AAAAAA-BBBBBB-CCCCCC", "ssc-c-celltwo02", 1.0)[
        "lifecycle"
    ].startswith("unreadable")
    assert t12.masked("AAAAAA-BBBBBB-CCCCCC") == "…CCCC"


def test_t12_watch_stops_when_the_slot_is_back_and_resumes(tmp_path: Path) -> None:
    clock = Clock(at(9))
    store = StateFile(tmp_path / "t12.json")
    state = store.resume({"project": "p", "expect": 4, "interval_minutes": 10, "hours": 12})
    plan = [
        (5, "ACTIVE"),
        (5, "DELETE_REQUESTED"),
        (5, "DELETE_REQUESTED"),
        (4, "DELETE_REQUESTED"),
    ]

    def read(now: float) -> dict[str, Any]:
        linked, life = plan.pop(0)
        return {"at": now, "linked": linked, "lifecycle": life}

    readings = t12.watch(
        store, state, read=read, once=False, clock=clock, sleep=clock.sleep, say=lambda _: None
    )
    assert len(readings) == 4
    assert clock.slept == [600.0] * 3
    saved = store.load()
    assert saved is not None
    assert len(saved["readings"]) == 4
    once = store.resume({"project": "p", "expect": 4, "interval_minutes": 10, "hours": 12})
    t12.watch(
        store,
        once,
        read=lambda now: {"at": now, "linked": 4, "lifecycle": "DELETE_REQUESTED"},
        once=True,
        clock=clock,
        sleep=clock.sleep,
        say=lambda _: None,
    )
    assert len(once["readings"]) == 5


def test_t12_never_prints_the_billing_account(tmp_path: Path) -> None:
    run = FakeRun(
        [
            (["billing", "projects", "list"], ok([{"billingEnabled": True}])),
            (["projects", "describe"], ok({"lifecycleState": "ACTIVE"})),
        ]
    )
    args = argparse.Namespace(
        billing_account="AAAAAA-BBBBBB-CCCCCC",
        project="ssc-c-celltwo02",
        expect=4,
        interval_minutes=10.0,
        hours=12.0,
        state=tmp_path / "t12.json",
        once=True,
    )
    out = t12.run(args, run)
    assert out.passed is None
    assert "AAAAAA-BBBBBB" not in "\n".join(out.lines) + out.number
    with pytest.raises(SystemExit):
        t12.run(argparse.Namespace(**{**vars(args), "billing_account": None}), run)


def _cell_keys() -> set[str]:
    """Every stack setting ``infra/ssc_infra/cell.py`` reads, from its source."""
    source = (REPO / "infra" / "ssc_infra" / "cell.py").read_text()
    keys = set(re.findall(r'config\.get(?:_bool|_int)?\("([a-z_]+)"\)', source))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in {"BUILD_IMAGES", "GATEWAY_SETTINGS"} and isinstance(
                node.value, ast.Tuple
            ):
                keys |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return keys


@pytest.mark.parametrize("name", ["Pulumi.c-CELL1.example.yaml", "Pulumi.c-CELL2.example.yaml"])
def test_example_stack_configs_set_every_cell_setting(name: str) -> None:
    if not (REPO / "infra" / "ssc_infra" / "cell.py").exists():
        pytest.skip("infra not in this checkout")
    text = (KIT / "configs" / name).read_text()
    keys = set(re.findall(r"^  ssc-infra:([a-z_]+):", text, re.MULTILINE))
    assert keys == _cell_keys()
    flags = dict(
        re.findall(
            r'^  ssc-infra:(database|egress|connections|probe|warm): "(\w+)"', text, re.MULTILINE
        )
    )
    full = name.endswith("CELL1.example.yaml")
    assert (
        flags["database"]
        == flags["egress"]
        == flags["connections"]
        == ("true" if full else "false")
    )
    assert flags["probe"] == "true"
    assert flags["warm"] == "false"


def test_kit_sources_never_name_the_fenced_project() -> None:
    from proofrun.common import fenced

    for path in KIT.rglob("*"):
        if (
            path.is_file()
            and ".venv" not in path.parts
            and "results" not in path.parts
            and path.suffix != ".pyc"
        ):
            assert not fenced(path.read_text(errors="ignore")), path


def test_stage_probe_copies_the_app_and_manifest_only(tmp_path: Path) -> None:
    if not (cli.PROBE_APP / "app.py").exists():
        pytest.skip("conformance probe app not in this checkout")
    copied = cli.stage_probe(tmp_path / "probe")
    assert sorted(copied) == ["app.py", "requirements.txt", "ssc.toml"]
    assert not (tmp_path / "probe" / "checks.py").exists()


def test_every_proof_has_a_subcommand() -> None:
    p = cli.parser()
    args = p.parse_args(["t1", "cost", "--usd", "5", "--days", "7"])
    assert args.command == "t1"
    for name in cli.PROOFS:
        assert name in p.format_help()


def test_main_fences_arguments_and_settings(
    fake_digest: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FencedError):
        cli.main(["t5", "ops", f"--project={fake_digest}"])
    monkeypatch.setenv("SSC_PROBE_PROJECT", fake_digest)
    with pytest.raises(FencedError):
        cli.main(["t1", "cost", "--usd", "5", "--days", "7"])


def test_main_prints_a_number_and_a_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["t1", "cost", "--usd", "5.6", "--days", "7"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "T1 empty cell $0.80 a day PASS"


def test_cookie_set_never_echoes_the_value(capsys: pytest.CaptureFixture[str]) -> None:
    value = "v1." + "s" * 40
    args = argparse.Namespace(action="set", host="App.Cell.Example.com")
    assert cli.cookie(args, prompt=lambda _: value) == 0
    assert cli.cookie(argparse.Namespace(action="list")) == 0
    assert value not in capsys.readouterr().out
    assert CookieJar().get("app.cell.example.com").source == "browser"


@pytest.mark.skipif(not os.environ.get("PROOFRUN_LIVE"), reason="live only: set PROOFRUN_LIVE=1")
def test_live_tools_are_installed() -> None:
    """With ``PROOFRUN_LIVE=1``, the operator's tools answer (no cloud call is made)."""
    import shutil

    for tool in ("gcloud", "pulumi", "uv"):
        assert shutil.which(tool), tool

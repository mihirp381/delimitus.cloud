"""GA-6.5 build secret kit, offline: a fake ``ssc``, a fake login reader and a fake audit API.
The planted value is made by the kit's own generator; no line here holds a token shape."""

import argparse
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeHttp, answer

from proofrun import build_secret as bs
from proofrun.__main__ import PROOFS, parser
from proofrun.common import Done, emit

SLUG = "ga6secret"
BUILD = "bld_" + "b" * 20
BUNDLE = "bun_" + "u" * 20
API = "https://api.example.test"
TOKEN = "operator-token-value"
FRESH_STATUS = {"environments": [{"name": "preview", "current_deployment_id": None}]}
LOG_LINES = [
    'Step #1 - "scan": Finding:     export const upstream = "REDACTED"',
    'Step #1 - "scan": Secret:      REDACTED',
    'Step #1 - "scan": RuleID:      github-pat',
    'Step #1 - "scan": \x1b[33mWRN\x1b[0m \x1b[1mleaks found: 1\x1b[0m',
]


def refused(code: str = bs.CODE, instance: str | None = f"/v1/builds/{BUILD}") -> str:
    body = {"code": code, "title": "The build failed.", "detail": "d", "instance": instance}
    return json.dumps({"error": body})


class FakeSsc:
    """``uv run ssc ...`` and the login reader, answered by subcommand."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.deploy = Done(1, refused(), "")
        self.releases: list[Any] = []
        self.status: dict[str, Any] = FRESH_STATUS
        self.logs: list[list[str]] = [LOG_LINES]
        self.staged: dict[str, str] = {}
        self.staged_at: Path | None = None
        self.calls: list[list[str]] = []
        self.on_deploy: Callable[[], None] = lambda: None

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Done:
        self.calls.append(list(argv))
        if "-c" in argv:
            return Done(0, f"{API}\n{TOKEN}\n", "")
        sub = argv[3]
        if sub == "releases":
            return Done(0, json.dumps({"releases": self.releases}), "")
        if sub == "status":
            return Done(0, json.dumps(self.status), "")
        if sub == "deploy":
            folder = Path(argv[argv.index("--app") + 2])
            self.staged_at = folder
            self.staged = {p.name: p.read_text() for p in folder.iterdir()}
            self.on_deploy()
            return self.deploy
        if sub == "logs":
            lines = self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
            return Done(0, json.dumps({"lines": [{"text": t} for t in lines]}), "")
        raise AssertionError(f"unexpected command: {argv}")


def audit_http(started: Any = None, stored: Any = None, failed: Any = None) -> FakeHttp:
    def events(rows: Any) -> Any:
        return answer(200, {"events": rows, "next_before_seq": None})

    return FakeHttp(
        [
            ("action=build.started", events(started or [{"after": {"bundle_id": BUNDLE}}])),
            ("action=bundle.stored", events(stored if stored is not None else [{"after": {}}])),
            ("action=build.failed", events(failed or [{"after": {"failure_code": bs.CODE}}])),
        ]
    )


def go(ssc: FakeSsc, http: FakeHttp | None = None, sleeps: list[float] | None = None) -> Any:
    slept = sleeps if sleeps is not None else []
    return bs.run(
        argparse.Namespace(app=SLUG),
        run=ssc,
        http=http or audit_http(),
        sleep=slept.append,
        say=lambda _line: None,
        make_value=lambda: ssc.value,
    )


@pytest.fixture
def value() -> str:
    return bs.make_token()


def test_token_has_the_github_pat_shape_and_is_random() -> None:
    a, b = bs.make_token(), bs.make_token()
    pattern = re.escape(bs.TOKEN_PREFIX) + r"[0-9A-Za-z]{36}"
    assert re.fullmatch(pattern, a)
    assert a != b


def test_no_kit_file_holds_a_token_shape() -> None:
    prefix = "gh" + "p_"
    for path in (Path(bs.__file__), Path(__file__)):
        assert prefix not in path.read_text(), path


def test_pass_stages_the_value_only_in_a_temporary_copy(value: str) -> None:
    ssc = FakeSsc(value)
    outcome = go(ssc)
    assert outcome.verdict == "PASS", outcome.lines
    assert set(ssc.staged) == {"index.html", "ssc.toml", "config.js"}
    assert value in ssc.staged["config.js"]
    assert ssc.staged_at is not None and not ssc.staged_at.exists()
    assert not any(value in p.read_text() for p in bs.FIXTURE.iterdir())
    assert outcome.data["build_id"] == BUILD
    assert outcome.data["fingerprint"] == bs.fingerprint(value)
    assert "--json" in next(c for c in ssc.calls if "deploy" in c)


def test_nothing_printed_or_saved_holds_the_value(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome = go(FakeSsc(value))
    assert emit(outcome, tmp_path / "results") == 0
    saved = (tmp_path / "results" / "ga-6.5.json").read_text()
    printed = capsys.readouterr().out
    body = value[len(bs.TOKEN_PREFIX) :]
    for text in (saved, printed, json.dumps(outcome.data), "\n".join(outcome.lines)):
        assert value not in text and body not in text
    assert "GA-6.5" in printed.splitlines()[-1]


def test_cli_refusal_is_the_wrong_layer(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.deploy = Done(4, refused(instance=None), "")
    outcome = go(ssc)
    assert outcome.verdict == "FAIL"
    assert "the CLI's own scan refused it" in outcome.lines[1]
    assert not any("logs" in c for c in ssc.calls)


def test_a_control_plane_refusal_has_no_build_and_fails(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.deploy = Done(1, refused(instance=None), "")
    outcome = go(ssc)
    assert outcome.verdict == "FAIL"
    assert outcome.lines[1].startswith("check 1 FAIL")


def test_a_build_that_succeeds_fails(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.deploy = Done(0, json.dumps({"build_id": BUILD}), "")

    def released() -> None:
        ssc.releases = [{"release_id": "rel_x"}]

    ssc.on_deploy = released
    outcome = go(ssc)
    assert outcome.verdict == "FAIL"
    assert outcome.lines[1].startswith("check 1 FAIL: ")
    assert outcome.lines[4].startswith("check 4 FAIL")


def test_a_timed_out_wait_is_not_read(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.deploy = Done(1, refused("WAIT_TIMED_OUT"), "")
    ssc.logs = [["nothing yet"]]
    outcome = go(ssc)
    assert outcome.verdict == "INCOMPLETE"


def test_an_app_already_deployed_stops_before_deploying(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.releases = [{"release_id": "rel_x"}]
    outcome = go(ssc)
    assert outcome.verdict == "INCOMPLETE"
    assert not any("deploy" in c for c in ssc.calls)
    assert outcome.lines[0].startswith("check 0 FAIL")


def test_a_value_in_any_output_fails_check_2(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.logs = [[*LOG_LINES, f"leaked {value}"]]
    outcome = go(ssc)
    assert outcome.verdict == "FAIL"
    assert outcome.lines[2] == "check 2 FAIL: " + bs.NAMES[2] + " (the value appeared in: ssc logs)"


def test_the_log_is_polled_until_the_finding_shows(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.logs = [['Step #0 - "fetch": ok'], LOG_LINES]
    sleeps: list[float] = []
    outcome = go(ssc, sleeps=sleeps)
    assert outcome.verdict == "PASS"
    assert sleeps == [bs.LOG_GAP_S]


def test_a_log_without_the_rule_fails_after_every_try(value: str) -> None:
    ssc = FakeSsc(value)
    ssc.logs = [['Step #0 - "fetch": ok']]
    sleeps: list[float] = []
    outcome = go(ssc, sleeps=sleeps)
    assert outcome.verdict == "FAIL"
    assert len(sleeps) == bs.LOG_TRIES - 1


def test_audit_needs_the_stored_bundle_and_the_failure_code(value: str) -> None:
    outcome = go(FakeSsc(value), audit_http(stored=[]))
    assert outcome.verdict == "FAIL"
    assert "bundle.stored no" in outcome.lines[3]
    wrong = audit_http(failed=[{"after": {"failure_code": "BUILD_EXITED_NONZERO"}}])
    assert go(FakeSsc(value), wrong).verdict == "FAIL"


def test_audit_reads_send_the_token_and_name_the_build(value: str) -> None:
    http = audit_http()
    go(FakeSsc(value), http)
    urls = [u for u, _ in http.calls]
    assert any(f"target_id={BUILD}" in u and "action=build.failed" in u for u in urls)
    assert any(f"target_id={BUNDLE}" in u for u in urls)
    assert all(h == {"Authorization": f"Bearer {TOKEN}"} for _, h in http.calls)


def test_check_5_is_printed_with_the_build_id(value: str) -> None:
    outcome = go(FakeSsc(value))
    assert any(f"--filter='tags={BUILD}'" in line for line in outcome.lines)


def test_registered_in_the_kit() -> None:
    assert PROOFS["buildsecret"] is bs
    assert parser().parse_args(["buildsecret", "--app", SLUG]).app == SLUG

import argparse
import json
import sys
import tomllib
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock

from proofrun import secret_proof as sp
from proofrun.__main__ import PROOFS, parser
from proofrun.common import (
    KIT,
    CookieJar,
    Done,
    FencedError,
    Fetched,
    emit,
)

SLUG = "secretsapp"
LABEL = "c2label"
HOST = "secretsapp.c2label.apps.example.test"
URL = f"https://{HOST}"
ENV_ID = "env_" + "c" * 20
SECRET = f"ssc-a-{'c' * 20}-GA46_TOKEN"
INTAKE = sp.intake_email(LABEL)
CONTROL = "ssc-control@ssc-control-prod.iam.gserviceaccount.com"
COOKIE = "v1." + "c" * 30
OP = "dep_" + "a" * 20
VALUES = ["first-value-" + "A" * 30, "second-value-" + "B" * 30]


def deny_policy(*, with_intake: bool = True, condition: bool = False) -> dict[str, Any]:
    people = ["ssc-gateway", "ssc-cell-agent", "ssc-build", "ssc-proxy"]
    if with_intake:
        people.append("ssc-secret-intake")
    rule: dict[str, Any] = {
        "deniedPermissions": [sp.DENIED_PERMISSION],
        "deniedPrincipals": [
            sp.principal(f"{p}@ssc-c-{LABEL}.iam.gserviceaccount.com") for p in people
        ],
    }
    if condition:
        rule["denialCondition"] = {"expression": "true"}
    return {"rules": [{"denyRule": rule}]}


def secret_policy(
    *members: str, role: str = "roles/secretmanager.secretAccessor"
) -> dict[str, Any]:
    return {"bindings": [{"role": role, "members": list(members)}]}


OWN = f"serviceAccount:ssc-a-env@ssc-c-{LABEL}.iam.gserviceaccount.com"


class World:
    """The platform as the kit sees it: ``ssc`` commands, the secret store, the app and gcloud."""

    def __init__(self, *, live: bool = False, **over: Any) -> None:
        self.over = over
        self.live_exists = live
        self.stored: str | None = None
        self.live: str | None = None
        self.values: dict[str, str] = {}
        self.pending: int | None = None
        self.calls: list[list[str]] = []
        self.inputs: list[tuple[list[str], bytes]] = []
        self.http_calls: list[tuple[str, Any]] = []
        self.deploys = 0
        self.lists = 0
        if live:
            self.live = self.stored = "1"
            self.values["1"] = "old-live-value"

    def run(self, argv: Sequence[str], *, cwd: Path | None = None, env: Any = None) -> Done:
        words = list(argv)
        self.calls.append(words)
        if words[0] == "gcloud":
            return self.gcloud(words)
        if "status" in words:
            return self.status()
        if "deploy" in words:
            return self.deploy()
        if "--help" in words:
            return Done(0, self.over.get("help", HELP), "")
        assert words[-2:] != [] and "list" in words, argv
        return self.listing("--json" in words)

    def runin(self, argv: Sequence[str], *, input: bytes, cwd: Path | None = None) -> Done:  # noqa: A002
        words = list(argv)
        self.calls.append(words)
        self.inputs.append((words, input))
        assert "set" in words
        fail = self.over.get("set_fail")
        if fail and (self.stored is not None or fail == "first"):
            return Done(4, json.dumps({"error": {"title": "Refused.", "detail": "in flight"}}), "")
        version = str(int(self.stored or "0") + 1)
        self.stored = version
        self.values[version] = input.decode()
        op: str | None = None
        state: str | None = None
        if self.live_exists and not self.over.get("no_redeploy"):
            op = OP
            if "--wait" in words or self.over.get("instant"):
                self.live, state = version, "healthy"
            else:
                state, self.pending = "pending", self.over.get("land_after", 2)
        body = {
            "environment_id": ENV_ID,
            "name": sp.NAME,
            "version": version,
            "changed": True,
            "operation_id": op,
            "state": state,
        }
        return Done(0, json.dumps(body), "")

    def status(self) -> Done:
        env = {"name": "preview", "id": ENV_ID, "url": URL}
        return Done(0, json.dumps({"environments": [env]}), "")

    def deploy(self) -> Done:
        self.deploys += 1
        self.live_exists = True
        state = self.over.get("deploy_state", "healthy")
        if state == "healthy":
            self.live = self.stored
        body = {"operation_id": OP, "state": state, "release_number": 1}
        return Done(0 if state == "healthy" else 5, json.dumps(body), "")

    def listing(self, as_json: bool) -> Done:
        self.lists += 1
        if self.pending is not None:
            self.pending -= 1
            if self.pending <= 0 and not self.over.get("never_lands"):
                self.live, self.pending = self.stored, None
        row = {
            "name": sp.NAME,
            "version": self.stored,
            "live_version": self.live,
            "updated_at": "2026-10-08T10:00:00Z",
        }
        row.update(self.over.get("extra_row", {}))
        if not as_json:
            extra = self.over.get("leak_text", "")
            return Done(0, f"NAME {sp.NAME} VERSION {self.stored} {extra}\n", "")
        body = {"secrets": [row], "environment_id": ENV_ID, "slug": SLUG}
        return Done(0, json.dumps(body), "")

    def gcloud(self, words: list[str]) -> Done:
        if "policies" in words:
            answer = self.over.get("deny", deny_policy())
        else:
            answer = self.over.get("policy", secret_policy(OWN))
        if answer is None:
            return Done(1, "", "ERROR: (gcloud) PERMISSION_DENIED: nope")
        return Done(0, json.dumps(answer), "")

    def http(self, url: str, headers: Any = None, timeout: float = 90.0) -> Fetched:
        self.http_calls.append((url, headers))
        script = self.over.get("http")
        if script is not None:
            return script(self, url)
        return self.app_answer()

    def app_answer(self, version: str | None = None) -> Fetched:
        version = version or self.live
        value = self.values[str(version)]
        fp, length = sp.fingerprint(value)
        body = {"name": sp.NAME, "set": True, "fingerprint": fp, "length": length}
        return Fetched(200, 0.1, None, json.dumps(body).encode())


HELP = (
    "Usage: ssc secret [OPTIONS] COMMAND [ARGS]...\n\n  Set an app's secrets.\n\n"
    "Options:\n  --help  Show this message and exit.\n\nCommands:\n"
    "  set   Read a secret's value from stdin and store it as the...\n"
    "  list  List an environment's secrets: names and versions, never values.\n"
)


@pytest.fixture(autouse=True)
def cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_SSC", "ssc")
    CookieJar().put(HOST, COOKIE, "browser")


def drive(
    world: World, control: str | None = CONTROL, clock: Clock | None = None
) -> tuple[Any, list[str], Clock]:
    clock = clock or Clock()
    said: list[str] = []
    values = iter(VALUES)
    args = argparse.Namespace(app=SLUG, label=LABEL, env="preview", control_sa=control)
    outcome = sp.run(
        args,
        run=world.run,
        runin=world.runin,
        http=world.http,
        sleep=clock.sleep,
        clock=clock,
        say=said.append,
        make_value=lambda: next(values),
    )
    return outcome, said, clock


def results(outcome: Any) -> dict[int, Any]:
    return {c["n"]: c for c in outcome.data["checks"]}


def word(outcome: Any, n: int) -> str:
    return results(outcome)[n]["result"]


def test_a_fresh_app_passes_every_automatic_check() -> None:
    world = World()
    outcome, said, _ = drive(world)
    assert outcome.verdict == "PASS"
    assert [word(outcome, n) for n in range(1, 11)] == ["PASS"] * 10
    assert word(outcome, 11) == word(outcome, 12) == "manual"
    assert world.deploys == 1
    assert outcome.number.startswith("v1/v2, 10 of 10 automatic checks passed")
    assert said[0].startswith(f"app {SLUG}: preview only")
    assert sum("[real]" in line for line in said) >= 3


def test_the_value_goes_on_stdin_only_and_set_runs_in_order() -> None:
    world = World()
    drive(world)
    assert [i for _, i in world.inputs] == [v.encode() for v in VALUES]
    for argv in world.calls:
        assert not any(v in " ".join(argv) for v in VALUES)
    sets = [c for c in world.calls if "set" in c]
    assert "--wait" in sets[0] and "--timeout" in sets[0]
    assert "--wait" not in sets[1]
    assert "900" in sets[0]
    deploy = next(c for c in world.calls if "deploy" in c)
    assert "--wait" in deploy and "1200" in deploy
    order = [c for c in world.calls if "deploy" in c or "set" in c]
    assert order.index(deploy) == 1 and len(order) == 3


def test_an_app_with_a_live_deployment_needs_no_ssc_deploy() -> None:
    world = World(live=True)
    outcome, _, _ = drive(world)
    assert outcome.verdict == "PASS"
    assert world.deploys == 0
    assert (
        "deployment" in results(outcome)[2]["detail"] and "set's" in results(outcome)[2]["detail"]
    )


def test_neither_the_value_nor_its_fingerprint_is_printed_or_saved_but_the_fingerprint(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome, said, _ = drive(World())
    emit(outcome)
    shown = capsys.readouterr().out + "\n".join(said)
    saved = "".join(p.read_text() for p in (isolated / "results").iterdir())
    for value in VALUES:
        assert value not in shown + saved
    for value in VALUES:
        fp = sp.fingerprint(value)[0]
        assert fp in saved
    assert COOKIE not in shown + saved
    assert (isolated / "results" / "ga-4.6.json").exists()
    assert any(p.name.startswith(f"secrets-{SLUG}-") for p in (isolated / "results").iterdir())
    assert sp.NOTE_PIN in outcome.lines and sp.NOTE_PIN in outcome.data["notes"]
    assert sp.LEAVES_BEHIND in outcome.lines and sp.LEAVES_BEHIND in outcome.data["notes"]


def test_the_cookie_goes_to_the_app_host_only() -> None:
    world = World()
    drive(world)
    assert world.http_calls
    for url, headers in world.http_calls:
        assert url == f"{URL}/secret"
        assert headers["Cookie"] == f"__Host-ssc-session={COOKIE}"
    assert not any(COOKIE in " ".join(c) for c in world.calls)


def test_a_failed_first_set_fails_and_nothing_else_runs() -> None:
    world = World(set_fail="first")
    outcome, _, _ = drive(world)
    assert word(outcome, 1) == "FAIL"
    assert outcome.verdict == "FAIL"
    assert world.deploys == 0 and not world.http_calls
    assert [word(outcome, n) for n in (2, 3, 4, 6, 7, 8)] == ["not read"] * 6


def test_a_deploy_that_is_not_healthy_fails_check_2() -> None:
    outcome, _, _ = drive(World(deploy_state="failed"))
    assert word(outcome, 2) == "FAIL"
    assert word(outcome, 3) == "not read"


def test_the_wrong_fingerprint_fails_check_3() -> None:
    def wrong(world: World, url: str) -> Fetched:
        body = {"name": sp.NAME, "set": True, "fingerprint": "0" * 12, "length": 5}
        return Fetched(200, 0.1, None, json.dumps(body).encode())

    outcome, _, _ = drive(World(http=wrong))
    assert word(outcome, 3) == "FAIL"


def test_an_answer_holding_the_value_fails_check_3() -> None:
    def leaky(world: World, url: str) -> Fetched:
        body = {"name": sp.NAME, "set": True, "value": VALUES[0]}
        return Fetched(200, 0.1, None, json.dumps(body).encode())

    outcome, _, _ = drive(World(http=leaky))
    assert word(outcome, 3) == "FAIL"
    assert "contains the value" in results(outcome)[3]["detail"]
    assert VALUES[0] not in json.dumps(outcome.data)


@pytest.mark.parametrize("status", [302, 303])
def test_a_login_redirect_is_a_bad_cookie_and_not_retried(status: int) -> None:
    world = World(http=lambda w, u: Fetched(status, 0.1, None, b""))
    outcome, _, _ = drive(world)
    assert word(outcome, 3) == "FAIL"
    assert f"cookie set {HOST}" in results(outcome)[3]["detail"]
    assert len(world.http_calls) == 3  # checks 3, 7 and 8 read once each: no retry


def test_a_waking_app_is_retried_six_times_ten_seconds_apart() -> None:
    answers = iter([Fetched(503, 0.1, None, b"")] * 5)

    def waking(world: World, url: str) -> Fetched:
        return next(answers, None) or world.app_answer()

    world = World(http=waking)
    outcome, _, clock = drive(world)
    assert word(outcome, 3) == "PASS"
    assert clock.slept[:5] == [10.0] * 5


def test_an_app_that_never_answers_fails_check_3_after_six_tries() -> None:
    world = World(http=lambda w, u: Fetched(None, 0.1, "URLError: refused", b""))
    outcome, _, clock = drive(world)
    assert word(outcome, 3) in {"FAIL", "not read"}
    assert [c for c in clock.slept if c == 10.0][:5] == [10.0] * 5


def test_a_missing_cookie_makes_the_reads_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFRUN_COOKIES", str(KIT / "no-such-jar.json"))
    world = World()
    outcome, _, _ = drive(world)
    assert word(outcome, 3) == "not read"
    assert "cookie set" in results(outcome)[3]["detail"]
    assert outcome.verdict == "INCOMPLETE"
    assert not world.http_calls


def test_a_value_in_the_list_output_fails_check_4_without_printing_it() -> None:
    fp = sp.fingerprint(VALUES[0])[0]
    outcome, _, _ = drive(World(leak_text=f"fp {fp}"))
    assert word(outcome, 4) == "FAIL"
    detail = results(outcome)[4]["detail"]
    assert "fingerprint 1" in detail and fp not in detail
    outcome, _, _ = drive(World(leak_text=f"v {VALUES[0]}"))
    assert word(outcome, 4) == "FAIL"
    assert VALUES[0] not in json.dumps(outcome.data)


def test_an_extra_field_in_the_list_row_fails_check_4() -> None:
    outcome, _, _ = drive(World(extra_row={"value": "x"}))
    assert word(outcome, 4) == "FAIL"


def test_a_secret_command_that_reads_back_fails_check_5() -> None:
    help_text = HELP + "  get   Show a secret's value.\n"
    outcome, _, _ = drive(World(help=help_text))
    assert word(outcome, 5) == "FAIL"
    assert "get" in results(outcome)[5]["detail"]


def test_a_rotation_that_starts_no_deployment_fails_check_6() -> None:
    world = World(live=True, no_redeploy=True)
    outcome, _, _ = drive(world)
    assert word(outcome, 2) == "PASS" and world.deploys == 1
    assert word(outcome, 6) == "FAIL"
    assert [word(outcome, n) for n in (7, 8)] == ["not read", "not read"]


def test_a_refused_second_set_fails_check_6() -> None:
    outcome, _, _ = drive(World(set_fail="second"))
    assert word(outcome, 6) == "FAIL"
    assert word(outcome, 7) == "not read"
    assert "in flight" in results(outcome)[6]["detail"]


def test_a_deployment_that_already_landed_leaves_check_7_not_read() -> None:
    outcome, _, _ = drive(World(live=True, instant=True))
    assert word(outcome, 7) == "not read"
    assert outcome.verdict == "INCOMPLETE"
    assert word(outcome, 8) == "PASS"


def test_a_pin_that_moved_early_fails_check_7() -> None:
    def early(world: World, url: str) -> Fetched:
        return world.app_answer(world.stored)

    outcome, _, _ = drive(World(http=early))
    assert word(outcome, 7) == "FAIL"


def test_the_pin_is_seen_before_it_lands() -> None:
    world = World(land_after=3)
    outcome, _, clock = drive(world)
    assert "live_version 1" in results(outcome)[7]["detail"]
    assert "version 2" in results(outcome)[7]["detail"]
    assert word(outcome, 8) == "PASS"
    assert clock.slept and set(clock.slept) == {10.0}


def test_a_version_that_never_goes_live_fails_check_8_after_ten_minutes() -> None:
    clock = Clock()
    outcome, _, _ = drive(World(never_lands=True), clock=clock)
    assert word(outcome, 8) == "FAIL"
    assert sum(clock.slept) >= sp.POLL_LIMIT_S
    assert "600 s" in results(outcome)[8]["detail"]
    assert outcome.verdict == "FAIL"


def test_the_old_fingerprint_after_the_new_version_is_live_fails_check_8() -> None:
    def stale(world: World, url: str) -> Fetched:
        return world.app_answer("1")

    outcome, _, _ = drive(World(http=stale))
    assert word(outcome, 8) == "FAIL"


def test_a_deny_rule_without_the_intake_fails_check_9() -> None:
    outcome, _, _ = drive(World(deny=deny_policy(with_intake=False)))
    assert word(outcome, 9) == "FAIL"
    outcome, _, _ = drive(World(deny=deny_policy(condition=True)))
    assert word(outcome, 9) == "FAIL"


def test_the_control_plane_missing_from_the_deny_rule_is_detail_only() -> None:
    world = World()
    outcome, _, _ = drive(world)
    detail = results(outcome)[9]["detail"]
    assert word(outcome, 9) == "PASS"
    assert "control plane not named" in detail and "expected" in detail
    outcome, _, _ = drive(World(), control=None)
    assert "control plane not given" in results(outcome)[9]["detail"]
    assert outcome.verdict == "PASS"


def test_unreadable_policies_are_not_read_with_the_command_to_run() -> None:
    outcome, _, _ = drive(World(deny=None, policy=None))
    assert word(outcome, 9) == word(outcome, 10) == "not read"
    assert outcome.verdict == "INCOMPLETE"
    assert "gcloud iam policies get ssc-deny-secret-read" in results(outcome)[9]["detail"]
    assert f"gcloud secrets get-iam-policy {SECRET}" in results(outcome)[10]["detail"]


@pytest.mark.parametrize(
    "policy",
    [
        secret_policy(OWN, f"serviceAccount:{INTAKE}"),
        secret_policy(OWN, f"serviceAccount:{CONTROL}"),
        secret_policy(OWN, "allUsers"),
        secret_policy(OWN, f"serviceAccount:other@ssc-c-{LABEL}.iam.gserviceaccount.com"),
        secret_policy("user:someone@example.test"),
        {"bindings": []},
    ],
)
def test_a_secret_readable_by_others_fails_check_10(policy: dict[str, Any]) -> None:
    outcome, _, _ = drive(World(policy=policy))
    assert word(outcome, 10) == "FAIL"
    assert outcome.verdict == "FAIL"


def test_the_gcloud_calls_are_the_read_only_ones() -> None:
    world = World()
    drive(world)
    gcloud = [c for c in world.calls if c[0] == "gcloud"]
    assert [c[1:3] for c in gcloud] == [["iam", "policies"], ["secrets", "get-iam-policy"]]
    assert all("--impersonate-service-account" not in " ".join(c) for c in gcloud)
    assert f"--project=ssc-c-{LABEL}" in gcloud[1]
    assert SECRET in gcloud[1]
    assert (
        f"--attachment-point=cloudresourcemanager.googleapis.com/projects/ssc-c-{LABEL}"
        in gcloud[0]
    )


def test_the_manual_commands_carry_the_cell_and_never_set_the_verdict() -> None:
    outcome, _, _ = drive(World())
    text = "\n".join(outcome.lines)
    assert f"--impersonate-service-account={INTAKE}" in text
    assert f"--impersonate-service-account={CONTROL}" in text
    assert f"--secret={SECRET}" in text
    assert f"--project=ssc-c-{LABEL}" in text
    assert "do not run 11 or 12 while a deployment is pending" in text
    assert "adds a junk version" in text
    assert "roles/iam.serviceAccountTokenCreator" in text
    assert "check 11 manual" in text and "check 12 manual" in text
    assert outcome.verdict == "PASS"
    placeholder, _, _ = drive(World(), control=None)
    assert "<SSC_CONTROL_SA of the intake service>" in "\n".join(placeholder.lines)


def test_the_verdict_rule() -> None:
    manual = sp.Check(11, "x", None, "d", manual=True)
    passed = sp.Check(1, "x", True, "d")
    assert sp.verdict([passed, manual]) is True
    assert sp.verdict([passed, sp.Check(2, "x", None, "d"), manual]) is None
    assert sp.verdict([sp.Check(2, "x", False, "d"), sp.Check(3, "x", None, "d")]) is False
    assert manual.line().startswith("check 11 manual:")


def test_a_fenced_argument_is_refused(fake_digest: str) -> None:
    args = argparse.Namespace(app=fake_digest, label=LABEL, env="preview", control_sa=None)
    with pytest.raises(FencedError):
        sp.run(args, run=World().run, runin=World().runin, say=lambda s: None)
    with pytest.raises(FencedError):
        sp.run(
            argparse.Namespace(app=SLUG, label=LABEL, env="preview", control_sa=fake_digest),
            run=World().run,
            runin=World().runin,
            say=lambda s: None,
        )
    with pytest.raises(FencedError):
        sp.run_input([sys.executable, "-c", fake_digest], input=b"x")


def test_run_input_sends_the_value_on_stdin() -> None:
    code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    done = sp.run_input([sys.executable, "-I", "-c", code], input=b"abc")
    assert done == Done(0, "ABC", "")


def test_the_command_is_registered_with_a_preview_only_env() -> None:
    assert PROOFS["secrets"] is sp
    args = parser().parse_args(["secrets", "--app", SLUG, "--label", LABEL])
    assert args.env == "preview" and args.control_sa is None
    with pytest.raises(SystemExit):
        parser().parse_args(["secrets", "--app", SLUG, "--label", LABEL, "--env", "prod"])


def load_fixture(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    class Api:
        def get(self, _path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: fn

    stub = types.ModuleType("fastapi")
    stub.FastAPI = Api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", stub)
    module = types.ModuleType("secrets_fixture")
    source = (KIT / "apps" / "secrets" / "main.py").read_text()
    exec(compile(source, "main.py", "exec"), module.__dict__)  # noqa: S102
    return module


def test_the_fixture_answers_a_fingerprint_and_never_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = load_fixture(monkeypatch)
    monkeypatch.setenv("GA46_TOKEN", VALUES[0])
    answer = app.secret()
    assert answer == {
        "name": "GA46_TOKEN",
        "set": True,
        "fingerprint": sp.fingerprint(VALUES[0])[0],
        "length": len(VALUES[0]),
    }
    assert VALUES[0] not in json.dumps(answer)
    monkeypatch.delenv("GA46_TOKEN")
    assert app.secret() == {"name": "GA46_TOKEN", "set": False}
    assert app.health() == {"ok": True}


def test_the_fixture_has_a_manifest_with_no_database_or_schedule() -> None:
    folder = KIT / "apps" / "secrets"
    manifest = tomllib.loads((folder / "ssc.toml").read_text())
    assert manifest["runtime"]["health_path"] == "/health"
    assert set(manifest) == {"schema", "runtime"}
    assert "fastapi" in (folder / "requirements.txt").read_text()

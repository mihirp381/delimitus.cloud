"""The cell deployer (SSC-087): two arguments from fixed sets, exactly one flag set, a run killed
halfway converged by the next, and what a run changes read back from the cloud before it is
applied. The warm option's gateway part (SSC-092): ``warm=true`` and ``warm=false`` set the
``warm`` flag and nothing else. Pulumi is a fake; nothing here reaches a cloud."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pulumi
import pytest

import mockcloud
from mockcloud import Declared, run
from ssc_contracts.cells import CellResource, WarmGateway
from ssc_infra import cell, deployer, naming, stack_config
from ssc_infra.deployer import Deployer, RefusedError, parse

LABEL = "testcell05"
STACK = naming.cell_stack(LABEL)
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
RUN_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/ssc",
    "HOSTNAME": "localhost",
    "CLOUD_RUN_JOB": "ssc-cell-deployer",
    "CLOUD_RUN_EXECUTION": "ssc-cell-deployer-abcde",
    "CLOUD_RUN_TASK_INDEX": "0",
    "CLOUD_RUN_TASK_ATTEMPT": "0",
    "CLOUD_RUN_TASK_COUNT": "1",
}


CELL = {
    "agent_image": f"{naming.platform_registry()}/ssc-agent@sha256:" + "a" * 64,
    "gateway_image": f"{naming.platform_registry()}/ssc-gateway@sha256:" + "f" * 64,
    "gateway_keyring": "CiQAc2VhbGVkLWtleXJpbmc=",
    "gateway_jwks": '{"keys":[{"kty":"EC","crv":"P-256","kid":"id-1","x":"AA","y":"AA"}]}',
    "org_id": "org_" + "a" * 20,
    "datagw_image": f"{naming.platform_registry()}/ssc-datagw@sha256:" + "c" * 64,
    "proxy_image": f"{naming.platform_registry()}/ssc-egress@sha256:" + "9" * 64,
}


PROXIED = {"stage": "staging", "proxy_image": CELL["proxy_image"]}


def _urn(key: str) -> str:
    return f"urn:pulumi:{STACK}::{naming.PROJECT}::{key}"


def _exported(config: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(STACK, config)
    return exported


class FakePulumi:
    """Answers ``stack output`` and ``stack export`` and records every call."""

    def __init__(
        self,
        outputs: dict[str, Any],
        pending: list[str] | None = None,
        resources: list[dict[str, Any]] | None = None,
    ) -> None:
        self.outputs = outputs
        self.pending = pending or []
        self.resources = resources or []
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, cwd: str | None = None) -> str:
        self.calls.append(args)
        if args[:2] == ("stack", "output"):
            return json.dumps(self.outputs)
        if args[:2] == ("stack", "export"):
            ops = [{"type": "creating", "resource": {"urn": u}} for u in self.pending]
            deployment = {"pending_operations": ops, "resources": self.resources}
            return json.dumps({"version": 3, "deployment": deployment})
        return ""

    def set_all(self) -> dict[str, str]:
        (call,) = [c for c in self.calls if c[:2] == ("config", "set-all")]
        assert call[2:4] == ("--stack", STACK)
        flags, pairs = call[4::2], call[5::2]
        assert set(flags) == {"--plaintext"}
        return dict(p.split("=", 1) for p in pairs)


class FakeProxies:
    """The proxy group's machines as each read finds them; the last answer repeats."""

    def __init__(self, *reads: list[str]) -> None:
        self.reads = list(reads) or [[deployer.PROXY_HEALTHY]]
        self.asked: list[tuple[str, bool]] = []
        self.slept: list[float] = []

    def __call__(self, label: str, ha: bool) -> list[str]:  # noqa: FBT001
        self.asked.append((label, ha))
        return self.reads[min(len(self.asked), len(self.reads)) - 1]


def _deployer(
    fake: FakePulumi,
    tmp_path: Path,
    locks: list[datetime] | None = None,
    proxies: FakeProxies | None = None,
) -> Deployer:
    proxies = proxies or FakeProxies()
    return Deployer(
        run=fake,
        locks=lambda _stack: list(locks or []),
        proxies=proxies,
        now=lambda: NOW,
        sleep=proxies.slept.append,
        infra_dir=str(tmp_path),
    )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [LABEL],
        [LABEL, "database", "egress"],
        [LABEL, "gateway_min"],
        [LABEL, "warm"],
        [LABEL, "database=false"],
        [LABEL, "--yes"],
        [LABEL, "Database"],
        ["forbidden-000000", "database"],
        ["Testcell05", "database"],
        ["../testcell05", "egress"],
        ["testcell05 --target x", "egress"],
        ["database", LABEL],
    ],
)
def test_the_runner_refuses_any_argument_outside_the_fixed_set(
    argv: list[str], tmp_path: Path
) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
    assert deployer.main(argv, RUN_ENV, _deployer(fake, tmp_path)) == 2
    assert fake.calls == []


@pytest.mark.parametrize(
    "name",
    ["PYTHONPATH", "PULUMI_CONFIG", "PULUMI_BACKEND_URL", "GOOGLE_APPLICATION_CREDENTIALS", "X"],
)
def test_the_runner_refuses_an_unexpected_environment_variable(name: str, tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
    assert deployer.main([LABEL, "database"], RUN_ENV | {name: "1"}, _deployer(fake, tmp_path)) == 2
    assert fake.calls == []
    with pytest.raises(RefusedError, match=name):
        parse([LABEL, "database"], RUN_ENV | {name: "1"})


def test_the_runner_accepts_the_working_directory_cloud_run_sets() -> None:
    assert parse([LABEL, "database"], RUN_ENV | {"PWD": "/app/infra"}) == (LABEL, "database")


def test_each_lazy_flag_is_accepted() -> None:
    assert [parse([LABEL, f], RUN_ENV) for f in naming.LAZY_FLAGS] == [
        (LABEL, "database"),
        (LABEL, "egress"),
        (LABEL, "connections"),
    ]
    assert set(naming.LAZY_FLAGS) == set(naming.LAZY_RESOURCES)
    assert set(naming.LAZY_FLAGS) == {r.value for r in CellResource}


@pytest.mark.parametrize("flag", naming.LAZY_FLAGS)
def test_the_runner_sets_exactly_its_flag_and_nothing_else(
    flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = {
        "probe": "true",
        "gateway_min": "1",
        "agent_image": "img@sha256:1",
        "org_id": "org_" + "d" * 20,  # the agent serves one org (decision 030)
    }
    if flag == "egress":
        applied = CELL | applied  # a proxy needs its image, and that the gateway settings
    outputs = {"config": _exported(applied, monkeypatch)["config"]}
    fake = FakePulumi(outputs)
    assert deployer.main([LABEL, flag], RUN_ENV, _deployer(fake, tmp_path)) == 0
    written = fake.set_all()
    expected = {stack_config.GLOBAL_WARNING: "true"} | outputs["config"] | {flag: "true"}
    assert written == expected
    changed = {k for k in written if outputs["config"].get(k) != written[k]}
    assert changed == {flag, stack_config.GLOBAL_WARNING}
    assert (tmp_path / f"Pulumi.{STACK}.yaml").read_text() == (
        f"secretsprovider: {naming.SECRETS_PROVIDER}\n"
    )
    assert fake.calls[-1] == ("up", "--yes", "--stack", STACK)


def test_each_warm_setting_is_accepted() -> None:
    assert [parse([LABEL, w], RUN_ENV) for w in naming.WARM_ARGS] == [
        (LABEL, "warm=true"),
        (LABEL, "warm=false"),
    ]
    assert set(naming.WARM_ARGS) == {w.value for w in WarmGateway}


@pytest.mark.parametrize(
    ("arg", "before", "after"), [("warm=true", "false", "true"), ("warm=false", "true", "false")]
)
def test_the_warm_setting_changes_only_the_warm_flag(
    arg: str, before: str, after: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = {"probe": "true", "gateway_min": "1", "warm": before, "database": "true"}
    outputs = {"config": _exported(applied, monkeypatch)["config"]}
    assert outputs["config"]["warm"] == before
    fake = FakePulumi(outputs)
    assert deployer.main([LABEL, arg], RUN_ENV, _deployer(fake, tmp_path)) == 0
    written = fake.set_all()
    assert written == {stack_config.GLOBAL_WARNING: "true"} | outputs["config"] | {"warm": after}
    changed = {k for k in written if outputs["config"].get(k) != written[k]}
    assert changed == {"warm", stack_config.GLOBAL_WARNING}
    assert fake.calls[-1] == ("up", "--yes", "--stack", STACK)


def test_the_applied_config_restores_the_same_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``config`` output, set back as config, builds the stack it came from."""
    applied = {
        "stage": "staging",
        "probe": "true",
        "gateway_min": "2",
        "warm": "true",
        "egress": "true",
        "probe_digest": "sha256:" + "a" * 64,
        "billing_account": "0000AA-BBBBBB-CCCCCC",
        "build_tools_image": f"{naming.platform_registry()}/ssc-build-tools@sha256:" + "d" * 64,
        "build_frontend_image": f"{naming.platform_registry()}/railpack-frontend@sha256:"
        + "e" * 64,
        "gateway_image": f"{naming.platform_registry()}/ssc-gateway@sha256:" + "f" * 64,
        "gateway_keyring": "CiQAc2VhbGVkLWtleXJpbmc=",
        "gateway_jwks": '{"keys":[{"kty":"EC","crv":"P-256","kid":"id-1","x":"AA","y":"AA"}]}',
        "org_id": "org_" + "a" * 20,
        "datagw_image": f"{naming.platform_registry()}/ssc-datagw@sha256:" + "c" * 64,
        "datagw_connections": "con_" + "b" * 20 + ":2,con_" + "a" * 20 + ":7",
        "proxy_image": f"{naming.platform_registry()}/ssc-egress@sha256:" + "9" * 64,
        "proxy_ha": "true",
    }
    first = _exported(applied, monkeypatch)["config"]
    assert first["build_tools_image"] == applied["build_tools_image"]
    assert first["datagw_image"] == applied["datagw_image"]
    assert (first["proxy_image"], first["proxy_ha"]) == (applied["proxy_image"], "true")
    assert first["datagw_connections"] == "con_" + "a" * 20 + ":7,con_" + "b" * 20 + ":2"
    assert {k: first[k] for k in cell.GATEWAY_SETTINGS} == {
        k: applied[k] for k in cell.GATEWAY_SETTINGS
    }
    assert all(isinstance(v, str) for v in first.values())
    assert _exported(first, monkeypatch)["config"] == first
    assert _names(run(STACK, first)) == _names(run(STACK, applied))


def _names(declared: list[Declared]) -> set[str]:
    return {f"{d.type}::{d.name}" for d in declared}


def test_turning_a_flag_on_adds_only_its_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _exported({"probe": "true"}, monkeypatch)["config"]
    names = _names(run(STACK, before))
    after = _names(run(STACK, before | {"database": "true"}))
    assert after - names == naming.LAZY_RESOURCES["database"]
    assert names <= after


def test_a_stack_with_no_applied_config_is_not_applied(tmp_path: Path) -> None:
    fake = FakePulumi({"project_id": naming.cell_project(LABEL)})
    assert deployer.main([LABEL, "database"], RUN_ENV, _deployer(fake, tmp_path)) == 1
    assert [c[0] for c in fake.calls] == ["stack"]


def test_a_lock_left_by_a_killed_run_is_cancelled(tmp_path: Path) -> None:
    fake = FakePulumi({"config": PROXIED})
    stale = [NOW - deployer.STALE_LOCK - timedelta(minutes=1)]
    assert deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, stale)) == 0
    commands = [c[0] for c in fake.calls]
    assert commands.index("cancel") < commands.index("up")
    assert ("cancel", "--yes", "--stack", STACK) in fake.calls


def test_a_live_run_is_left_alone(tmp_path: Path) -> None:
    fake = FakePulumi({"config": PROXIED})
    fresh = [NOW - deployer.STALE_LOCK - timedelta(minutes=1), NOW - timedelta(minutes=5)]
    assert deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, fresh)) == 1
    assert not [c for c in fake.calls if c[0] in ("cancel", "up", "refresh")]


def test_creates_a_killed_run_left_pending_are_imported_or_dropped(tmp_path: Path) -> None:
    sql = _urn("gcp:sql/databaseInstance:DatabaseInstance::sql")
    user = _urn("gcp:sql/user:User::sql-agent")
    fake = FakePulumi({"config": {"stage": "staging"}}, pending=[sql, user])
    assert deployer.main([LABEL, "database"], RUN_ENV, _deployer(fake, tmp_path)) == 0
    refreshes = [c for c in fake.calls if c[0] == "refresh"]
    assert refreshes == [
        (
            "refresh",
            "--yes",
            "--stack",
            STACK,
            "--target",
            sql,
            "--import-pending-creates",
            sql,
            "--import-pending-creates",
            f"projects/{naming.cell_project(LABEL)}/instances/ssc-cell",
        ),
        ("refresh", "--yes", "--stack", STACK, "--clear-pending-creates", "--target", user),
    ]
    assert fake.calls[-1][0] == "up"


def test_a_state_with_nothing_of_the_flag_s_needs_no_refresh(tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
    assert deployer.main([LABEL, "connections"], RUN_ENV, _deployer(fake, tmp_path)) == 0
    assert [c[0] for c in fake.calls] == ["stack", "config", "stack", "up"]


def _state(declared: list[Declared]) -> list[dict[str, Any]]:
    """A cell's resources as ``stack export`` lists them, each under a parent's type."""
    parent = "pulumi:pulumi:Stack"
    return [{"urn": _urn(f"{parent}${d.type}::{d.name}"), "type": d.type} for d in declared]


def _reread(fake: FakePulumi) -> set[str]:
    """The resources the one read-back names, as ``naming`` writes them."""
    (refresh,) = [c for c in fake.calls if c[0] == "refresh"]
    assert refresh[:4] == ("refresh", "--yes", "--stack", STACK)
    assert set(refresh[4::2]) == {"--target"}
    assert [c[0] for c in fake.calls].index("refresh") < [c[0] for c in fake.calls].index("up")
    return {deployer.resource_key(u) for u in refresh[5::2]}


@pytest.mark.parametrize("arg", [*naming.LAZY_FLAGS, *naming.WARM_ARGS])
def test_a_run_reads_back_what_its_flag_adds_or_changes(arg: str, tmp_path: Path) -> None:
    """A second run on a cell that already has the flag's resources: each is read from the cloud
    before ``up``, so a state that wrongly says the change is made cannot make the run a no-op."""
    ((flag, _),) = deployer.setting(arg).items()
    cell_state = _state(run(STACK, CELL | dict.fromkeys(naming.LAZY_FLAGS, "true")))
    fake = FakePulumi({"config": PROXIED}, resources=cell_state)
    assert deployer.main([LABEL, arg], RUN_ENV, _deployer(fake, tmp_path)) == 0
    assert _reread(fake) == (
        naming.LAZY_RESOURCES.get(flag, frozenset()) | naming.DEPLOYER_UPDATES[flag]
    )
    assert fake.calls[-1] == ("up", "--yes", "--stack", STACK)


def test_the_first_run_reads_back_only_what_the_cell_already_has(tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}}, resources=_state(run(STACK, CELL)))
    assert deployer.main([LABEL, "database"], RUN_ENV, _deployer(fake, tmp_path)) == 0
    assert _reread(fake) == {naming.AGENT_SERVICE}


def test_an_update_the_cloud_refused_is_read_back_whatever_it_was_on(tmp_path: Path) -> None:
    """The provider keeps a refused update in the state as if made, with ``initErrors``; ``up``
    would clear the error without calling the cloud (the false ready on proof cell 2)."""
    refused = "gcp:cloudrunv2/service:Service::ssc-gateway"
    cell_state = _state(run(STACK, CELL))
    for resource in cell_state:
        if deployer.resource_key(resource["urn"]) in (naming.AGENT_SERVICE, refused):
            resource["initErrors"] = ["updating: googleapi: Error 403: denied"]
    fake = FakePulumi({"config": PROXIED}, resources=cell_state)
    assert deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path)) == 0
    assert _reread(fake) == {naming.AGENT_SERVICE, refused}


def test_a_read_back_that_fails_stops_the_run_before_it_applies(tmp_path: Path) -> None:
    class Unreadable(FakePulumi):
        def __call__(self, *args: str, cwd: str | None = None) -> str:
            if args[0] == "refresh":
                self.calls.append(args)
                raise deployer.CommandError("pulumi refresh --yes failed: 403")
            return super().__call__(*args, cwd=cwd)

    fake = Unreadable({"config": {"stage": "staging"}}, resources=_state(run(STACK, CELL)))
    assert deployer.main([LABEL, "connections"], RUN_ENV, _deployer(fake, tmp_path)) == 1
    assert not [c for c in fake.calls if c[0] == "up"]


def test_egress_is_done_only_when_the_proxy_answers_its_check(tmp_path: Path) -> None:
    """``up`` returns when the group exists. The run goes on reading the group until its check
    on the proxy port finds the machine healthy, and only then says the flag is on."""
    fake = FakePulumi({"config": PROXIED})
    proxies = FakeProxies([], ["PROVISIONING/CREATING"], ["RUNNING/NONE/UNHEALTHY"], ["HEALTHY"])
    assert (
        deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, proxies=proxies)) == 0
    )
    assert proxies.asked == [(LABEL, False)] * 4
    assert proxies.slept == [deployer.PROXY_POLL_SECONDS] * 3
    assert fake.calls[-1][0] == "up"


def test_a_proxy_that_never_answers_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePulumi({"config": PROXIED})
    proxies = FakeProxies(["RUNNING/NONE/TIMEOUT"])
    assert (
        deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, proxies=proxies)) == 1
    )
    assert len(proxies.asked) == deployer.PROXY_WAIT_SECONDS // deployer.PROXY_POLL_SECONDS + 1
    assert sum(proxies.slept) == deployer.PROXY_WAIT_SECONDS
    printed = capsys.readouterr().err
    assert f"did not answer its check on port {cell.PROXY_PORT}" in printed
    assert "RUNNING/NONE/TIMEOUT" in printed
    assert "egress is true" not in printed


def test_both_machines_of_a_proxy_in_two_zones_must_answer(tmp_path: Path) -> None:
    fake = FakePulumi({"config": PROXIED | {"proxy_ha": "true"}})
    proxies = FakeProxies(["HEALTHY"], ["HEALTHY", "RUNNING/VERIFYING"], ["HEALTHY", "HEALTHY"])
    assert (
        deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, proxies=proxies)) == 0
    )
    assert proxies.asked == [(LABEL, True)] * 3


@pytest.mark.parametrize("arg", ["database", "connections", "warm=true", "warm=false"])
def test_only_egress_waits_for_the_proxy(arg: str, tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
    proxies = FakeProxies(["RUNNING/NONE/UNHEALTHY"])
    assert deployer.main([LABEL, arg], RUN_ENV, _deployer(fake, tmp_path, proxies=proxies)) == 0
    assert proxies.asked == []


def test_egress_is_refused_on_a_cell_that_names_no_proxy_image(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its machine would run nothing and never pass its check; nothing is written or applied."""
    fake = FakePulumi({"config": {"stage": "staging", "database": "true"}})
    proxies = FakeProxies()
    assert (
        deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, proxies=proxies)) == 1
    )
    assert [c[:2] for c in fake.calls] == [("stack", "output")]
    assert proxies.asked == []
    assert "has no proxy_image" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("machine", "state"),
    [
        (
            {
                "instanceStatus": "RUNNING",
                "currentAction": "NONE",
                "instanceHealth": [{"detailedHealthState": "HEALTHY"}],
            },
            "HEALTHY",
        ),
        (
            {
                "instanceStatus": "RUNNING",
                "currentAction": "NONE",
                "instanceHealth": [{"detailedHealthState": "UNHEALTHY"}],
            },
            "RUNNING/NONE/UNHEALTHY",
        ),
        ({"instanceStatus": "RUNNING", "currentAction": "NONE"}, "RUNNING/NONE"),
        (
            {
                "instanceStatus": "RUNNING",
                "currentAction": "RECREATING",
                "instanceHealth": [{"detailedHealthState": "HEALTHY"}],
            },
            "RUNNING/RECREATING/HEALTHY",
        ),
        ({"currentAction": "CREATING"}, "CREATING"),
    ],
)
def test_a_machine_is_healthy_only_when_it_runs_untouched_and_passes_its_check(
    machine: dict[str, Any], state: str
) -> None:
    assert deployer.machine_state(machine) == state


def test_the_proxy_group_read_is_the_one_the_cell_declares() -> None:
    declared = mockcloud.run(STACK, CELL | {"egress": "true"})
    group = mockcloud.one(declared, "gcp:compute/instanceGroupManager:InstanceGroupManager")
    assert (group.inputs["name"], group.inputs["zone"]) == (deployer.PROXY_GROUP, cell.PROXY_ZONE)
    assert group.inputs["waitForInstances"] is False
    assert group.inputs["autoHealingPolicies"]["initialDelaySec"] < deployer.PROXY_WAIT_SECONDS
    both = mockcloud.run(STACK, CELL | {"egress": "true", "proxy_ha": "true"})
    regional = "gcp:compute/regionInstanceGroupManager:RegionInstanceGroupManager"
    spread = mockcloud.one(both, regional).inputs
    assert (spread["name"], spread["region"]) == (deployer.PROXY_GROUP, naming.REGION)
    assert spread["targetSize"] == cell.PROXY_HA_SIZE


@pytest.mark.parametrize("flag", sorted(naming.DEPLOYER_UPDATES))
def test_a_flag_changes_in_place_exactly_what_the_deployer_reads_back(flag: str) -> None:
    def inputs(config: dict[str, str]) -> dict[str, dict[str, Any]]:
        return {f"{d.type}::{d.name}": d.inputs for d in run(STACK, config)}

    off, on = inputs(CELL | {flag: "false"}), inputs(CELL | {flag: "true"})
    assert {k for k in off.keys() & on.keys() if off[k] != on[k]} == naming.DEPLOYER_UPDATES[flag]
    assert set(on) - set(off) == naming.LAZY_RESOURCES.get(flag, frozenset())
    assert set(naming.DEPLOYER_UPDATES) == {*naming.LAZY_FLAGS, *deployer.setting("warm=true")}


def test_every_import_id_names_the_resource_the_cell_declares() -> None:
    declared: list[Declared] = mockcloud.run(
        STACK, {"database": "true", "egress": "true", "connections": "true"}
    )
    by_key = {f"{d.type}::{d.name}": d for d in declared}
    lazy = set().union(*naming.LAZY_RESOURCES.values())
    assert set(deployer.IMPORT_IDS) <= lazy
    for key in deployer.IMPORT_IDS:
        found = deployer.import_id(_urn(key), LABEL)
        assert found is not None
        assert found.startswith(f"projects/{naming.cell_project(LABEL)}/")
        assert found.rsplit("/", 1)[1] == by_key[key].inputs["name"]


def test_pulumi_runs_with_the_deployers_own_environment() -> None:
    env = deployer.PULUMI_ENV
    assert env["PULUMI_BACKEND_URL"] == f"gs://{naming.STATE_BUCKET}"
    assert not [k for k in env if k.startswith(("PYTHON", "GOOGLE_", "PULUMI_CONFIG"))]


def test_pulumis_own_uv_sync_stays_offline_and_skips_dev_packages() -> None:
    env = deployer.PULUMI_ENV
    assert env["UV_OFFLINE"] == env["UV_FROZEN"] == "1"
    assert env["UV_NO_DEV"] == "true"


def test_the_operator_restores_the_applied_config(tmp_path: Path) -> None:
    stale = tmp_path / f"Pulumi.{STACK}.yaml"
    stale.write_text("secretsprovider: x\nconfig:\n  ssc-infra:database: 'false'\n")
    fake = FakePulumi({"config": {"stage": "staging", "database": "true"}})
    assert stack_config.restore(LABEL, run=fake, infra_dir=str(tmp_path)) == {
        "stage": "staging",
        "database": "true",
    }
    assert stale.read_text() == f"secretsprovider: {naming.SECRETS_PROVIDER}\n"
    assert fake.set_all() == {
        stack_config.GLOBAL_WARNING: "true",
        "stage": "staging",
        "database": "true",
    }

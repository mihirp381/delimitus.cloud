"""The cell deployer (SSC-087): two arguments from fixed sets, exactly one flag set, and a run
killed halfway converged by the next. Pulumi is a fake; nothing here reaches a cloud."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pulumi
import pytest

import mockcloud
from mockcloud import Declared, run
from ssc_contracts.cells import CellResource
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


def _urn(key: str) -> str:
    return f"urn:pulumi:{STACK}::{naming.PROJECT}::{key}"


def _exported(config: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(STACK, config)
    return exported


class FakePulumi:
    """Answers ``stack output`` and ``stack export`` and records every call."""

    def __init__(self, outputs: dict[str, Any], pending: list[str] | None = None) -> None:
        self.outputs = outputs
        self.pending = pending or []
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, cwd: str | None = None) -> str:
        self.calls.append(args)
        if args[:2] == ("stack", "output"):
            return json.dumps(self.outputs)
        if args[:2] == ("stack", "export"):
            ops = [{"type": "creating", "resource": {"urn": u}} for u in self.pending]
            return json.dumps({"version": 3, "deployment": {"pending_operations": ops}})
        return ""

    def set_all(self) -> dict[str, str]:
        (call,) = [c for c in self.calls if c[:2] == ("config", "set-all")]
        assert call[2:4] == ("--stack", STACK)
        flags, pairs = call[4::2], call[5::2]
        assert set(flags) == {"--plaintext"}
        return dict(p.split("=", 1) for p in pairs)


def _deployer(fake: FakePulumi, tmp_path: Path, locks: list[datetime] | None = None) -> Deployer:
    return Deployer(
        run=fake, locks=lambda _stack: list(locks or []), now=lambda: NOW, infra_dir=str(tmp_path)
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
        ["ristretto-506621", "database"],
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
    applied = {"probe": "true", "gateway_min": "1", "agent_image": "img@sha256:1"}
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
    }
    first = _exported(applied, monkeypatch)["config"]
    assert first["build_tools_image"] == applied["build_tools_image"]
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
    fake = FakePulumi({"config": {"stage": "staging"}})
    stale = [NOW - deployer.STALE_LOCK - timedelta(minutes=1)]
    assert deployer.main([LABEL, "egress"], RUN_ENV, _deployer(fake, tmp_path, stale)) == 0
    commands = [c[0] for c in fake.calls]
    assert commands.index("cancel") < commands.index("up")
    assert ("cancel", "--yes", "--stack", STACK) in fake.calls


def test_a_live_run_is_left_alone(tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
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


def test_a_clean_state_needs_no_refresh(tmp_path: Path) -> None:
    fake = FakePulumi({"config": {"stage": "staging"}})
    assert deployer.main([LABEL, "connections"], RUN_ENV, _deployer(fake, tmp_path)) == 0
    assert [c[0] for c in fake.calls] == ["stack", "config", "stack", "up"]


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

"""Ticket 19D: ``python -m ssc_control.onboarding`` through ``main(argv)``, and the worker's
``onboarding:tick``, against postgres:18 and a fake WorkOS."""

import asyncio
from collections.abc import Callable

import pytest
from fake_workos import FakeWorkOS
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns
from test_onboarding import DIRECTORY_USER, EMAIL, IDP, JWKS, OP, OP_ACTOR, close_open_rows, connect

from ssc_control.cells import service as cells
from ssc_control.db import make_engine
from ssc_control.identity.workos import WorkOSClient
from ssc_control.onboarding import __main__ as cli
from ssc_control.onboarding import jobs, service
from ssc_control.worker_ports import Ports

type Out = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True)
def database(dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    monkeypatch.setenv("SSC_WORKOS_API_KEY", "sk_test_fake")
    monkeypatch.setenv("SSC_WORKOS_CLIENT_ID", "client_fake")
    close_open_rows(dsns)


@pytest.fixture
def wo() -> FakeWorkOS:
    fake = FakeWorkOS()
    fake.user(DIRECTORY_USER, IDP, EMAIL, first="Ada", last="Admin")
    return fake


def factory(wo: FakeWorkOS) -> Callable[[dict[str, str]], WorkOSClient]:
    return lambda _env: wo.client()


def ok(argv: list[str], wo: FakeWorkOS, capsys: Out) -> list[str]:
    assert cli.main(argv, workos=factory(wo)) == 0, capsys.readouterr().err
    return capsys.readouterr().out.splitlines()


def refused(argv: list[str], wo: FakeWorkOS, capsys: Out) -> str:
    assert cli.main(argv, workos=factory(wo)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err.strip()


def start_argv(*extra: str) -> list[str]:
    return [
        "start",
        "--customer-name",
        "Acme",
        "--domain",
        "acme.example",
        "--admin-email",
        EMAIL,
        "--stage",
        "prod",
        "--operator",
        OP,
        *extra,
    ]


def started(wo: FakeWorkOS, capsys: Out) -> tuple[str, str, str, list[str]]:
    out = ok(start_argv(), wo, capsys)
    onboarding_id, label, _stage, org = out[0].split()[:4]
    return onboarding_id, label, org.removeprefix("org="), out


def ready(dsns: Dsns, label: str) -> None:
    async def go() -> None:
        engine = make_engine(dsns.app)
        try:
            await cells.ready(engine, label, jwks=JWKS, project_number=1234, actor=OP_ACTOR)
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_start_prints_the_row_the_links_and_the_build_command(wo: FakeWorkOS, capsys: Out) -> None:
    onboarding_id, label, org, out = started(wo, capsys)
    assert onboarding_id.startswith("onb_")
    assert org.startswith("org_")
    (made,) = wo.created
    assert out[1:] == [
        f"sso link: https://setup.workos.test/sso/{made}",
        f"directory link: https://setup.workos.test/dsync/{made}",
        "the links expire five minutes after they are made; new ones: "
        f"uv run python -m ssc_control.onboarding links --id {onboarding_id}",
        f"build the cell: uv run python -m ssc_infra.onboard {label} --stage prod "
        f"--settings settings.json --set org_id={org} --probe-image <probe image>",
    ]
    assert all("#" not in line for line in out)


def test_status_and_list_show_both_tracks_the_reason_and_the_links_while_waiting(
    dsns: Dsns, wo: FakeWorkOS, capsys: Out
) -> None:
    onboarding_id, label, _org, _ = started(wo, capsys)
    (made,) = wo.created
    connect(wo, kind="GoogleSAML", directory="gsuite directory")
    assert ok(["tick"], wo, capsys) == [
        f"checked 1; changed {onboarding_id}; live none; failed none"
    ]
    out = ok(["status", "--id", label], wo, capsys)
    assert "open cell=awaiting_cell identity=awaiting_idp" in out[0]
    assert "reason=JOIN_RULE_CONFIRM" in out[0]
    assert out[1].startswith("waiting: JOIN_RULE_CONFIRM: ")
    assert out[2:] == [
        f"sso link: https://setup.workos.test/sso/{made}",
        f"directory link: https://setup.workos.test/dsync/{made}",
    ]
    listed = ok(["list"], wo, capsys)
    assert [line for line in listed if line.startswith(onboarding_id)]

    confirmed = ok(
        ["confirm", "--id", onboarding_id, "--join-rule", "email", "--operator", OP], wo, capsys
    )
    assert "reason=" not in confirmed[0]
    assert "not waiting for a join rule" in refused(
        ["confirm", "--id", onboarding_id, "--join-rule", "email", "--operator", OP], wo, capsys
    )
    ok(["tick"], wo, capsys)
    out = ok(["status", "--id", onboarding_id], wo, capsys)
    assert out == [out[0]]
    assert "identity=org_created" in out[0]
    ready(dsns, label)
    assert ok(["tick"], wo, capsys) == [
        f"checked 1; changed {onboarding_id}; live {onboarding_id}; failed none"
    ]


def test_links_makes_new_ones_while_the_identity_waits(wo: FakeWorkOS, capsys: Out) -> None:
    onboarding_id, _label, _org, _ = started(wo, capsys)
    calls = wo.calls.count("/portal/generate_link")
    out = ok(["links", "--id", onboarding_id], wo, capsys)
    assert len(out) == 3
    assert out[1].startswith("sso link: ")
    assert wo.calls.count("/portal/generate_link") == calls + 2


def test_abandon_prints_the_destroy_command_and_the_workos_reminder(
    dsns: Dsns, wo: FakeWorkOS, capsys: Out
) -> None:
    onboarding_id, label, _org, _ = started(wo, capsys)
    (made,) = wo.created
    argv = ["abandon", "--id", onboarding_id, "--reason", "customer withdrew", "--operator", OP]
    out = ok(argv, wo, capsys)
    assert " abandoned " in out[0]
    assert out[1:] == [
        f"uv run python -m ssc_infra.onboard {label} --stage prod --destroy",
        f"WorkOS organisation {made} is kept: delete it in WorkOS if the customer is not "
        "coming back",
    ]
    assert "already abandoned" in refused(argv, wo, capsys)


def test_refusals_exit_one_and_print_nothing(wo: FakeWorkOS, capsys: Out) -> None:
    assert "no onboarding" in refused(["status", "--id", "nosuchlabel1"], wo, capsys)
    assert "domain" in refused(
        [*start_argv()[:3], "--domain", "not a domain", *start_argv()[5:]], wo, capsys
    )
    assert wo.created == {}


def test_without_the_dsn_or_workos_settings_it_exits_two(
    monkeypatch: pytest.MonkeyPatch, wo: FakeWorkOS, capsys: Out
) -> None:
    monkeypatch.delenv("SSC_WORKOS_API_KEY")
    assert cli.main(["tick"]) == 2
    assert "SSC_WORKOS_API_KEY" in capsys.readouterr().err
    monkeypatch.delenv("SSC_DATABASE_DSN")
    assert cli.main(["list"]) == 2
    assert "SSC_DATABASE_DSN" in capsys.readouterr().err


def test_a_bad_operator_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["confirm", "--id", "x", "--join-rule", "email", "--operator", "dana"])
    assert raised.value.code == 2


async def test_the_worker_tick_moves_the_rows_and_skips_without_workos(
    dsns: Dsns, wo: FakeWorkOS
) -> None:
    engine: AsyncEngine = make_engine(dsns.app)
    client = wo.client()
    try:
        ob = await service.start(
            engine,
            client,
            customer_name="Acme",
            domain="acme.example",
            admin_email=EMAIL,
            stage="prod",
            operator=OP,
        )
        assert await jobs.tick_with(Ports(engine=engine)) is None
        connect(wo)
        report = await jobs.tick_with(Ports(engine=engine, directory=client))
        assert report is not None
        assert report.changed == (ob.id,)
    finally:
        await client.aclose()
        await engine.dispose()

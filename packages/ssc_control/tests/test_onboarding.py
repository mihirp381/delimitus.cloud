"""Ticket 19D: one ``ssc.onboarding`` row per customer, its cell and identity tracks, driven by
``onboarding.service.tick`` against postgres:18 and a fake WorkOS."""

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, LiteralString

import psycopg
import pytest
from fake_workos import FakeWorkOS
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns, new_key

from ssc_contracts.audit import ActorKind
from ssc_contracts.ids import new_id
from ssc_control.audit.chain import Actor
from ssc_control.cells import service as cells
from ssc_control.db import make_engine
from ssc_control.identity import operator
from ssc_control.identity.workos import WorkOSClient
from ssc_control.onboarding import service
from ssc_control.onboarding.service import OnboardingError

OP = "op_dana"
OP_ACTOR = Actor(kind=ActorKind.OPERATOR, id=OP)
DIRECTORY_USER = "directory_user_01ADA"
IDP = "00uada"
EMAIL = "ada@example.com"
JWKS = {"keys": [{"kid": "gw-1", "kty": "EC", "crv": "P-256", "x": "AA", "y": "BB"}]}
OKTA_DIRECTORY = "okta scim v2.0"
GOOGLE_DIRECTORY = "gsuite directory"

_OURS = re.compile(r"\b([a-z]{3})_[a-z0-9]{20}\b")
_WORKOS = re.compile(r"\b(org|directory|conn)_01[0-9A-F]{32}\b")


@pytest.fixture
async def engine(dsns: Dsns) -> AsyncIterator[AsyncEngine]:
    e = make_engine(dsns.app)
    try:
        yield e
    finally:
        await e.dispose()


@pytest.fixture(autouse=True)
def no_other_open_rows(dsns: Dsns) -> None:
    close_open_rows(dsns)


def close_open_rows(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute(
            "update ssc.onboarding set status = 'abandoned', reason = null where status = 'open'"
        )


@pytest.fixture
def wo() -> FakeWorkOS:
    fake = FakeWorkOS()
    fake.user(DIRECTORY_USER, IDP, EMAIL, first="Ada", last="Admin")
    return fake


@pytest.fixture
async def client(wo: FakeWorkOS) -> AsyncIterator[WorkOSClient]:
    c = wo.client()
    try:
        yield c
    finally:
        await c.aclose()


def connect(wo: FakeWorkOS, *, kind: str = "OktaSAML", directory: str = OKTA_DIRECTORY) -> None:
    wo.connection_list.append({"id": wo.sso, "connection_type": kind, "state": "active"})
    wo.directory_list.append({"id": wo.directory, "type": directory, "state": "linked"})


def fresh_domain() -> str:
    return f"acme-{new_key().replace('-', '')[:8].lower()}.example"


async def start(engine: AsyncEngine, client: WorkOSClient, **kw: Any) -> service.Onboarding:
    args: dict[str, Any] = {
        "customer_name": "Acme",
        "domain": fresh_domain(),
        "admin_email": "Ada@Example.com",
        "stage": "prod",
        "operator": OP,
    }
    return await service.start(engine, client, **(args | kw))


async def tick(engine: AsyncEngine, client: WorkOSClient) -> service.TickReport:
    return await service.tick(engine, client, datetime.now(UTC))


async def ready(engine: AsyncEngine, label: str) -> None:
    await cells.ready(engine, label, jwks=JWKS, project_number=1234, actor=OP_ACTOR)


def row(dsns: Dsns, onboarding_id: str) -> dict[str, Any]:
    with psycopg.connect(dsns.superuser) as conn:
        found = conn.execute(
            "select to_jsonb(o) from ssc.onboarding o where id = %s", (onboarding_id,)
        ).fetchone()
    assert found is not None
    return found[0]


def fetch(
    dsns: Dsns, query: LiteralString | sql.Composed, *params: object
) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsns.superuser) as conn:
        return conn.execute(query, params).fetchall()


def tracks(dsns: Dsns, onboarding_id: str) -> tuple[Any, ...]:
    r = row(dsns, onboarding_id)
    return r["status"], r["cell_state"], r["idp_state"], r["reason"]


def actions(dsns: Dsns, org_id: str) -> list[str]:
    rows = fetch(dsns, "select action from ssc.audit_event where org_id = %s order by seq", org_id)
    return [a for (a,) in rows]


def welcome_rows(dsns: Dsns, org_id: str) -> list[tuple[Any, ...]]:
    return fetch(
        dsns,
        "select user_id, kind, approval_id, dedupe_key from ssc.notification_outbox "
        "where org_id = %s",
        org_id,
    )


# ── the happy paths ─────────────────────────────────────────────────────────


async def test_okta_goes_live_with_the_welcome_and_the_audit_event(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    assert (ob.status, ob.cell_state, ob.idp_state) == ("open", "awaiting_cell", "awaiting_idp")
    assert ob.admin_email == EMAIL
    assert ob.workos_organization_id in wo.created
    assert ob.sso_link and ob.dsync_link
    assert fetch(dsns, "select status, org_id from ssc.cell where label = %s", ob.label) == [
        ("building", ob.org_id)
    ]
    assert (await tick(engine, client)).changed == ()

    connect(wo)
    report = await tick(engine, client)
    assert report.changed == (ob.id,)
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "org_created", None)
    assert row(dsns, ob.id)["join_rule"] == "idp_id"
    assert fetch(dsns, "select cell_label from ssc.org where id = %s", ob.org_id) == [(ob.label,)]

    await ready(engine, ob.label)
    report = await tick(engine, client)
    assert report.live == (ob.id,)
    assert tracks(dsns, ob.id) == ("live", "cell_ready", "org_created", None)
    assert actions(dsns, ob.org_id)[-2:] == ["cell.ready", "org.onboarded"]
    (event,) = fetch(
        dsns,
        "select actor_kind, actor_id, target_kind, target_id, after from ssc.audit_event "
        "where org_id = %s and action = 'org.onboarded'",
        ob.org_id,
    )
    assert event == ("operator", "system:create_org", "org", ob.org_id, {"cell_label": ob.label})
    ((admin,),) = fetch(
        dsns, "select id from ssc.user_account where org_id = %s and role = 'admin'", ob.org_id
    )
    assert welcome_rows(dsns, ob.org_id) == [
        (admin, "welcome", None, f"welcome:{ob.org_id}:{admin}")
    ]
    assert (await tick(engine, client)).changed == ()


async def test_google_waits_for_confirmation_and_then_proceeds(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    await ready(engine, ob.label)
    connect(wo, kind="GoogleSAML", directory=GOOGLE_DIRECTORY)
    await tick(engine, client)
    assert tracks(dsns, ob.id) == ("open", "cell_ready", "awaiting_idp", "JOIN_RULE_CONFIRM")
    assert (await tick(engine, client)).changed == ()

    confirmed = await service.confirm(engine, id_or_label=ob.label, join_rule="email", operator=OP)
    assert (confirmed.join_rule, confirmed.reason) == ("email", None)
    with pytest.raises(OnboardingError, match="not waiting for a join rule"):
        await service.confirm(engine, id_or_label=ob.id, join_rule="email", operator=OP)

    assert (await tick(engine, client)).live == (ob.id,)
    assert tracks(dsns, ob.id) == ("live", "cell_ready", "org_created", None)
    assert fetch(
        dsns, "select join_rule from ssc.directory_connection where org_id = %s", ob.org_id
    ) == [("email",)]


async def test_an_oauth_connection_alone_is_unsupported_until_okta_appears(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    connect(wo, kind="FooOAuth")
    await tick(engine, client)
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "awaiting_idp", "IDP_UNSUPPORTED")
    assert "FooOAuth" in row(dsns, ob.id)["last_error"]

    wo.connection_list.append({"id": "conn_okta", "connection_type": "OktaSAML", "state": "active"})
    await tick(engine, client)
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "org_created", None)
    assert row(dsns, ob.id)["last_error"] is None
    assert fetch(
        dsns,
        "select sso_connection_ids from ssc.directory_connection where org_id = %s",
        ob.org_id,
    ) == [(["conn_okta"],)]


async def test_a_missing_admin_waits_until_the_directory_has_them(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    del wo.users[DIRECTORY_USER]
    ob = await start(engine, client)
    connect(wo)
    await tick(engine, client)
    assert tracks(dsns, ob.id) == (
        "open",
        "awaiting_cell",
        "awaiting_idp",
        "ADMIN_NOT_IN_DIRECTORY",
    )
    before = row(dsns, ob.id)
    assert (await tick(engine, client)).changed == ()
    assert row(dsns, ob.id) == before

    wo.user(DIRECTORY_USER, IDP, EMAIL, first="Ada", last="Admin")
    await tick(engine, client)
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "org_created", None)


async def test_an_admin_email_two_directory_users_share_waits_without_naming_it(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    wo.user("directory_user_01TWIN", "00utwin", EMAIL)
    ob = await start(engine, client)
    connect(wo)
    await tick(engine, client)
    assert tracks(dsns, ob.id) == (
        "open",
        "awaiting_cell",
        "awaiting_idp",
        "ADMIN_NOT_IN_DIRECTORY",
    )
    error = row(dsns, ob.id)["last_error"]
    assert "2 users" in error
    assert "@" not in error


# ── WorkOS failures ─────────────────────────────────────────────────────────


async def test_a_workos_failure_at_start_is_retried_without_a_second_organisation(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    wo.fail_on.add("/portal/generate_link")
    ob = await start(engine, client)
    assert (ob.status, ob.idp_state, ob.reason) == ("open", "requested", "WORKOS_ERROR")
    assert ob.last_error == "WorkOS answered HTTP 500 for /portal/generate_link"
    assert ob.workos_organization_id in wo.created
    assert (await tick(engine, client)).failed == (ob.id,)

    wo.fail_on.clear()
    await tick(engine, client)
    after = row(dsns, ob.id)
    assert (after["idp_state"], after["reason"], after["last_error"]) == (
        "awaiting_idp",
        None,
        None,
    )
    assert after["workos_organization_id"] == ob.workos_organization_id
    assert len(wo.created) == 1


async def test_a_verified_organisation_for_the_domain_is_reused_not_created(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    domain = fresh_domain()
    wo.domains[domain] = "verified"
    wo.fail = 500
    ob = await start(engine, client, domain=domain)
    assert (ob.idp_state, ob.reason, ob.workos_organization_id) == (
        "requested",
        "WORKOS_ERROR",
        None,
    )
    wo.fail = None
    await tick(engine, client)
    assert row(dsns, ob.id)["workos_organization_id"] == wo.organization
    assert row(dsns, ob.id)["idp_state"] == "awaiting_idp"
    assert wo.created == {}


async def test_a_workos_error_on_one_row_leaves_the_others_ticking(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    broken = await start(engine, client)
    ob = await start(engine, client)
    assert broken.workos_organization_id is not None
    wo.fail_for.add(broken.workos_organization_id)
    connect(wo)
    report = await tick(engine, client)
    assert report.failed == (broken.id,)
    assert set(report.changed) == {broken.id, ob.id}
    assert tracks(dsns, broken.id) == ("open", "awaiting_cell", "awaiting_idp", "WORKOS_ERROR")
    assert "HTTP 500" in row(dsns, broken.id)["last_error"]
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "org_created", None)


# ── the operator's commands ─────────────────────────────────────────────────


async def test_start_refuses_bad_input_and_writes_nothing(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    taken = await start(engine, client)
    count = len(fetch(dsns, "select 1 from ssc.onboarding"))
    bad: list[dict[str, Any]] = [
        {"customer_name": ""},
        {"customer_name": "A" * 121},
        {"domain": "not a domain"},
        {"domain": "localhost"},
        {"admin_email": "nobody"},
        {"stage": "dev"},
        {"operator": "dana"},
        {"label": "Bad"},
        {"label": taken.label},
        {"org_id": "usr_" + "a" * 20},
        {"org_id": taken.org_id},
    ]
    for kw in bad:
        with pytest.raises(ValueError):  # noqa: PT011
            await start(engine, client, **kw)
    assert len(fetch(dsns, "select 1 from ssc.onboarding")) == count
    assert len(wo.created) == 1


async def test_a_generated_label_is_a_fresh_cell_label(
    engine: AsyncEngine, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    assert re.fullmatch(r"[a-z][a-z0-9]{11}", ob.label)
    chosen = "t" + new_key().replace("-", "")[:11].lower()
    org_id = new_id("org")
    named = await start(engine, client, label=chosen, org_id=org_id)
    assert (named.label, named.org_id) == (chosen, org_id)
    assert await service.status(engine, chosen) == named
    assert await service.status(engine, named.id) == named
    assert {o.id for o in await service.list_all(engine)} >= {ob.id, named.id}
    with pytest.raises(OnboardingError, match="no onboarding"):
        await service.status(engine, "nosuchlabel1")


async def test_abandon_retires_the_cell_and_is_refused_once_live(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    done = await service.abandon(engine, id_or_label=ob.id, operator=OP, reason="customer withdrew")
    assert done.onboarding.status == "abandoned"
    assert done.lines == (
        f"uv run python -m ssc_infra.onboard {ob.label} --stage prod --destroy",
        f"WorkOS organisation {ob.workos_organization_id} is kept: delete it in WorkOS if the "
        "customer is not coming back",
    )
    assert fetch(dsns, "select status from ssc.cell where label = %s", ob.label) == [("retired",)]
    with pytest.raises(OnboardingError, match="already abandoned"):
        await service.abandon(engine, id_or_label=ob.id, operator=OP, reason="again")
    assert (await tick(engine, client)).changed == ()

    live = await start(engine, client)
    connect(wo)
    await ready(engine, live.label)
    await tick(engine, client)
    with pytest.raises(OnboardingError, match="is live"):
        await service.abandon(engine, id_or_label=live.label, operator=OP, reason="late")
    assert tracks(dsns, live.id)[0] == "live"


async def test_abandon_is_refused_while_the_org_is_on_the_cell(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    connect(wo)
    await tick(engine, client)
    with pytest.raises(OnboardingError, match=f"{ob.org_id} is on cell {ob.label}"):
        await service.abandon(engine, id_or_label=ob.id, operator=OP, reason="customer withdrew")
    assert tracks(dsns, ob.id) == ("open", "awaiting_cell", "org_created", None)
    assert fetch(dsns, "select status from ssc.cell where label = %s", ob.label) == [("building",)]


async def test_confirm_is_refused_unless_the_row_waits_for_it(
    engine: AsyncEngine, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    with pytest.raises(OnboardingError, match="not waiting for a join rule"):
        await service.confirm(engine, id_or_label=ob.id, join_rule="email", operator=OP)
    with pytest.raises(OnboardingError, match="only email"):
        await service.confirm(engine, id_or_label=ob.id, join_rule="idp_id", operator=OP)


async def test_refreshed_links_replace_the_stored_ones_while_identity_waits(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    calls = wo.calls.count("/portal/generate_link")
    fresh = await service.refresh_links(engine, client, id_or_label=ob.label)
    assert (fresh.sso_link, fresh.dsync_link) == (ob.sso_link, ob.dsync_link)
    assert wo.calls.count("/portal/generate_link") == calls + 2
    connect(wo)
    await tick(engine, client)
    with pytest.raises(OnboardingError, match="org_created"):
        await service.refresh_links(engine, client, id_or_label=ob.label)


async def test_log_lines_name_the_row_and_never_an_email(
    engine: AsyncEngine,
    wo: FakeWorkOS,
    client: WorkOSClient,
    caplog: pytest.LogCaptureFixture,
    unredacted_logging: None,
) -> None:
    caplog.set_level(logging.INFO, logger="ssc.onboarding")
    ob = await start(engine, client)
    connect(wo)
    await ready(engine, ob.label)
    await tick(engine, client)
    lines = [r for r in caplog.records if r.name == "ssc.onboarding"]
    assert lines
    assert all(ob.id in r.getMessage() for r in lines)
    for r in lines:
        assert "@" not in r.getMessage()
        assert "@" not in json.dumps({k: str(v) for k, v in vars(r).items()})


# ── the org it makes ────────────────────────────────────────────────────────


def _normal(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "L" if k == "cell_label" else _normal(v)
            for k, v in value.items()
            if not k.endswith("_at") and k not in {"hash", "prev_hash"}
        }
    if isinstance(value, list):
        return [_normal(v) for v in value]
    if isinstance(value, str):
        return _WORKOS.sub(r"\1_W", _OURS.sub(r"\1_X", value))
    return value


def org_rows(dsns: Dsns, org_id: str) -> dict[str, list[str]]:
    skip = {"audit_event", "audit_head", "notification_outbox", "org_index", "cell", "onboarding"}
    tables = [
        t
        for (t,) in fetch(
            dsns,
            "select table_name from information_schema.columns "
            "where table_schema = 'ssc' and column_name = 'org_id' order by 1",
        )
        if t not in skip
    ]
    found: dict[str, list[str]] = {}
    for t in [*tables, "org"]:
        key = "id" if t == "org" else "org_id"
        query = sql.SQL("select to_jsonb(r) from ssc.{} r where {} = %s")
        rows = fetch(dsns, query.format(sql.Identifier(t), sql.Identifier(key)), org_id)
        if rows:
            found[t] = sorted(json.dumps(_normal(r), sort_keys=True) for (r,) in rows)
    return found


def org_events(dsns: Dsns, org_id: str) -> list[Any]:
    rows = fetch(
        dsns,
        "select action, actor_kind, actor_id, target_kind, target_id, before, after "
        "from ssc.audit_event where org_id = %s order by seq",
        org_id,
    )
    return [_normal(list(r)) for r in rows]


async def test_the_onboarded_org_matches_one_create_org_makes(
    dsns: Dsns, engine: AsyncEngine, wo: FakeWorkOS, client: WorkOSClient
) -> None:
    ob = await start(engine, client)
    await ready(engine, ob.label)
    connect(wo)
    await tick(engine, client)
    assert tracks(dsns, ob.id)[0] == "live"

    other = FakeWorkOS()
    other.user(DIRECTORY_USER, IDP, EMAIL, first="Ada", last="Admin")
    other_client = other.client()
    try:
        made = await operator.create_org_with_directory(
            engine,
            other_client,
            name="Acme",
            founder_name="Ada Admin",
            founder_email=EMAIL,
            founder_idp_id=IDP,
            workos_organization_id=other.organization,
            workos_directory_id=other.directory,
            sso_connection_ids=[other.sso],
            join_rule="idp_id",
            admin_group_ref=None,
            cell_label="t" + new_key().replace("-", "")[:11].lower(),
            actor=OP_ACTOR,
        )
    finally:
        await other_client.aclose()

    assert org_rows(dsns, ob.org_id) == org_rows(dsns, made.org.org_id)
    onboarded = org_events(dsns, ob.org_id)
    assert onboarded[-1][0] == "org.onboarded"
    assert onboarded[:-1] == org_events(dsns, made.org.org_id)

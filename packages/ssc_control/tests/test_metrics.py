"""SSC-028: metrics events, org-scoped pseudonyms and the report with the pilot kill criteria.

Ticket "done when" (the report shows every metric) -> test_report_shows_every_metric_at_n40 and
test_report_flags_every_small_sample_at_n5, on a synthetic dataset holding all seven kinds.
Plus: the pseudonym scheme, key handling, refused properties, the 0007 checks, the source tool
and the share events PUT grants records.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import psycopg
import psycopg.sql
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from sqlalchemy import text
from ssc_testkit import ISSUER, Dsns, SigningKey, assert_problem, auth, mint, new_key

from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import new_id
from ssc_control.api import Settings, create_app
from ssc_control.api.auth import Principal
from ssc_control.db import (
    CreatedOrg,
    NewOrg,
    bind_org_sync,
    bound_org,
    create_org,
    downgrade,
    make_engine,
    upgrade,
)
from ssc_control.db.errors import CHECK_VIOLATION
from ssc_control.metrics import (
    SOURCE_TOOL_HEADER,
    DerivedKeys,
    Metrics,
    MetricsKeyError,
    metrics_port,
    parse_master_key,
    pseudonym,
    source_tool_of,
)
from ssc_control.metrics import report as rep
from ssc_control.ports import MetricKind, NullMetricsPort

MASTER = bytes(range(32))
KEYS = DerivedKeys(MASTER)
ORG_A = "org_" + "a" * 20
ORG_B = "org_" + "b" * 20
USER = "usr_" + "u" * 20
DAY0 = date(2026, 6, 1)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


async def new_org(dsn: str, name: str = "Metrics") -> CreatedOrg:
    engine = make_engine(dsn)
    try:
        return await create_org(
            engine, NewOrg(name, "Ada Admin", "ada@example.com", ISSUER, new_id("usr"))
        )
    finally:
        await engine.dispose()


# ── pseudonyms and keys ──────────────────────────────────────────────────────


def test_pseudonym_is_stable_within_an_org_and_differs_across_orgs() -> None:
    p = pseudonym(KEYS, ORG_A, USER)
    assert p == pseudonym(DerivedKeys(MASTER), ORG_A, USER)
    assert p != pseudonym(KEYS, ORG_B, USER)
    assert p != pseudonym(DerivedKeys(bytes(32)), ORG_A, USER)
    assert re.fullmatch(r"[0-9a-f]{32}", p)
    assert USER not in p
    assert USER.removeprefix("usr_") not in p


def test_the_scheme_is_the_documented_hmac() -> None:
    org_key = hmac.new(MASTER, b"ssc-metrics-v1\0" + ORG_A.encode(), hashlib.sha256).digest()
    assert KEYS.org_key(ORG_A) == org_key
    expected = hmac.new(org_key, USER.encode(), hashlib.sha256).hexdigest()[:32]
    assert pseudonym(KEYS, ORG_A, USER) == expected
    assert "redacted" in repr(KEYS)
    assert MASTER.hex() not in repr(KEYS)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "not set"),
        ("", "not set"),
        ("not base64!", "not valid base64"),
        (b64(bytes(16)), "32 bytes"),
        (b64(bytes(33)), "32 bytes"),
    ],
)
def test_a_missing_or_malformed_key_is_refused(value: str | None, message: str) -> None:
    with pytest.raises(MetricsKeyError, match=message) as e:
        parse_master_key(value)
    if value:
        assert value not in str(e.value)


def test_keys_and_ids_are_checked() -> None:
    assert parse_master_key(b64(MASTER)) == MASTER
    with pytest.raises(MetricsKeyError):
        DerivedKeys(b"short")
    with pytest.raises(ValueError, match="usr_ ids only"):
        pseudonym(KEYS, ORG_A, "ada@example.com")
    with pytest.raises(ValueError, match="usr_ ids only"):
        pseudonym(KEYS, ORG_A, "op_ada")
    with pytest.raises(ValueError):
        KEYS.org_key("acme")


def test_a_process_records_only_with_a_key() -> None:
    assert isinstance(metrics_port(None), NullMetricsPort)
    assert isinstance(metrics_port(MASTER), Metrics)
    with pytest.raises(MetricsKeyError):
        metrics_port(b"short")


def test_settings_read_the_metrics_key_and_never_show_it() -> None:
    env = {
        "SSC_DATABASE_DSN": "postgresql://ssc_app:x@db/ssc",
        "SSC_API_JWKS": json.dumps({"keys": []}),
        "SSC_API_ISSUER": ISSUER,
    }
    assert Settings.from_env(env).metrics_key is None
    s = Settings.from_env({**env, "SSC_METRICS_KEY": b64(MASTER)})
    assert s.metrics_key == MASTER
    assert "metrics_key" not in repr(s)
    with pytest.raises(MetricsKeyError):
        Settings.from_env({**env, "SSC_METRICS_KEY": b64(bytes(8))})


# ── source tool ──────────────────────────────────────────────────────────────


def principal(*, agent: bool = False, client_id: str | None = None) -> Principal:
    return Principal(
        org_id=ORG_A,
        subject=USER,
        kind="user",
        credential_id="cred_x",
        is_agent=agent,
        client_id=client_id,
    )


@pytest.mark.parametrize(
    ("caller", "header", "expected"),
    [
        (principal(), None, None),
        (principal(), "  ", None),
        (principal(), "Claude-Code", "claude-code"),
        (principal(), " cursor ", "cursor"),
        (principal(), "lovable.dev_2", "lovable.dev_2"),
        (principal(), "Claude Code", "other"),
        (principal(), "x" * 41, "other"),
        (principal(), "ada@example.com", "other"),
        (principal(agent=True, client_id="Agent-X"), "cursor", "agent-x"),
        (principal(agent=True, client_id=None), "cursor", "cursor"),
        (principal(agent=False, client_id="agent-x"), "cursor", "cursor"),
        (principal(agent=True, client_id="an agent!"), None, "other"),
    ],
)
def test_source_tool_of(caller: Principal, header: str | None, expected: str | None) -> None:
    assert source_tool_of(caller, header) == expected


def test_the_header_name_is_the_documented_contract() -> None:
    assert SOURCE_TOOL_HEADER == "X-SSC-Source-Tool"


# ── recording ────────────────────────────────────────────────────────────────


class NoConnection:
    async def execute(self, *_: object, **__: object) -> None:
        raise AssertionError("nothing may be written")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"properties": {"email": "x"}}, "names a person"),
        ({"properties": {"user_id": "x"}}, "names a person"),
        ({"properties": {"display_name": "Ada"}}, "names a person"),
        ({"properties": {"note": "ada@example.com"}}, "user id or an email"),
        ({"properties": {"note": f"by {USER}"}}, "user id or an email"),
        ({"properties": {"Role": "user"}}, "snake_case"),
        ({"properties": {"roles": ["a"]}}, "flat scalar"),
        ({"properties": {"nested": {"a": 1}}}, "flat scalar"),
        ({"properties": {"ratio": math.nan}}, "finite"),
        ({"properties": {"note": "x" * 201}}, "too long"),
        ({"properties": {f"k{i}": i for i in range(21)}}, "at most 20"),
        ({"app_id": "ledger"}, "app id"),
        ({"at": datetime(2026, 6, 1)}, "time zone"),  # noqa: DTZ001  (the naive time refused)
        ({"user_id": "ada@example.com"}, "usr_ ids only"),
    ],
)
async def test_record_event_refuses_what_it_must_never_store(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await Metrics(KEYS).record_event(
            NoConnection(),  # type: ignore[arg-type]
            org_id=ORG_A,
            kind=MetricKind.SHARE,
            **kwargs,
        )


def metrics_rows(dsn: str, org: str) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, org)
        return conn.execute(
            "select kind, pseudonym, app_id, source_tool, properties, at "
            "from ssc.metrics_event order by id"
        ).fetchall()


async def test_record_event_stores_a_pseudonym_never_the_user(dsns: Dsns) -> None:
    org = await new_org(dsns.app)
    app = new_id("app")
    engine = make_engine(dsns.app)
    at = datetime(2026, 6, 2, 9, 30, tzinfo=UTC)
    try:
        async with bound_org(engine, org.org_id) as conn:
            await Metrics(KEYS).record_event(
                conn,
                org_id=org.org_id,
                kind=MetricKind.DEPLOY,
                app_id=app,
                user_id=org.admin_user_id,
                source_tool="Claude-Code",
                properties={"environment": "prod", "files": 3, "ok": True, "ratio": 0.5},
                at=at,
            )
            await Metrics(KEYS).record_event(conn, org_id=org.org_id, kind=MetricKind.TIMER_RUN)
    finally:
        await engine.dispose()
    first, second = metrics_rows(dsns.app, org.org_id)
    assert first == {
        "kind": "deploy",
        "pseudonym": pseudonym(KEYS, org.org_id, org.admin_user_id),
        "app_id": app,
        "source_tool": "claude-code",
        "properties": {"environment": "prod", "files": 3, "ok": True, "ratio": 0.5},
        "at": at,
    }
    assert org.admin_user_id not in json.dumps(first, default=str)
    assert (second["kind"], second["pseudonym"], second["properties"]) == ("timer_run", None, {})


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("pseudonym", USER, "metrics_event_pseudonym_check"),
        ("pseudonym", "ada@example.com", "metrics_event_pseudonym_check"),
        ("source_tool", "Claude Code", "metrics_event_source_tool_check"),
        ("app_id", "ledger", "metrics_event_app_id_check"),
        ("properties", json.dumps({"by": USER}), "metrics_event_properties_check"),
        ("properties", json.dumps({"to": "ada@example.com"}), "metrics_event_properties_check"),
        ("properties", json.dumps([1, 2]), "metrics_event_properties_check"),
    ],
)
async def test_the_database_refuses_raw_ids_and_emails(
    dsns: Dsns, column: str, value: str, constraint: str
) -> None:
    org = await new_org(dsns.app)
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        with pytest.raises(psycopg.Error) as e:
            conn.execute(
                psycopg.sql.SQL(
                    "insert into ssc.metrics_event (org_id, kind, {}) values (%s, 'share', %s)"
                ).format(psycopg.sql.Identifier(column)),
                (org.org_id, value),
            )
    assert (e.value.sqlstate, e.value.diag.constraint_name) == (CHECK_VIOLATION, constraint)


def test_0007_adds_the_checks_and_downgrade_drops_them(dsns: Dsns) -> None:
    def checks() -> set[str]:
        with psycopg.connect(dsns.migrate) as conn:
            rows = conn.execute(
                "select conname from pg_constraint "
                "where conrelid = 'ssc.metrics_event'::regclass and contype = 'c'"
            ).fetchall()
        return {str(n) for (n,) in rows}

    ours = {
        "metrics_event_pseudonym_check",
        "metrics_event_source_tool_check",
        "metrics_event_app_id_check",
        "metrics_event_properties_check",
    }
    assert ours <= checks()
    # Re-adding the checks scans rows under forced RLS with no org bound: there must be some.
    org = asyncio.run(new_org(dsns.app))
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, org.org_id)
        conn.execute(
            "insert into ssc.metrics_event (org_id, kind, pseudonym, source_tool, properties) "
            "values (%s, 'deploy', %s, 'cursor', '{\"environment\": \"prod\"}')",
            (org.org_id, pseudonym(KEYS, org.org_id, org.admin_user_id)),
        )
    downgrade(dsns.migrate, "0006_procrastinate_orgindex")
    assert not ours & checks()
    assert "metrics_event_kind_check" in checks()
    upgrade(dsns.migrate)
    assert ours <= checks()


# ── share events from PUT grants ─────────────────────────────────────────────


@dataclass(frozen=True)
class World:
    org: str
    admin: str
    approver: str
    member: str
    app: str
    prod: str
    preview: str


def add_account(conn: psycopg.Connection[Any], org: str, role: str) -> str:
    uid = new_id("usr")
    conn.execute(
        "insert into ssc.user_account (id, org_id, display_name, email, role) "
        "values (%s, %s, 'Some One', 'someone@example.com', %s)",
        (uid, org, role),
    )
    return uid


async def make_world(dsn: str) -> World:
    created = await new_org(dsn)
    org, admin = created.org_id, created.admin_user_id
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        approver = add_account(conn, org, "admin")
        member = add_account(conn, org, "member")
        app, prod, preview = new_id("app"), new_id("env"), new_id("env")
        conn.execute(
            "insert into ssc.app (id, org_id, slug, owner_user_id) values (%s, %s, 'ledger', %s)",
            (app, org, admin),
        )
        for env, env_name in ((prod, "prod"), (preview, "preview")):
            conn.execute(
                "insert into ssc.environment (id, org_id, app_id, name) values (%s, %s, %s, %s)",
                (env, org, app, env_name),
            )
    return World(org, admin, approver, member, app, prod, preview)


def settings_for(dsns: Dsns, signing_key: SigningKey, metrics_key: bytes | None) -> Settings:
    return Settings(
        database_dsn=dsns.app,
        jwks={"keys": [signing_key.jwk]},
        issuer=ISSUER,
        rate_capacity=1000,
        rate_refill_per_second=1000.0,
        metrics_key=metrics_key,
    )


@pytest.fixture(scope="module")
def client(dsns: Dsns, signing_key: SigningKey) -> Any:
    with TestClient(create_app(settings_for(dsns, signing_key, MASTER))) as c:
        yield c


@pytest.fixture
def world(dsns: Dsns) -> World:
    return asyncio.run(make_world(dsns.app))


def token(key: SigningKey, w: World, sub: str, **claims: Any) -> str:
    return mint(key, org=w.org, sub=sub, jti=f"cred_{new_key()[:16]}", **claims)


def put(  # noqa: PLR0913
    client: TestClient,
    w: World,
    env: str,
    bearer: str,
    grants: list[dict[str, Any]],
    version: int,
    **headers: str,
) -> Any:
    return client.put(
        f"/v1/apps/{w.app}/environments/{env}/grants",
        json={"grants": grants},
        headers=auth(bearer, **{"If-Match": f'"{version}"', **headers}),
    )


def shares(dsns: Dsns, w: World) -> list[dict[str, Any]]:
    return [r for r in metrics_rows(dsns.app, w.org) if r["kind"] == "share"]


def user_grant(uid: str, role: str = "user") -> dict[str, Any]:
    return {"role": role, "subject_kind": "user", "subject_id": uid}


ORG_WIDE = {"role": "user", "subject_kind": "org", "subject_id": None}


def test_put_grants_records_one_share_event_per_added_grant(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    admin = token(signing_key, world, world.admin)
    two = [user_grant(world.member), user_grant(world.approver, "builder")]
    r = put(client, world, world.preview, admin, two, 1, **{SOURCE_TOOL_HEADER: "Claude-Code"})
    assert r.status_code == 200, r.text
    rows = shares(dsns, world)
    assert len(rows) == 2
    for row in rows:
        assert row["pseudonym"] == pseudonym(KEYS, world.org, world.admin)
        assert (row["app_id"], row["source_tool"]) == (world.app, "claude-code")
        assert json.dumps(row["properties"]).count("usr_") == 0
    assert sorted((r["properties"]["role"], r["properties"]["subject_kind"]) for r in rows) == [
        ("builder", "user"),
        ("user", "user"),
    ]
    assert {json.dumps(r["properties"], sort_keys=True) for r in rows} >= {
        json.dumps(
            {"environment": "preview", "role": "user", "subject_kind": "user", "via_agent": False},
            sort_keys=True,
        )
    }
    # Keeping one and adding one records one; removing and re-sending the same set record none.
    three = [two[0], ORG_WIDE]
    assert put(client, world, world.preview, admin, three, 2).status_code == 200
    (added,) = shares(dsns, world)[2:]
    assert (added["properties"]["subject_kind"], added["source_tool"]) == ("org", None)
    assert put(client, world, world.preview, admin, three, 3).status_code == 200
    assert put(client, world, world.preview, admin, [], 4).status_code == 200
    assert len(shares(dsns, world)) == 3


def test_share_events_wait_until_the_change_applies(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    agent = token(signing_key, world, world.admin, agent=True, client_id="Agent-X")
    operator = token(signing_key, world, "op_ada", kind="operator")
    wider = [user_grant(world.member)]
    pending = put(client, world, world.prod, agent, wider, 1, **{SOURCE_TOOL_HEADER: "cursor"})
    assert pending.status_code == 202, pending.text
    assert shares(dsns, world) == []
    (apr,) = pending.json()["approval_ids"]
    decided = client.post(
        f"/v1/approvals/{apr}/decision",
        json={
            "outcome": "approved",
            "approver_user_id": world.approver,
            "channel": "email",
            "reason": "Yes, by email.",
        },
        headers=auth(operator, **{"Idempotency-Key": new_key()}),
    )
    assert decided.status_code == 200, decided.text
    applied = put(client, world, world.prod, agent, wider, 1, **{SOURCE_TOOL_HEADER: "cursor"})
    assert applied.status_code == 200, applied.text
    (row,) = shares(dsns, world)
    assert row["source_tool"] == "agent-x"
    assert row["properties"] == {
        "environment": "prod",
        "role": "user",
        "subject_kind": "user",
        "via_agent": True,
    }


def test_a_refused_change_records_nothing(
    client: TestClient, world: World, dsns: Dsns, signing_key: SigningKey
) -> None:
    admin = token(signing_key, world, world.admin)
    asked = client.post(
        "/v1/approvals",
        json={
            "environment_id": world.prod,
            "kind": "connect_data_source",
            "subject_key": "finance",
            "payload": {},
        },
        headers=auth(admin, **{"Idempotency-Key": new_key()}),
    )
    assert asked.status_code == 201, asked.text
    assert_problem(
        put(client, world, world.prod, admin, [ORG_WIDE], 1), ErrorCode.APPROVAL_REQUIRED
    )
    assert_problem(
        put(client, world, world.preview, admin, [ORG_WIDE], 7), ErrorCode.PRECONDITION_STALE
    )
    assert shares(dsns, world) == []


def test_without_a_key_nothing_is_recorded(
    world: World, dsns: Dsns, signing_key: SigningKey, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(create_app(settings_for(dsns, signing_key, None))) as c:
        assert "SSC_METRICS_KEY is not set" in caplog.text
        admin = token(signing_key, world, world.admin)
        assert put(c, world, world.preview, admin, [ORG_WIDE], 1).status_code == 200
    assert metrics_rows(dsns.app, world.org) == []


# ── the report ───────────────────────────────────────────────────────────────

START = datetime.combine(DAY0, time(), UTC)
TOOLS = ("claude-code", "Lovable", None, "Replit")
DAY60 = (DAY0 + timedelta(days=59)).isoformat()


async def seed(dsn: str, n: int) -> str:
    """``n`` builders, one app each, and every event kind; even builders deploy a second app
    in pilot week 6 from Lovable. See the tests for the numbers this yields."""
    created = await new_org(dsn, f"Pilot {n}")
    org, admin = created.org_id, created.admin_user_id
    apps = [new_id("app") for _ in range(n)]
    builders = [new_id("usr") for _ in range(n)]
    users = [new_id("usr") for _ in range(7)]
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, org)
        for i, app in enumerate(apps):
            conn.execute(
                "insert into ssc.app (id, org_id, slug, owner_user_id, created_at) "
                "values (%s, %s, %s, %s, %s)",
                (app, org, f"app-{i}", admin, START + timedelta(hours=i)),
            )
    metrics = Metrics(KEYS)
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:

            async def event(
                kind: MetricKind,
                at: datetime,
                app: str,
                user: str | None = None,
                tool: str | None = None,
            ) -> None:
                await metrics.record_event(
                    conn, org_id=org, kind=kind, app_id=app, user_id=user, source_tool=tool, at=at
                )

            for i, (app, builder) in enumerate(zip(apps, builders, strict=True)):
                made = START + timedelta(hours=i)
                await event(
                    MetricKind.DEPLOY, made + timedelta(minutes=5), app, builder, TOOLS[i % 4]
                )
                await event(
                    MetricKind.FIRST_URL, made + timedelta(minutes=10 * (i + 1)), app, builder
                )
                await event(MetricKind.SHARE, START + timedelta(days=2), app, builder)
                await event(MetricKind.APP_OPENED, START + timedelta(days=3), app, users[i % 7])
                await event(MetricKind.APP_OPENED, START + timedelta(days=10), app, users[i % 3])
                if i % 3 == 0:
                    await event(MetricKind.DATA_QUERY, START + timedelta(days=4), app)
                if i % 5 == 0:
                    await event(MetricKind.DATABASE_USE, START + timedelta(days=4), app)
                if i % 2 == 0:
                    second = apps[(i + 1) % n]
                    await event(
                        MetricKind.DEPLOY, START + timedelta(days=36), second, builder, "lovable"
                    )
            await event(MetricKind.TIMER_RUN, START + timedelta(days=5), apps[0])
    finally:
        await engine.dispose()
    return org


@pytest.fixture(scope="module")
def pilot40(dsns: Dsns) -> str:
    return asyncio.run(seed(dsns.app, 40))


@pytest.fixture(scope="module")
def pilot5(dsns: Dsns) -> str:
    return asyncio.run(seed(dsns.app, 5))


def report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dsn: str, *argv: str
) -> str:
    monkeypatch.setenv(rep.DSN_ENV, dsn)
    assert rep.main(list(argv)) == 0
    return capsys.readouterr().out


def report_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dsn: str, *argv: str
) -> dict[str, Any]:
    return json.loads(report(monkeypatch, capsys, dsn, *argv, "--json"))


HEADINGS = (
    "1. Time to first URL",
    "2. Apps per builder",
    "3. Weekly unique users",
    "4. Share of apps using data or state",
    "5. Source tool mix",
    "6. Weekly active builders",
)


def under(out: str, heading: str) -> str:
    lines = out.splitlines()
    (at,) = [i for i, line in enumerate(lines) if line.startswith(heading)]
    return lines[at + 1].strip()


def keys_of(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: keys_of(v) for k, v in value.items()}
    if isinstance(value, list):
        return [keys_of(value[0])] if value else []
    return None


RATE_KEYS = {
    "successes": None,
    "n": None,
    "small_sample": None,
    "rate": None,
    "low": None,
    "high": None,
}
CRITERION_KEYS = dict.fromkeys(
    (
        "id",
        "label",
        "target",
        "due_day",
        "verdict",
        "observed",
        "n",
        "small_sample",
        "low",
        "high",
        "versus_target",
        "note",
    )
)


def test_report_shows_every_metric_at_n40(
    pilot40: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ("--org", pilot40, "--since", DAY0.isoformat(), "--as-of", DAY60)
    out = report(monkeypatch, capsys, dsns.app, *args)
    for heading in HEADINGS:
        assert "insufficient" not in under(out, heading), heading
    assert under(out, HEADINGS[0]) == "median 3h 25m, p75 5h 02m (n=40)"
    assert under(out, HEADINGS[1]) == "mean 1.50 (n=40)"
    assert under(out, HEADINGS[2]) == "2026-W23  7 users"
    assert under(out, HEADINGS[3]) == "47.5% [32.9%–62.5%] n=40"
    assert under(out, HEADINGS[4]) == "lovable: 30 of 60 deploys, 50.0% [37.7%–62.3%] n=60"
    assert under(out, HEADINGS[5]) == "2026-W23  40 builders"
    assert "not declared: 10 of 60 deploys" in out
    assert "builders by apps: 1 app(s): 20, 2 app(s): 20" in out

    body = report_json(monkeypatch, capsys, dsns.app, *args)
    assert body["format"] == "ssc-metrics-report/v1"
    assert body["window"] == {"since": "2026-06-01", "as_of": "2026-07-30", "elapsed_days": 60}
    assert body["events"] == {
        "first_url": 40,
        "deploy": 60,
        "share": 40,
        "app_opened": 80,
        "data_query": 14,
        "database_use": 8,
        "timer_run": 1,
    }
    m = body["metrics"]
    assert m["time_to_first_url"] == {
        "n": 40,
        "small_sample": False,
        "median_seconds": 12300.0,
        "p75_seconds": 18150.0,
    }
    assert m["apps_per_builder"] == {
        "n": 40,
        "small_sample": False,
        "mean": 1.5,
        "distribution": {"1": 20, "2": 20},
    }
    assert m["weekly_unique_users"] == [
        {"week": "2026-W23", "users": 7},
        {"week": "2026-W24", "users": 3},
    ]
    assert m["weekly_active_builders"] == [
        {"week": "2026-W23", "builders": 40},
        {"week": "2026-W28", "builders": 20},
    ]
    usage = m["apps_using_data_or_state"]
    assert (usage["successes"], usage["n"], usage["rate"]) == (19, 40, 0.475)
    assert [(t["tool"], t["deploys"]) for t in m["source_tool_mix"]["tools"]] == [
        ("lovable", 30),
        ("claude-code", 10),
        ("replit", 10),
        (None, 10),
    ]
    assert all(t["rate"] is not None for t in m["source_tool_mix"]["tools"])


def test_report_json_shape_is_pinned(
    pilot40: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ("--org", pilot40, "--since", DAY0.isoformat(), "--as-of", DAY60)
    body = report_json(monkeypatch, capsys, dsns.app, *args)
    shape = keys_of(body)
    shape["metrics"]["apps_per_builder"]["distribution"] = None
    assert shape == {
        "format": None,
        "org_id": None,
        "window": {"since": None, "as_of": None, "elapsed_days": None},
        "min_n": None,
        "events": dict.fromkeys(k.value for k in MetricKind),
        "metrics": {
            "time_to_first_url": dict.fromkeys(
                ("n", "small_sample", "median_seconds", "p75_seconds")
            ),
            "apps_per_builder": dict.fromkeys(("n", "small_sample", "mean", "distribution")),
            "weekly_unique_users": [{"week": None, "users": None}],
            "apps_using_data_or_state": RATE_KEYS,
            "source_tool_mix": {"n": None, "tools": [{"tool": None, "deploys": None, **RATE_KEYS}]},
            "weekly_active_builders": [{"week": None, "builders": None}],
        },
        "kill_criteria": [CRITERION_KEYS],
        "stop_rule": {"misses": None, "open": None, "decision": None},
    }
    assert [c["id"] for c in body["kill_criteria"]] == [
        "active_builders_week6",
        "apps_per_builder_day60",
        "apps_using_data_day60",
        "tools_side_by_side",
        "approved_path",
        "paid_conversion",
    ]
    assert body["min_n"] == 20


def test_kill_criteria_and_the_stop_rule_at_n40(
    pilot40: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ("--org", pilot40, "--since", DAY0.isoformat(), "--as-of", DAY60)
    pilot = ("--invited-builders", "40", "--approved-path", "yes", "--paid", "no")
    body = report_json(monkeypatch, capsys, dsns.app, *args, *pilot)
    got = {c["id"]: c for c in body["kill_criteria"]}
    assert [c["verdict"] for c in got.values()] == ["met", "missed", "met", "met", "met", "missed"]
    k1 = got["active_builders_week6"]
    assert (k1["observed"], k1["n"], k1["small_sample"], k1["versus_target"]) == (
        0.5,
        40,
        False,
        "indistinguishable",
    )
    assert (round(k1["low"], 4), round(k1["high"], 4)) == (0.3520, 0.6480)
    k2 = got["apps_per_builder_day60"]
    assert (k2["observed"], k2["n"], k2["low"]) == (1.5, 40, None)
    assert (got["apps_using_data_day60"]["observed"], got["apps_using_data_day60"]["n"]) == (
        0.475,
        40,
    )
    k4 = got["tools_side_by_side"]
    assert (k4["observed"], k4["n"], k4["note"]) == (2.0, 40, "claude-code, lovable")
    assert body["stop_rule"] == {"misses": 2, "open": 0, "decision": "stop"}
    out = report(monkeypatch, capsys, dsns.app, *args, *pilot)
    assert "[missed] ≥2 apps per active builder by day 60: 1.50 (n=40)" in out
    assert (
        "[met] ≥40% of invited builders active weekly by week 6: "
        "50.0% (20 of 40) [35.2%–64.8%], interval spans target"
    ) in out
    assert (
        "[met] apps from ≥2 builder tools running side by side: "
        "2 (claude-code, lovable) across 40 running apps"
    ) in out
    assert "Stop rule, two misses by day 60: stop (2 missed, 0 open)" in out


def test_report_flags_every_small_sample_at_n5(
    pilot5: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = (
        "--org",
        pilot5,
        "--since",
        DAY0.isoformat(),
        "--as-of",
        DAY60,
        "--invited-builders",
        "5",
    )
    out = report(monkeypatch, capsys, dsns.app, *args)
    assert under(out, HEADINGS[0]) == "insufficient data (n=5, need 20)"
    assert under(out, HEADINGS[1]) == "insufficient data (n=5, need 20)"
    assert under(out, HEADINGS[2]) == "2026-W23  5 users"
    assert under(out, HEADINGS[3]) == "insufficient data (n=5, need 20)"
    assert under(out, HEADINGS[4]) == "lovable: 4 of 8 deploys, insufficient data (n=8, need 20)"
    assert under(out, HEADINGS[5]) == "2026-W23  5 builders"
    body = report_json(monkeypatch, capsys, dsns.app, *args)
    m = body["metrics"]
    assert (m["time_to_first_url"]["small_sample"], m["time_to_first_url"]["median_seconds"]) == (
        True,
        None,
    )
    assert (m["apps_per_builder"]["mean"], m["apps_per_builder"]["distribution"]) == (
        None,
        {"1": 2, "2": 3},
    )
    assert m["apps_using_data_or_state"] == {
        "successes": 2,
        "n": 5,
        "small_sample": True,
        "rate": None,
        "low": None,
        "high": None,
    }
    assert body["events"]["deploy"] == 8
    got = {c["id"]: c for c in body["kill_criteria"]}
    k1 = got["active_builders_week6"]
    assert (k1["verdict"], k1["observed"], k1["small_sample"]) == ("met", 0.6, True)
    assert got["tools_side_by_side"]["small_sample"] is False
    assert [c["verdict"] for c in got.values()] == [
        "met",
        "missed",
        "met",
        "met",
        "needs_input",
        "needs_input",
    ]
    assert body["stop_rule"] == {"misses": 1, "open": 2, "decision": "undecided"}
    assert "small sample" in out


def test_criteria_before_they_are_due(
    pilot40: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    day21 = (DAY0 + timedelta(days=20)).isoformat()
    args = ("--org", pilot40, "--since", DAY0.isoformat(), "--as-of", day21)
    body = report_json(
        monkeypatch,
        capsys,
        dsns.app,
        *args,
        "--invited-builders",
        "40",
        "--approved-path",
        "yes",
        "--paid",
        "no",
    )
    got = {c["id"]: c for c in body["kill_criteria"]}
    assert [c["verdict"] for c in got.values()] == [
        "not_due",
        "not_due",
        "not_due",
        "met",
        "met",
        "not_due",
    ]
    # By day 21 no app has been redeployed from Lovable, so four tools are still running.
    assert got["tools_side_by_side"]["note"] == "claude-code, lovable, replit"
    assert body["stop_rule"] == {"misses": 0, "open": 4, "decision": "undecided"}
    assert body["events"]["deploy"] == 40


def test_the_report_reads_one_read_only_snapshot_and_writes_nothing(
    pilot5: str, dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, str] = {}
    real = rep.gather

    async def spy(conn: Any, org_id: str, window: rep.Window) -> rep.Facts:
        seen["read_only"] = (await conn.execute(text("show transaction_read_only"))).scalar_one()
        seen["isolation"] = (await conn.execute(text("show transaction_isolation"))).scalar_one()
        return await real(conn, org_id, window)

    def counts() -> tuple[int, int]:
        with psycopg.connect(dsns.app) as conn:
            bind_org_sync(conn, pilot5)
            row = conn.execute(
                "select (select count(*) from ssc.metrics_event), "
                "(select count(*) from ssc.audit_event)"
            ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1])

    before = counts()
    monkeypatch.setattr(rep, "gather", spy)
    report(monkeypatch, capsys, dsns.app, "--org", pilot5)
    assert seen == {"read_only": "on", "isolation": "repeatable read"}
    assert counts() == before


def test_an_org_with_no_events_reports_zeros_and_flags(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    org = asyncio.run(new_org(dsns.app)).org_id
    out = report(monkeypatch, capsys, dsns.app, "--org", org)
    for heading in (HEADINGS[0], HEADINGS[1], HEADINGS[3], HEADINGS[4]):
        assert under(out, heading) == "insufficient data (n=0, need 20)", heading
    assert under(out, HEADINGS[2]) == "none: no app_opened events"
    assert under(out, HEADINGS[5]) == "none: no deploy or share events"
    body = report_json(monkeypatch, capsys, dsns.app, "--org", org)
    assert set(body["events"].values()) == {0}
    assert body["window"]["elapsed_days"] in {1, 2}  # 2 only across midnight UTC
    assert body["metrics"]["source_tool_mix"] == {"n": 0, "tools": []}
    assert body["stop_rule"]["decision"] == "undecided"


def test_report_command_line_refusals(
    dsns: Dsns, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(rep.DSN_ENV, raising=False)
    with pytest.raises(SystemExit):
        rep.main(["--org", ORG_A])
    assert "SSC_DATABASE_DSN is not set" in capsys.readouterr().err
    monkeypatch.setenv(rep.DSN_ENV, dsns.app)
    org = asyncio.run(new_org(dsns.app))
    for argv, message in (
        (["--org", ORG_A], f"org {ORG_A} not found"),
        (["--org", ORG_A, "--since", "2026-07-02", "--as-of", "2026-07-01"], "after --as-of"),
        (["--org", ORG_A, "--invited-builders", "0"], "at least 1"),
        (["--org", org.org_id, "--as-of", "2026-01-01"], "after --as-of"),
        (["--org", ORG_A, "--paid", "maybe"], "yes or no"),
        (["--org", "acme"], "--org"),
    ):
        with pytest.raises(SystemExit):
            rep.main(argv)
        assert message in capsys.readouterr().err


# ── kill criteria edges, without a database ──────────────────────────────────


def facts(**changes: Any) -> rep.Facts:
    base = rep.Facts(
        events={},
        first_url_seconds=(),
        apps_per_builder=(),
        weekly_users=(),
        weekly_builders=(),
        app_usage=(0, 0),
        tool_deploys=(),
        week6_builders=0,
        day60_apps_per_builder=(),
        day60_app_usage=(0, 0),
        running_tools=(),
    )
    return rep.Facts(**{**{f: getattr(base, f) for f in rep.Facts.__slots__}, **changes})


WINDOW60 = rep.Window(DAY0, DAY0 + timedelta(days=59))


def verdicts(f: rep.Facts, pilot: rep.PilotInputs, window: rep.Window = WINDOW60) -> list[str]:
    return [c.verdict for c in rep.criteria(f, window, pilot)]


def test_nothing_recorded_is_not_a_pass() -> None:
    found = rep.criteria(facts(), WINDOW60, rep.PilotInputs(10, True, True))
    assert [c.verdict for c in found] == [
        "missed",
        "not_recorded",
        "not_recorded",
        "not_recorded",
        "met",
        "met",
    ]
    assert rep.stop_rule(found).decision == "undecided"


def test_more_active_than_invited_needs_input() -> None:
    (k1, *_) = rep.criteria(facts(week6_builders=12), WINDOW60, rep.PilotInputs(10))
    assert (k1.verdict, k1.observed) == ("needs_input", None)
    assert k1.note == "12 builders active in week 6, more than 10 invited"


def test_unknown_tools_do_not_count_and_one_tool_misses_at_day60() -> None:
    running = ((None, 3), ("other", 2), ("claude-code", 4))
    (*_, k4, _, _) = rep.criteria(facts(running_tools=running), WINDOW60, rep.PilotInputs())
    assert (k4.verdict, k4.observed, k4.n, k4.note) == ("missed", 1.0, 9, "claude-code")
    early = rep.Window(DAY0, DAY0 + timedelta(days=30))
    (*_, k4, _, _) = rep.criteria(facts(running_tools=running), early, rep.PilotInputs())
    assert k4.verdict == "not_due"


def test_the_stop_rule_continues_when_two_misses_are_out_of_reach() -> None:
    f = facts(
        week6_builders=5,
        day60_apps_per_builder=(2, 3, 2),
        day60_app_usage=(1, 3),
        running_tools=(("cursor", 1), ("lovable", 2)),
    )
    found = rep.criteria(f, WINDOW60, rep.PilotInputs(10, True, False))
    assert [c.verdict for c in found] == ["met", "met", "met", "met", "met", "missed"]
    assert rep.stop_rule(found) == rep.StopRule(misses=1, open=0)
    assert rep.stop_rule(found).decision == "continue"

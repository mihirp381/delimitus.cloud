"""Two orgs for the agent adversarial probes (T4): A, whose admin's agent attacks, and B, the
victim, served by the ``test_mcp`` rig."""

import asyncio
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import psycopg
from psycopg import sql
from ssc_testkit import ISSUER, KID, Dsns, SigningKey, make_org, mint, new_key
from test_mcp import CLIENT_ID, PlantedLogs, add_account, add_grant, env_id, rows

from ssc_conformance.adversarial import Ids
from ssc_contracts.audit import ActorKind
from ssc_contracts.ids import new_id
from ssc_control.api.settings import USER_AUDIENCE
from ssc_control.audit.chain import Actor
from ssc_control.db import CreatedOrg, bound_org, make_engine
from ssc_control.identity import sessions, tokens
from ssc_shared.logs import LogLine, LogPage, LogQuery

SSO = "conn_adversarial"
SUPABASE_KEY = "sb_secret_" + "AdvSupabase" + "0" * 20
SHAPED: dict[str, str] = {
    "supabase secret key": SUPABASE_KEY,
    "supabase service role jwt": "eyJ"
    + "hbGciOiJIUzI1NiJ9."
    + "eyJyb2xlIjoic2VydmljZV9yb2xlIn0"
    + ".adv-sb-sig-0001",
    "postgres url": "adv-db-pass-0001",
    "mysql url": "adv-db-pass-0002",
    "github classic pat": "ghp_" + ("AdvPat" + "0" * 40)[:36],
    "github fine-grained pat": "github_pat_11ADV" + "0" * 18 + "_" + "a" * 59,
    "aws access key id": "AKIAADVERSARIAL00001",
    "aws secret access key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYADVKEY0001",
    "jwt": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZHYtand0In0.adv-jwt-signature-000001",
}
LINES = (
    f"supabase admin client ready with {SHAPED['supabase secret key']}",
    f"SUPABASE_SERVICE_ROLE_KEY={SHAPED['supabase service role jwt']}",
    f"connecting to postgresql://app_user:{SHAPED['postgres url']}@db.example.internal:5432/app",
    f"connecting to mysql://root:{SHAPED['mysql url']}@10.0.0.5/app",
    f"cloning with {SHAPED['github classic pat']}",
    f"cloning with {SHAPED['github fine-grained pat']}",
    f"aws key {SHAPED['aws access key id']}",
    f"aws_secret_access_key={SHAPED['aws secret access key']}",
    f"session {SHAPED['jwt']}",
)
PLANTED_HOST = "db.adv-planted.internal"
PLANTED_DATABASE = "adv_planted_db"


class ShapedLogs(PlantedLogs):
    """An app that printed every credential shape the redaction should catch."""

    async def read(
        self, query: LogQuery, *, since_seconds: int, limit: int, caller: str
    ) -> LogPage:
        now = datetime.now(UTC)
        lines = tuple(LogLine(timestamp=now, severity="INFO", source="app", text=t) for t in LINES)
        return LogPage(lines=lines, cursor="0.0.1")


def add_release(dsn: str, org: str, app: str, number: int, user: str) -> str:
    rid = new_id("rel")
    d = "sha256:" + hashlib.sha256(rid.encode()).hexdigest()
    rows(
        dsn,
        org,
        "insert into ssc.release (id, org_id, app_id, number, image_digest, manifest_digest, "
        "source_digest, actor_kind, actor_id) values (%s, %s, %s, %s, %s, %s, %s, 'user', %s) "
        "returning id",
        (rid, org, app, number, d, d, d, user),
    )
    return rid


def fingerprint(superuser: str, orgs: Iterable[str]) -> dict[str, str]:
    """Every ``ssc`` table's rows of these orgs, hashed per table."""
    wanted = list(orgs)
    with psycopg.connect(superuser) as conn:
        tables = conn.execute(
            "select c.table_name from information_schema.columns c join information_schema.tables "
            "t using (table_schema, table_name) where c.table_schema = 'ssc' and c.column_name = "
            "'org_id' and t.table_type = 'BASE TABLE' order by 1"
        ).fetchall()
        out: dict[str, str] = {}
        for (table,) in tables:
            query = sql.SQL(
                "select md5(coalesce(string_agg(x::text, '|' order by x::text), '')) "
                "from ssc.{} x where org_id = any(%s)"
            ).format(sql.Identifier(table))
            row = conn.execute(query, (wanted,)).fetchone()
            assert row is not None
            out[str(table)] = str(row[0])
        return out


@dataclass(frozen=True)
class Login:
    sid: str
    access: str
    refresh: str


def login(
    dsns: Dsns, key: SigningKey, org: str, user: str, *, audience: str, agent: str | None
) -> Login:
    """A command-line session as the auth host opens it, its access token and refresh token."""
    signer = tokens.Signer(key.private_pem, KID, ISSUER)
    mcp = None if audience == USER_AUDIENCE else audience

    async def go() -> tuple[str, str]:
        engine = make_engine(dsns.app)
        try:
            async with bound_org(engine, org) as conn:
                sid = await sessions.open_session(
                    conn,
                    org,
                    user_id=user,
                    kind="cli",
                    connection_id=SSO,
                    actor=Actor(ActorKind.USER, user),
                    agent_client_id=agent,
                    token_audience=mcp,
                )
                return sid, await tokens.issue_refresh(conn, org, sid)
        finally:
            await engine.dispose()

    sid, refresh = asyncio.run(go())
    now = tokens.utcnow()
    access = signer.access_token(
        org_id=org,
        user_id=user,
        session_id=sid,
        audience=audience,
        now=now,
        agent_client_id=agent,
        expires_at=now + timedelta(hours=1),
    )
    return Login(sid, access, refresh)


def ci_token(dsns: Dsns, key: SigningKey, org: str, user: str) -> str:
    parent = login(dsns, key, org, user, audience=USER_AUDIENCE, agent=None)

    async def go() -> tuple[str, datetime]:
        engine = make_engine(dsns.app)
        try:
            async with bound_org(engine, org) as conn:
                opened = await sessions.open_ci_session(
                    conn,
                    org,
                    parent_id=parent.sid,
                    user_id=user,
                    label="adversarial",
                    days=1,
                    actor=Actor(ActorKind.USER, user),
                )
                assert opened is not None
                return opened
        finally:
            await engine.dispose()

    sid, expires = asyncio.run(go())
    signer = tokens.Signer(key.private_pem, KID, ISSUER)
    return signer.access_token(
        org_id=org,
        user_id=user,
        session_id=sid,
        audience=USER_AUDIENCE,
        now=tokens.utcnow(),
        expires_at=expires,
        scope="preview",
    )


def call(url: str, token: str, method: str, path: str, body: object = None) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": new_key()}
    return httpx2.request(method, f"{url}{path}", json=body, headers=headers, timeout=30)


def made(r: httpx2.Response) -> dict[str, Any]:
    assert r.is_success, r.text
    body: dict[str, Any] = r.json()
    return body


@dataclass(frozen=True)
class Victim:
    org: CreatedOrg
    human: str
    app: dict[str, Any]
    release: str
    operation: str
    bundle: dict[str, Any]
    digest: str
    approval: str
    connection: str
    secret: str

    def refs(self) -> set[str]:
        """What an attacker must never learn from an answer."""
        envs = {e["id"] for e in self.app["environments"]}
        return {
            self.org.org_id,
            self.org.admin_user_id,
            self.app["id"],
            self.release,
            self.operation,
            self.bundle["bundle_id"],
            self.approval,
            self.connection,
            self.secret,
            *envs,
        }


def victim(url: str, dsns: Dsns, key: SigningKey) -> Victim:
    org = make_org(dsns.app, "Victim org")
    human = mint(key, org=org.org_id, sub=org.admin_user_id)
    app = made(call(url, human, "POST", "/v1/apps", {"slug": "victim-ledger"}))
    release = add_release(dsns.app, org.org_id, app["id"], 1, org.admin_user_id)
    prod = env_id(app, "prod")
    op = made(
        call(
            url,
            human,
            "POST",
            f"/v1/apps/{app['id']}/environments/{prod}/deployments",
            {"release_id": release, "kind": "rollback"},
        )
    )
    digest = "sha256:" + hashlib.sha256(b"victim bundle").hexdigest()
    bundle = made(
        call(
            url,
            human,
            "POST",
            f"/v1/apps/{app['id']}/bundles",
            {"digest": digest, "size_bytes": 13},
        )
    )
    connection = "victim-warehouse"
    made(
        call(
            url,
            human,
            "POST",
            "/v1/connections",
            {
                "name": connection,
                "owner_user_id": org.admin_user_id,
                "classification": "internal",
                "host": "db.victim.internal",
                "database": "victim_db",
            },
        )
    )
    asked = made(
        call(
            url,
            human,
            "POST",
            "/v1/approvals",
            {"environment_id": prod, "kind": "connect_data_source", "subject_key": connection},
        )
    )
    secret = "VICTIM_API_TOKEN"
    made(
        call(
            url,
            human,
            "PUT",
            f"/v1/apps/{app['id']}/environments/{env_id(app, 'preview')}/secrets/{secret}",
            {"version": "1"},
        )
    )
    return Victim(
        org,
        human,
        app,
        release,
        op["operation_id"],
        bundle,
        digest,
        asked["id"],
        connection,
        secret,
    )


@dataclass(frozen=True)
class Attacker:
    org: CreatedOrg
    human: str
    app: dict[str, Any]
    releases: tuple[str, str]
    api: Login
    api_other: Login
    mcp: Login
    mcp_other: Login
    ci: str
    operator_agent: str
    own_approval: str
    others_approval: str
    connection: str

    @property
    def ids(self) -> Ids:
        return Ids(
            self.app["id"],
            self.app["slug"],
            env_id(self.app, "prod"),
            env_id(self.app, "preview"),
            self.org.admin_user_id,
            self.own_approval,
        )


def attacker(url: str, dsns: Dsns, key: SigningKey) -> Attacker:
    org = make_org(dsns.app, "Attacker org")
    admin = org.admin_user_id
    human = mint(key, org=org.org_id, sub=admin)
    app = made(call(url, human, "POST", "/v1/apps", {"slug": "attacker-app"}))
    releases = (
        add_release(dsns.app, org.org_id, app["id"], 1, admin),
        add_release(dsns.app, org.org_id, app["id"], 2, admin),
    )

    def agent_login(audience: str) -> Login:
        return login(dsns, key, org.org_id, admin, audience=audience, agent=CLIENT_ID)

    api, api_other = agent_login(USER_AUDIENCE), agent_login(USER_AUDIENCE)
    mcp, mcp_other = agent_login(f"{url}/mcp"), agent_login(f"{url}/mcp")
    preview = env_id(app, "preview")
    ask = {"environment_id": preview, "kind": "connect_data_source", "subject_key": "own-ask"}
    own = made(call(url, api.access, "POST", "/v1/approvals", ask))
    member = add_account(dsns.app, org.org_id, "member")
    add_grant(dsns.app, org.org_id, preview, member, "builder", admin)
    theirs = made(
        call(
            url,
            mint(key, org=org.org_id, sub=member),
            "POST",
            "/v1/approvals",
            {**ask, "subject_key": "their-ask"},
        )
    )
    connection = "attacker-warehouse"
    made(
        call(
            url,
            human,
            "POST",
            "/v1/connections",
            {
                "name": connection,
                "owner_user_id": admin,
                "classification": "internal",
                "host": PLANTED_HOST,
                "database": PLANTED_DATABASE,
            },
        )
    )
    prod = env_id(app, "prod")
    made(
        call(
            url,
            human,
            "PUT",
            f"/v1/apps/{app['id']}/environments/{prod}/connections/{connection}",
            {},
        )
    )
    operator_agent = mint(key, org=org.org_id, sub="op_staff", kind="operator", agent=True)
    ci = ci_token(dsns, key, org.org_id, admin)
    return Attacker(
        org,
        human,
        made(call(url, human, "GET", f"/v1/apps/{app['id']}")),
        releases,
        api,
        api_other,
        mcp,
        mcp_other,
        ci,
        operator_agent,
        own["id"],
        theirs["id"],
        connection,
    )

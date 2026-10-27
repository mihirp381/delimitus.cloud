"""Decision 029 amendment of 2026-10-26: ``python -m ssc_control.identity invite``, the
operator's pilot invite codes, through ``main(argv)`` against postgres:18."""

import hashlib
import secrets

import psycopg
import pytest
import uvicorn
from ssc_testkit import Dsns, make_org

from ssc_contracts.ids import new_id
from ssc_control.db import bind_org_sync
from ssc_control.identity import __main__ as cli
from ssc_control.identity import invites, tokens
from ssc_control.identity.settings import AuthSettings

OP = "op_dana"

type Out = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True)
def database(dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)


def events(dsn: str, org_id: str) -> list[tuple[object, ...]]:
    with psycopg.connect(dsn) as conn, conn.transaction():
        bind_org_sync(conn, org_id)
        return conn.execute(
            "select action, actor_kind, actor_id, target_id, after from ssc.audit_event "
            "where org_id = %s and target_kind = 'pilot_invite' order by seq",
            (org_id,),
        ).fetchall()


def run(argv: list[str], capsys: Out) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def create(org_id: str, *extra: str) -> list[str]:
    return ["invite", "create", "--org", org_id, "--operator", OP, *extra]


def test_create_prints_the_code_once_and_keeps_its_digest(dsns: Dsns, capsys: Out) -> None:
    org = make_org(dsns.app).org_id
    code, out, err = run(
        create(org, "--expires-in", "14d", "--max-uses", "3", "--note", "acme"), capsys
    )
    assert code == 0, err
    (secret,) = out.splitlines()
    assert invites.CODE.fullmatch(secret)
    assert secret not in err and " active uses=0/3 " in err and err.rstrip().endswith("acme")
    with psycopg.connect(dsns.app) as conn:
        (stored,) = conn.execute(
            "select code_sha256, org_id, max_uses, uses from ssc.pilot_invite where org_id = %s",
            (org,),
        ).fetchall()
    assert stored == (hashlib.sha256(secret.encode()).digest(), org, 3, 0)
    ((action, kind, actor, target, after),) = events(dsns.app, org)
    assert (action, kind, actor) == ("invite.created", "operator", OP)
    assert target.startswith("pinv_") and set(after) == {"expires_at", "max_uses"}


def test_list_and_revoke(dsns: Dsns, capsys: Out) -> None:
    org = make_org(dsns.app).org_id
    assert run(create(org, "--expires-in", "12h", "--max-uses", "1"), capsys)[0] == 0
    code, out, _ = run(["invite", "list", "--org", org], capsys)
    (line,) = out.splitlines()
    assert code == 0 and " active uses=0/1 " in line
    invite_id = line.split()[0]
    code, out, err = run(["invite", "revoke", invite_id, "--operator", OP], capsys)
    assert code == 0, err
    assert out.startswith(f"{invite_id} revoked ")
    assert " revoked " in run(["invite", "list", "--org", org], capsys)[1]
    assert [e[0] for e in events(dsns.app, org)] == ["invite.created", "invite.revoked"]
    code, out, err = run(["invite", "revoke", invite_id, "--operator", OP], capsys)
    assert (code, out) == (1, "") and "already revoked" in err
    assert len(events(dsns.app, org)) == 2


def test_refusals_write_nothing(dsns: Dsns, capsys: Out) -> None:
    org = make_org(dsns.app).org_id
    for bad in (
        ["--expires-in", "91d"],
        ["--expires-in", "0d"],
        ["--expires-in", "2w"],
        ["--max-uses", "0"],
        ["--max-uses", "1001"],
        ["--max-uses", "-1"],
    ):
        with pytest.raises(SystemExit):
            cli.main(create(org, *(["--expires-in", "1d", "--max-uses", "1"] + bad)))
    with pytest.raises(SystemExit):
        cli.main(
            [
                "invite",
                "create",
                "--org",
                org,
                "--operator",
                "dana",
                "--expires-in",
                "1d",
                "--max-uses",
                "1",
            ]
        )
    capsys.readouterr()
    code, out, err = run(create(new_id("org"), "--expires-in", "1d", "--max-uses", "1"), capsys)
    assert (code, out) == (1, "") and err.startswith("not done: no org")
    code, out, err = run(["invite", "revoke", new_id("pinv"), "--operator", OP], capsys)
    assert (code, out) == (1, "") and "unknown" in err
    assert events(dsns.app, org) == []
    with psycopg.connect(dsns.app) as conn:
        assert conn.execute(
            "select count(*) from ssc.pilot_invite where org_id = %s", (org,)
        ).fetchone() == (0,)


def test_the_auth_host_writes_no_access_log(dsns: Dsns, monkeypatch: pytest.MonkeyPatch) -> None:
    s = AuthSettings(
        database_dsn=dsns.app,
        workos_api_key="sk_test_fake",
        workos_client_id="client_fake",
        signing_pem=tokens.new_signing_pem(),
        signing_kid="k1",
        state_key=secrets.token_bytes(32),
        environment="test",
        dev_cell_secret=secrets.token_hex(16),
    )
    monkeypatch.setattr(AuthSettings, "from_env", staticmethod(lambda: s))
    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda _app, **kw: seen.update(kw))
    cli.serve("127.0.0.1", 0)
    assert seen["access_log"] is False

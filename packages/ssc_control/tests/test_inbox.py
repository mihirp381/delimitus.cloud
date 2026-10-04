"""SSC-049: the approvals inbox, its mail and the apply-on-approval step, against postgres:18.

Ticket "done when" checks:
  * a share request beyond an audience ceiling reaches the connection owner's inbox within a
    minute and approving it applies the grant
        -> test_an_exceed_request_reaches_the_owner_and_approving_it_applies_the_grant
  * an agent session cannot approve        -> test_an_agent_session_cannot_approve
  * a rejected request tells the requester why
        -> test_a_rejected_request_tells_the_requester_why
Plus: who sees which request, a change needing two approvals, approvals that no longer fit,
withdrawing a request, retries, the reminder and the digest, the SMTP rules, the settings and
migration 0032.
"""

from __future__ import annotations

import asyncio
import smtplib
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from typing import Any, LiteralString

import psycopg
import pytest
import test_approvals
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from ssc_testkit import Dsns, SigningKey, assert_problem, auth, mint, new_key
from test_approvals import (
    Tokens,
    World,
    approvals_of,
    ask_api,
    current,
    events_of,
    keyed,
    post,
    put,
)
from test_connections import (
    BUILDER_ORG,
    ORG_GRANT,
    add_group,
    ask_exceed,
    ceiling_doc,
    finance,
    link,
    prod_grants,
)
from test_worker import periods

from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_control.db import MIGRATE_ROLE, bind_org_sync, downgrade, make_engine, upgrade
from ssc_control.notifications import delivery
from ssc_control.notifications.jobs import blueprint
from ssc_control.notifications.mailer import (
    LogMailer,
    Mail,
    Security,
    SmtpConfig,
    SmtpMailer,
)
from ssc_control.worker import (
    CompositionError,
    build_app,
    console_url_from_env,
    mailer_from_env,
    refuse_fakes,
)
from ssc_control.worker_ports import Ports

client = test_approvals.client
world = test_approvals.world
tokens = test_approvals.tokens

CONSOLE = "https://console.example.test"
PASSWORD = "not-a-real-password"
YES = {"outcome": "approved", "reason": "Fine by me."}


def outbox(dsn: str, w: World, kind: str | None = None) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        bind_org_sync(conn, w.org)
        found = conn.execute(
            "select * from ssc.notification_outbox order by created_at, id"
        ).fetchall()
    return [r for r in found if kind in (None, r["kind"])]


def get(client: TestClient, path: str, token: str) -> Any:
    return client.get(path, headers=auth(token))


def run[T](dsn: str, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = make_engine(dsn)
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def flush(dsns: Dsns, w: World, sender: Any) -> int:
    return run(
        dsns.app,
        lambda e: delivery.flush(e, sender, org_id=w.org, console_url=CONSOLE),
    )


def set_email(dsn: str, w: World, user: str, address: str) -> None:
    with psycopg.connect(dsn) as conn:
        bind_org_sync(conn, w.org)
        conn.execute(
            "update ssc.user_account set email = %s where id = %s",
            (address, user),
        )


def mail_for(w: World, dsns: Dsns) -> dict[str, str]:
    """Distinct addresses so a test can tell the recipients apart; the user for each."""
    names = {
        w.admin: "admin",
        w.approver: "approver",
        w.member: "member",
        w.builder: "builder",
    }
    for user, name in names.items():
        set_email(dsns.app, w, user, f"{name}@example.test")
    return {f"{name}@example.test": name for name in names.values()}


def decide(
    client: TestClient, approval_id: str, token: str, body: dict[str, Any] | None = None
) -> Any:
    return post(client, f"/v1/approvals/{approval_id}/decide", token, body or YES)


def inbox_ids(client: TestClient, token: str) -> list[str]:
    r = client.get("/v1/approvals?inbox=true", headers=auth(token))
    assert r.status_code == 200, r.text
    return [a["id"] for a in r.json()["approvals"]]


def notify_jobs(dsns: Dsns, org: str) -> int:
    with psycopg.connect(dsns.superuser) as conn:
        row = conn.execute(
            "select count(*) from procrastinate.procrastinate_jobs "
            "where task_name = 'notify:send' and args->>'org_id' = %s",
            (org,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def shared_finance(
    client: TestClient, w: World, tokens: Tokens, dsns: Dsns
) -> list[dict[str, Any]]:
    """A Finance connection owned by the member, ceilinged to one group, used by prod."""
    group = add_group(dsns.app, w, [w.builder])
    finance(client, w, tokens, ceiling_doc(("group", group)))
    assert link(client, w, w.prod, tokens.admin).status_code == 200
    before, _ = prod_grants(client, w, tokens)
    return before


def test_an_exceed_request_reaches_the_owner_and_approving_it_applies_the_grant(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    addresses = mail_for(world, dsns)
    before = shared_finance(client, world, tokens, dsns)
    org_wide = [*before, ORG_GRANT]
    apr = ask_exceed(client, tokens.admin, world, org_wide)
    assert notify_jobs(dsns, world.org) == 1
    queued = outbox(dsns.app, world, "arrival")
    assert {r["user_id"] for r in queued} == {world.approver, world.member}
    assert all(r["state"] == "pending" and r["approval_id"] == apr for r in queued)
    sender = LogMailer()
    assert flush(dsns, world, sender) == 2
    sent = {addresses[m.to]: m for m in sender.sent}
    assert set(sent) == {"approver", "member"}
    body = sent["member"].body
    assert f"{CONSOLE}/approvals/{apr}" in body
    assert "Ada Admin" in body and "ledger" in body and "finance" in body
    assert "usr_" not in body and "grp_" not in body
    assert inbox_ids(client, tokens.member) == [apr]
    assert inbox_ids(client, tokens.approver) == [apr]
    assert inbox_ids(client, tokens.admin) == []
    seen = client.get(f"/v1/approvals/{apr}", headers=auth(tokens.member)).json()
    assert seen["can_decide"] is True and seen["can_cancel"] is False
    assert (seen["app"], seen["environment"]) == ("ledger", "prod")
    assert seen["requested_by_name"] == "Ada Admin"
    assert seen["connection"]["name"] == "finance"
    assert seen["connection"]["ceiling_audience"] == "subjects"
    assert [g["subject_kind"] for g in seen["grant_diff"]["added"]] == ["org"]
    assert seen["grant_diff"]["removed"] == []
    version = current(client, world, world.prod, tokens.admin)["grants_version"]
    decided = decide(client, apr, tokens.member)
    assert decided.status_code == 200, decided.text
    assert (decided.json()["applied"], decided.json()["decision_channel"]) == ("applied", "console")
    after = current(client, world, world.prod, tokens.admin)
    assert after["grants_version"] == version + 1
    assert any(g["subject_kind"] == "org" for g in after["grants"])
    (added,) = [
        e
        for e in events_of(dsns.app, world.org, AuditAction.GRANT_ADDED)
        if e["after"]["environment_id"] == world.prod and e["after"]["subject_kind"] == "org"
    ]
    assert added["policy_decision_id"] == decided.json()["policy_decision_id"]
    (event,) = events_of(dsns.app, world.org, AuditAction.APPROVAL_DECIDED)
    assert (event["actor_id"], event["policy_decision_id"]) == (
        world.member,
        decided.json()["policy_decision_id"],
    )
    assert inbox_ids(client, tokens.approver) == []
    assert flush(dsns, world, sender) == 1
    assert addresses[sender.sent[-1].to] == "admin"


def test_an_agent_session_cannot_approve(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns, signing_key: SigningKey
) -> None:
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    agent = mint(
        signing_key,
        org=world.org,
        sub=world.approver,
        jti=f"cred_{new_key()[:16]}",
        agent=True,
        client_id="agent-x",
    )
    assert_problem(decide(client, apr, agent), ErrorCode.AGENT_SESSION_REFUSED)
    assert inbox_ids(client, agent) == []
    assert approvals_of(dsns.app, world.org)[0]["state"] == "pending"
    assert events_of(dsns.app, world.org, AuditAction.APPROVAL_DECIDED) == []
    assert inbox_ids(client, tokens.approver) == [apr]


def test_you_cannot_decide_your_own_request_or_without_a_reason(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    apr = ask_api(client, tokens.admin, world.prod).json()["id"]
    assert_problem(decide(client, apr, tokens.admin), ErrorCode.SELF_APPROVAL_REFUSED)
    assert_problem(
        decide(client, apr, tokens.approver, {"outcome": "approved"}), ErrorCode.VALIDATION_FAILED
    )
    assert_problem(
        decide(client, apr, tokens.approver, {"outcome": "approved", "reason": ""}),
        ErrorCode.VALIDATION_FAILED,
    )
    assert_problem(decide(client, apr, tokens.operator), ErrorCode.FORBIDDEN)
    assert decide(client, apr, tokens.approver).status_code == 200
    assert_problem(decide(client, apr, tokens.approver), ErrorCode.APPROVAL_NOT_PENDING)


def test_a_rejected_request_tells_the_requester_why(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    addresses = mail_for(world, dsns)
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    denied = decide(
        client, apr, tokens.approver, {"outcome": "denied", "reason": "Finance says not yet."}
    )
    assert denied.status_code == 200, denied.text
    assert (denied.json()["state"], denied.json()["applied"]) == ("denied", "not_applicable")
    sender = LogMailer()
    flush(dsns, world, sender)
    (told,) = [m for m in sender.sent if addresses[m.to] == "builder"]
    assert "rejected" in told.subject
    assert "Finance says not yet." in told.body
    assert f"{CONSOLE}/approvals/{apr}" in told.body
    assert not any(addresses[m.to] != "builder" for m in sender.sent)
    got = client.get(f"/v1/approvals/{apr}", headers=auth(tokens.builder)).json()
    assert (got["state"], got["decision_reason"]) == ("denied", "Finance says not yet.")


def test_the_owner_sees_only_the_exceed_request_on_their_connection(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = shared_finance(client, world, tokens, dsns)
    exceed = ask_exceed(client, tokens.admin, world, [*before, ORG_GRANT])
    other = ask_api(client, tokens.admin, world.prod).json()["id"]
    listed = client.get("/v1/approvals", headers=auth(tokens.member)).json()["approvals"]
    assert [a["id"] for a in listed] == [exceed]
    assert inbox_ids(client, tokens.member) == [exceed]
    assert client.get(f"/v1/approvals/{exceed}", headers=auth(tokens.member)).status_code == 200
    assert_problem(get(client, f"/v1/approvals/{other}", tokens.member), ErrorCode.NOT_FOUND)
    assert_problem(decide(client, other, tokens.member), ErrorCode.NOT_FOUND)
    assert_problem(
        post(client, f"/v1/approvals/{other}/cancel", tokens.member, {}), ErrorCode.NOT_FOUND
    )
    assert client.get("/v1/approvals", headers=auth(tokens.builder)).json()["approvals"] == []
    assert inbox_ids(client, tokens.builder) == []
    assert sorted(inbox_ids(client, tokens.approver)) == sorted([exceed, other])
    assert inbox_ids(client, tokens.admin) == []


def test_a_change_needing_two_approvals_applies_with_the_last_one(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = shared_finance(client, world, tokens, dsns)
    org_wide = [*before, ORG_GRANT]
    pending = put(client, world, world.prod, tokens.admin_agent, org_wide, 1)
    assert pending.status_code == 202, pending.text
    ids = pending.json()["approval_ids"]
    assert len(ids) >= 2
    for apr in ids[:-1]:
        first = decide(client, apr, tokens.approver)
        assert (first.status_code, first.json()["applied"]) == (200, "waiting")
        assert current(client, world, world.prod, tokens.admin)["grants_version"] == 1
    last = decide(client, ids[-1], tokens.approver)
    assert (last.status_code, last.json()["applied"]) == (200, "applied")
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == 2
    added = [
        e
        for e in events_of(dsns.app, world.org, AuditAction.GRANT_ADDED)
        if e["after"]["environment_id"] == world.prod
    ]
    assert [e["policy_decision_id"] for e in added] != [None]
    assert all(e["actor_id"] == world.approver for e in added)


def test_an_approval_that_no_longer_fits_stays_approved_and_applies_nothing(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = keyed(current(client, world, world.prod, tokens.admin)["grants"])
    wider = [*before, {"role": "user", "subject_kind": "user", "subject_id": world.member}]
    pending = put(client, world, world.prod, tokens.admin_agent, wider, 1)
    (apr,) = pending.json()["approval_ids"]
    assert put(client, world, world.prod, tokens.admin, [], 1).status_code == 200
    stale = decide(client, apr, tokens.approver)
    assert stale.status_code == 200, stale.text
    assert (stale.json()["state"], stale.json()["applied"]) == ("approved", "not_applied")
    assert stale.json()["applied_reason"] == "stale"
    assert current(client, world, world.prod, tokens.admin)["grants_version"] == 2
    assert current(client, world, world.prod, tokens.admin)["grants"] == []
    assert events_of(dsns.app, world.org, AuditAction.GRANT_ADDED) == []


def test_a_frozen_app_is_not_changed_by_an_approval(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    before = keyed(current(client, world, world.prod, tokens.admin)["grants"])
    wider = [*before, {"role": "user", "subject_kind": "user", "subject_id": world.member}]
    (apr,) = put(client, world, world.prod, tokens.admin_agent, wider, 1).json()["approval_ids"]
    with psycopg.connect(dsns.superuser) as conn:
        conn.execute("update ssc.app set status = 'quarantined' where id = %s", (world.app,))
    frozen = decide(client, apr, tokens.approver)
    assert (frozen.json()["applied"], frozen.json()["applied_reason"]) == (
        "not_applied",
        "app_not_active",
    )
    assert keyed(current(client, world, world.prod, tokens.admin)["grants"]) == before


def test_connecting_a_data_source_applies_nothing(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    done = decide(client, apr, tokens.approver)
    assert (done.json()["state"], done.json()["applied"]) == ("approved", "not_applicable")
    assert done.json()["applied_reason"] is None


def test_the_requester_may_withdraw_their_own_pending_request(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    assert_problem(
        post(client, f"/v1/approvals/{apr}/cancel", tokens.approver, {}), ErrorCode.FORBIDDEN
    )
    assert_problem(
        post(client, f"/v1/approvals/{apr}/cancel", tokens.operator, {}), ErrorCode.FORBIDDEN
    )
    cancelled = post(
        client, f"/v1/approvals/{apr}/cancel", tokens.builder, {"reason": "Not needed."}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert (cancelled.json()["state"], cancelled.json()["decision_reason"]) == (
        "cancelled",
        "Not needed.",
    )
    (event,) = events_of(dsns.app, world.org, AuditAction.APPROVAL_CANCELLED)
    assert (event["actor_id"], event["target_id"]) == (world.builder, apr)
    assert_problem(
        post(client, f"/v1/approvals/{apr}/cancel", tokens.builder, {}),
        ErrorCode.APPROVAL_NOT_PENDING,
    )
    assert_problem(decide(client, apr, tokens.approver), ErrorCode.APPROVAL_NOT_PENDING)
    assert inbox_ids(client, tokens.approver) == []
    sender = LogMailer()
    assert flush(dsns, world, sender) == 0
    assert outbox(dsns.app, world) == []
    again = ask_api(client, tokens.builder, world.prod).json()["id"]
    assert again != apr


def test_an_agent_may_withdraw_its_own_request_but_gets_an_empty_inbox(
    client: TestClient, world: World, tokens: Tokens
) -> None:
    apr = ask_api(client, tokens.admin_agent, world.prod).json()["id"]
    assert inbox_ids(client, tokens.admin_agent) == []
    withdrawn = post(client, f"/v1/approvals/{apr}/cancel", tokens.admin_agent, {})
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["decision_reason"] == "Withdrawn by the requester."


class Down:
    def __init__(self) -> None:
        self.calls = 0

    async def send(self, mail: Mail) -> None:
        self.calls += 1
        raise OSError("connection refused")


def test_mail_that_fails_is_retried_with_backoff_then_marked_failed(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    apr = ask_api(client, tokens.builder, world.prod).json()["id"]
    down = Down()
    assert flush(dsns, world, down) == 0
    assert down.calls == 2
    assert {(r["state"], r["attempts"]) for r in outbox(dsns.app, world)} == {("pending", 1)}
    assert flush(dsns, world, down) == 0
    assert down.calls == 2
    for _ in range(4):
        with psycopg.connect(dsns.app) as conn:
            bind_org_sync(conn, world.org)
            conn.execute("update ssc.notification_outbox set next_attempt_at = now()")
        flush(dsns, world, down)
    assert {(r["state"], r["attempts"]) for r in outbox(dsns.app, world)} == {("failed", 5)}
    assert decide(client, apr, tokens.approver).status_code == 200
    assert flush(dsns, world, LogMailer()) == 1


def test_a_request_still_pending_after_three_days_is_reminded_once(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    addresses = mail_for(world, dsns)
    ask_api(client, tokens.builder, world.prod)
    sender = LogMailer()
    flush(dsns, world, sender)
    assert run(dsns.app, lambda e: delivery.remind(e, org_id=world.org)) == 0
    with psycopg.connect(dsns.app) as conn:
        bind_org_sync(conn, world.org)
        conn.execute("update ssc.approval_request set created_at = now() - interval '73 hours'")
    assert run(dsns.app, lambda e: delivery.remind(e, org_id=world.org)) == 1
    assert run(dsns.app, lambda e: delivery.remind(e, org_id=world.org)) == 0
    sender.sent.clear()
    assert flush(dsns, world, sender) == 2
    assert {addresses[m.to] for m in sender.sent} == {"admin", "approver"}
    assert all(m.subject.startswith("Reminder: ") for m in sender.sent)


def test_the_daily_digest_lists_what_each_approver_can_decide(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    addresses = mail_for(world, dsns)
    day = date(2026, 10, 4)
    assert run(dsns.app, lambda e: delivery.digest(e, org_id=world.org, day=day)) == 0
    before = shared_finance(client, world, tokens, dsns)
    ask_exceed(client, tokens.admin, world, [*before, ORG_GRANT])
    ask_api(client, tokens.builder, world.prod)
    sender = LogMailer()
    flush(dsns, world, sender)
    sender.sent.clear()
    assert run(dsns.app, lambda e: delivery.digest(e, org_id=world.org, day=day)) == 3
    assert run(dsns.app, lambda e: delivery.digest(e, org_id=world.org, day=day)) == 0
    assert flush(dsns, world, sender) == 3
    counts = {addresses[m.to]: m.subject for m in sender.sent}
    assert counts == {
        "admin": "1 approval request(s) waiting for you",
        "approver": "2 approval request(s) waiting for you",
        "member": "1 approval request(s) waiting for you",
    }
    tomorrow = date(2026, 10, 5)
    assert run(dsns.app, lambda e: delivery.digest(e, org_id=world.org, day=tomorrow)) == 3


def test_the_outbox_keeps_no_address_and_no_body(dsns: Dsns) -> None:
    with psycopg.connect(dsns.app) as conn:
        columns = {
            r[0]
            for r in conn.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'ssc' and table_name = 'notification_outbox'"
            )
        }
    assert columns == {
        "id",
        "org_id",
        "user_id",
        "kind",
        "approval_id",
        "dedupe_key",
        "state",
        "attempts",
        "next_attempt_at",
        "created_at",
        "sent_at",
    }


def test_another_orgs_mail_is_invisible(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    ask_api(client, tokens.builder, world.prod)
    other = asyncio.run(test_approvals.make_world(dsns.app, "Other"))
    assert outbox(dsns.app, other) == []
    assert flush(dsns, other, LogMailer()) == 0
    assert len(outbox(dsns.app, world)) == 2


class FakeSmtp:
    log: list[tuple[str, Any]] = []
    offers_starttls = True

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.log.append(("connect", (host, port, type(self).__name__)))

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.append(("quit", None))

    def starttls(self, *, context: Any) -> None:
        if not self.offers_starttls:
            raise smtplib.SMTPNotSupportedError("no STARTTLS")
        assert context.verify_mode.name == "CERT_REQUIRED"
        self.log.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user))

    def send_message(self, message: Any) -> None:
        self.log.append(("send", (message["To"], message["Subject"])))


class FakeSmtpSsl(FakeSmtp):
    pass


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSmtp]]:
    FakeSmtp.log = []
    FakeSmtp.offers_starttls = True
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)
    yield FakeSmtp


def smtp_config(security: Security) -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example.test",
        port=587,
        security=security,
        username="mailer",
        password=PASSWORD,
        sender="ssc@example.test",
    )


def test_smtp_upgrades_to_tls_before_it_logs_in(smtp: type[FakeSmtp]) -> None:
    mail = Mail(to="a@example.test", subject="Hello", body="Body")
    asyncio.run(SmtpMailer(smtp_config("starttls")).send(mail))
    assert [step for step, _ in smtp.log] == ["connect", "starttls", "login", "send", "quit"]
    assert smtp.log[-2][1] == ("a@example.test", "Hello")


def test_smtp_with_implicit_tls_never_speaks_plaintext(smtp: type[FakeSmtp]) -> None:
    mail = Mail(to="a@example.test", subject="Hello", body="Body")
    asyncio.run(SmtpMailer(smtp_config("tls")).send(mail))
    assert smtp.log[0] == ("connect", ("smtp.example.test", 587, "FakeSmtpSsl"))
    assert [step for step, _ in smtp.log][1:] == ["login", "send", "quit"]


def test_a_server_without_starttls_is_never_sent_a_password(smtp: type[FakeSmtp]) -> None:
    smtp.offers_starttls = False
    mail = Mail(to="a@example.test", subject="Hello", body="Body")
    with pytest.raises(smtplib.SMTPNotSupportedError):
        asyncio.run(SmtpMailer(smtp_config("starttls")).send(mail))
    assert "login" not in [step for step, _ in smtp.log]
    assert "send" not in [step for step, _ in smtp.log]


def test_the_config_never_shows_the_password() -> None:
    assert PASSWORD not in repr(smtp_config("tls"))


SMTP_ENV = {
    "SSC_MAIL_TRANSPORT": "smtp",
    "SSC_SMTP_HOST": "smtp.example.test",
    "SSC_SMTP_USER": "mailer",
    "SSC_SMTP_PASSWORD": PASSWORD,
    "SSC_MAIL_FROM": "ssc@example.test",
}


def test_the_mail_settings() -> None:
    assert isinstance(mailer_from_env({"SSC_ENV": "dev"}, ""), LogMailer)
    assert mailer_from_env({}, "") is None
    assert isinstance(mailer_from_env({"SSC_MAIL_TRANSPORT": "log"}, ""), LogMailer)
    assert isinstance(mailer_from_env(SMTP_ENV, CONSOLE), SmtpMailer)
    assert isinstance(mailer_from_env({**SMTP_ENV, "SSC_SMTP_TLS": "tls"}, CONSOLE), SmtpMailer)
    for broken in (
        {**SMTP_ENV, "SSC_SMTP_PASSWORD": ""},
        {**SMTP_ENV, "SSC_SMTP_TLS": "none"},
        {**SMTP_ENV, "SSC_SMTP_PORT": "many"},
        {"SSC_MAIL_TRANSPORT": "pigeon"},
    ):
        with pytest.raises(CompositionError):
            mailer_from_env(broken, CONSOLE)
    with pytest.raises(CompositionError, match="SSC_CONSOLE_URL"):
        mailer_from_env(SMTP_ENV, "")
    assert console_url_from_env({"SSC_ENV": "dev"}).startswith("http://")
    assert console_url_from_env({}) == ""
    assert console_url_from_env({"SSC_CONSOLE_URL": f"{CONSOLE}/"}) == CONSOLE
    with pytest.raises(CompositionError):
        console_url_from_env({"SSC_CONSOLE_URL": "http://console.example.test"})
    assert console_url_from_env({"SSC_ENV": "test", "SSC_CONSOLE_URL": "http://x.test"})


def test_the_log_mailer_is_refused_outside_dev_and_test() -> None:
    engine = make_engine("postgresql://ssc_app@localhost/ssc")
    ports = Ports(engine=engine, mailer=LogMailer())
    with pytest.raises(CompositionError, match="mailer"):
        refuse_fakes(ports, {"SSC_ENV": "prod"})
    refuse_fakes(ports, {"SSC_ENV": "test"})
    refuse_fakes(Ports(engine=engine, mailer=SmtpMailer(smtp_config("tls"))), {"SSC_ENV": "prod"})


def test_the_worker_registers_the_mail_tasks_on_a_minute_and_a_daily_beat() -> None:
    app = build_app("postgresql://ssc_app@localhost/ssc")
    assert {"notify:send", "notify:tick", "notify:digest"} <= set(app.tasks)
    assert periods(app, "notify:tick") == {60.0}
    assert periods(app, "notify:digest") == {86400.0}
    assert blueprint() is not blueprint()


def test_0032_adds_the_outbox_and_downgrade_removes_it(dsns: Dsns) -> None:
    with psycopg.connect(dsns.superuser, autocommit=True) as conn:
        conn.execute(f"create database inbox32 owner {MIGRATE_ROLE}")
    dsn = make_url(dsns.migrate).set(database="inbox32").render_as_string(hide_password=False)

    def exists(statement: LiteralString) -> bool:
        with psycopg.connect(dsn) as conn:
            row = conn.execute(statement).fetchone()
        return row == (1,)

    outbox_table = (
        "select count(*) from pg_class where oid = to_regclass('ssc.notification_outbox')"
    )
    reminded = (
        "select count(*) from information_schema.columns where table_schema = 'ssc' "
        "and table_name = 'approval_request' and column_name = 'reminded_at'"
    )
    upgrade(dsn, "0032_notifications")
    assert exists(outbox_table) and exists(reminded)
    downgrade(dsn, "0031_connections")
    assert not exists(outbox_table) and not exists(reminded)
    with psycopg.connect(dsn) as conn:
        (check,) = conn.execute(
            "select pg_get_constraintdef(oid) from pg_constraint "
            "where conname = 'approval_request_decision_channel_check'"
        ).fetchone() or ("",)
    assert "cli" not in check and "console" in check
    upgrade(dsn)
    assert exists(outbox_table)


def test_a_builder_group_grant_is_a_normal_inbox_diff_name(
    client: TestClient, world: World, tokens: Tokens, dsns: Dsns
) -> None:
    group = add_group(dsns.app, world, [world.builder])
    before = keyed(current(client, world, world.prod, tokens.admin)["grants"])
    wider = [*before, {"role": "user", "subject_kind": "group", "subject_id": group}, BUILDER_ORG]
    (apr, *_) = put(client, world, world.prod, tokens.admin_agent, wider, 1).json()["approval_ids"]
    detail = client.get(f"/v1/approvals/{apr}", headers=auth(tokens.approver)).json()
    named = [g for g in detail["grant_diff"]["added"] if g["subject_kind"] == "group"]
    assert [g["subject_name"] for g in named] == ["Finance"]

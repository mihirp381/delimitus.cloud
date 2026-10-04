"""``ssc approvals``: the inbox, one request, and approve or reject with a reason (SSC-049)."""

import json

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import ApprovalDecisionResult, ApprovalShowResult, ApprovalsResult

APR = "apr_aaaaaaaaaaaaaaaaaaaa"
OTHER = "apr_bbbbbbbbbbbbbbbbbbbb"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
AT = "2026-09-29T00:00:00Z"


def _approval(approval_id: str = APR, **changes: object) -> dict[str, object]:
    return {
        "id": approval_id,
        "app_id": "app_aaaaaaaaaaaaaaaaaaaa",
        "app": "ledger",
        "environment_id": "env_prodprodprodprodprod",
        "environment": "prod",
        "kind": "exceed_ceiling",
        "subject_key": "sha256:" + "a" * 64,
        "payload": {},
        "state": "pending",
        "requested_by_user_id": USR,
        "requested_by_name": "Ada Admin",
        "requested_via_agent": False,
        "decided_by_user_id": None,
        "decided_at": None,
        "decision_reason": None,
        "decision_channel": None,
        "recorded_by_operator": None,
        "policy_decision_id": None,
        "created_at": AT,
    } | changes


def _detail(**changes: object) -> dict[str, object]:
    return (
        _approval()
        | {
            "grant_diff": {
                "added": [
                    {
                        "role": "user",
                        "subject_kind": "org",
                        "subject_id": None,
                        "subject_name": None,
                    },
                    {
                        "role": "user",
                        "subject_kind": "group",
                        "subject_id": "grp_aaaaaaaaaaaaaaaaaaaa",
                        "subject_name": "Finance",
                    },
                ],
                "removed": [],
            },
            "connection": {
                "name": "finance",
                "classification": "confidential",
                "owner_user_id": USR,
                "ceiling_audience": "subjects",
                "ceiling_subjects": 1,
            },
            "can_decide": True,
            "can_cancel": False,
        }
        | changes
    )


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    return fake_api


def test_the_inbox_lists_what_you_may_decide(cli, scripted):
    body = {
        "approvals": [
            _approval(),
            _approval(
                OTHER,
                kind="enable_internet_hosts",
                subject_key="api.example.com",
                requested_via_agent=True,
            ),
        ],
        "next_before": None,
    }
    scripted.add("GET", "/v1/approvals", httpx2.Response(200, json=body))
    r = cli("approvals", "list", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    assert scripted.seen[-1].url.params["inbox"] == "true"
    result = ApprovalsResult.model_validate(r.json())
    assert result.scope == "inbox" and not result.more
    assert [(a.id, a.subject, a.requested_by) for a in result.approvals] == [
        (APR, None, "Ada Admin"),
        (OTHER, "api.example.com", "Ada Admin"),
    ]
    scripted.add("GET", "/v1/approvals", httpx2.Response(200, json=body))
    human = cli("approvals", "list", session=scripted.session())
    assert "beyond a connection's ceiling" in human.stdout
    assert "new outbound host api.example.com" in human.stdout
    assert "Ada Admin (agent)" in human.stdout
    assert "sha256" not in human.stdout


def test_an_empty_inbox_says_so_and_all_drops_the_filter(cli, scripted):
    empty = {"approvals": [], "next_before": APR}
    scripted.add("GET", "/v1/approvals", httpx2.Response(200, json=empty))
    assert cli("approvals", "list", session=scripted.session()).stdout.strip() == (
        "Nothing is waiting for you."
    )
    scripted.add("GET", "/v1/approvals", httpx2.Response(200, json=empty))
    r = cli("approvals", "list", "--all", "--json", session=scripted.session())
    assert "inbox" not in scripted.seen[-1].url.params
    result = ApprovalsResult.model_validate(r.json())
    assert (result.scope, result.more) == ("all", True)


def test_show_lists_the_sharing_rules_a_request_would_change(cli, scripted):
    path = f"/v1/approvals/{APR}"
    scripted.add("GET", path, httpx2.Response(200, json=_detail()))
    r = cli("approvals", "show", APR, "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    result = ApprovalShowResult.model_validate(r.json())
    assert (result.connection, result.can_decide) == ("finance", True)
    assert [g.subject_kind for g in result.added] == ["org", "group"]
    scripted.add("GET", path, httpx2.Response(200, json=_detail()))
    human = cli("approvals", "show", APR, session=scripted.session())
    assert "Add user: everyone in the org" in human.stdout
    assert "Add user: Finance" in human.stdout
    assert f"ssc approvals approve {APR}" in human.stdout


def test_show_says_when_you_cannot_decide(cli, scripted):
    scripted.add(
        "GET", f"/v1/approvals/{APR}", httpx2.Response(200, json=_detail(can_decide=False))
    )
    human = cli("approvals", "show", APR, session=scripted.session())
    assert "You cannot decide this one." in human.stdout


@pytest.mark.parametrize(
    ("command", "outcome", "state"),
    [("approve", "approved", "approved"), ("reject", "denied", "denied")],
)
def test_approve_and_reject_send_the_reason_as_the_cli(cli, scripted, command, outcome, state):
    done = _approval(state=state, decision_reason="Because.") | {
        "applied": "applied" if state == "approved" else "not_applicable",
        "applied_reason": None,
    }
    scripted.add("POST", f"/v1/approvals/{APR}/decide", httpx2.Response(200, json=done))
    r = cli("approvals", command, APR, "-m", "Because.", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    sent = json.loads(scripted.seen[-1].content)
    assert sent == {"outcome": outcome, "reason": "Because.", "channel": "cli"}
    assert scripted.seen[-1].headers["idempotency-key"]
    result = ApprovalDecisionResult.model_validate(r.json())
    assert (result.state, result.reason) == (state, "Because.")


@pytest.mark.parametrize(
    ("applied", "said"),
    [
        ("applied", "The change is applied."),
        ("waiting", "still open"),
        ("not_applied", "has to ask again"),
    ],
)
def test_approving_says_what_it_did(cli, scripted, applied, said):
    done = _approval(state="approved") | {"applied": applied, "applied_reason": "stale"}
    scripted.add("POST", f"/v1/approvals/{APR}/decide", httpx2.Response(200, json=done))
    human = cli("approvals", "approve", APR, "-m", "Fine.", session=scripted.session())
    assert human.code == 0, human.stderr
    assert said in human.stdout


def test_a_reason_is_required_and_bounded(cli, scripted):
    assert cli("approvals", "approve", APR, session=scripted.session()).code == ExitCode.USAGE
    assert cli("approvals", "reject", APR, "-m", "  ", session=scripted.session()).code == (
        ExitCode.USAGE
    )
    assert cli("approvals", "reject", APR, "-m", "x" * 501, session=scripted.session()).code == (
        ExitCode.USAGE
    )
    assert scripted.seen == []


@pytest.mark.parametrize(
    "code", ["AGENT_SESSION_REFUSED", "SELF_APPROVAL_REFUSED", "APPROVAL_NOT_PENDING"]
)
def test_a_refusal_is_shown_with_its_code(cli, scripted, fake_problem, code):
    scripted.add("POST", f"/v1/approvals/{APR}/decide", fake_problem(403, code))
    r = cli("approvals", "approve", APR, "-m", "Fine.", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == code

"""``ssc connections``: the org's data connections, and what one app environment may reach."""

import httpx2
import pytest

from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.shapes import ConnectionsResult, ConnectResult

APP_ID = "app_aaaaaaaaaaaaaaaaaaaa"
USR = "usr_aaaaaaaaaaaaaaaaaaaa"
GRP = "grp_aaaaaaaaaaaaaaaaaaaa"
PROD = "env_prodprodprodprodprod"
PREVIEW = "env_prevprevprevprevprev"
AT = "2026-09-29T00:00:00Z"
LINKS = f"/v1/apps/{APP_ID}/environments/{PROD}/connections"


def _connection(name: str = "finance", **changes: object) -> dict[str, object]:
    return {
        "id": "con_aaaaaaaaaaaaaaaaaaaa",
        "name": name,
        "kind": "postgres",
        "owner_user_id": USR,
        "classification": "confidential",
        "ceiling": {"audience": "subjects", "subjects": [{"kind": "group", "id": GRP}]},
        "allowed_schemas": ["public"],
        "limits": {},
        "setup_status": "ready",
        "status": "active",
        "created_at": AT,
        "updated_at": AT,
    } | changes


def _app() -> dict[str, object]:
    return {
        "id": APP_ID,
        "slug": "demo",
        "owner_user_id": USR,
        "status": "active",
        "created_at": AT,
        "environments": [
            {"id": PREVIEW, "name": "preview", "config_version": 1, "grants_version": 1},
            {"id": PROD, "name": "prod", "config_version": 1, "grants_version": 1},
        ],
    }


@pytest.fixture
def scripted(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    summary = {k: _app()[k] for k in ("id", "slug", "owner_user_id", "status")}
    fake_api.add("GET", "/v1/apps", httpx2.Response(200, json={"apps": [summary]}))
    fake_api.add("GET", f"/v1/apps/{APP_ID}", httpx2.Response(200, json=_app()))
    return fake_api


def test_the_orgs_connections_are_listed_by_name_and_ceiling(cli, scripted):
    body = {"connections": [_connection(), _connection("payroll", classification="internal")]}
    body["connections"][1]["ceiling"] = {"audience": "org", "subjects": []}
    scripted.add("GET", "/v1/connections", httpx2.Response(200, json=body))
    r = cli("connections", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    result = ConnectionsResult.model_validate(r.json())
    assert (result.app_id, result.slug, result.environment) == (None, None, None)
    assert [(c.name, c.ceiling) for c in result.connections] == [
        ("finance", [f"group:{GRP}"]),
        ("payroll", ["org"]),
    ]
    assert all(c.over_ceiling_since is None for c in result.connections)
    assert "host" not in r.stdout
    scripted.add("GET", "/v1/connections", httpx2.Response(200, json=body))
    human = cli("connections", session=scripted.session())
    assert human.code == 0, human.stderr
    assert "CONNECTION" in human.stdout
    assert f"group:{GRP}" in human.stdout
    assert "Over ceiling" not in human.stdout


def test_an_app_environment_shows_what_it_reaches_and_what_is_over_its_ceiling(cli, scripted):
    linked = {
        "connections": [
            {"environment_id": PROD, "connection": _connection(), "over_ceiling_since": AT},
            {
                "environment_id": PROD,
                "connection": _connection("payroll"),
                "over_ceiling_since": None,
            },
        ]
    }
    scripted.add("GET", LINKS, httpx2.Response(200, json=linked), httpx2.Response(200, json=linked))
    r = cli("connections", "demo", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    result = ConnectionsResult.model_validate(r.json())
    assert (result.app_id, result.slug, result.environment) == (APP_ID, "demo", "prod")
    assert [(c.name, c.over_ceiling_since) for c in result.connections] == [
        ("finance", AT),
        ("payroll", None),
    ]
    human = cli("connections", "demo", session=scripted.session())
    assert human.code == 0, human.stderr
    assert AT in human.stdout
    assert "Over ceiling: this environment is shared more widely" in human.stdout


def test_an_environment_with_no_connections_says_so(cli, scripted):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/connections"
    scripted.add("GET", path, httpx2.Response(200, json={"connections": []}))
    r = cli("connections", "demo", "--env", "preview", session=scripted.session())
    assert r.code == 0, r.stderr
    assert r.stdout.strip() == "demo preview reaches no data connections."


def test_no_connections_at_all_says_so(cli, scripted):
    scripted.add("GET", "/v1/connections", httpx2.Response(200, json={"connections": []}))
    assert cli("connections", session=scripted.session()).stdout.strip() == "No data connections."


def test_a_refusal_is_shown_with_its_code(cli, scripted, fake_problem):
    scripted.add("GET", LINKS, fake_problem(404, "NOT_FOUND"))
    r = cli("connections", "demo", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "NOT_FOUND"
    scripted.add("GET", "/v1/connections", fake_problem(401, "UNAUTHENTICATED"))
    refused = cli("connections", "--json", session=scripted.session())
    assert refused.code == ExitCode.AUTH


def _approval(state: str = "pending") -> dict[str, object]:
    return {
        "id": "apr_aaaaaaaaaaaaaaaaaaaa",
        "app": "demo",
        "environment": "prod",
        "kind": "connect_data_source",
        "subject_key": "finance",
        "state": state,
        "requested_by_user_id": USR,
        "requested_by_name": "Ada",
        "requested_via_agent": False,
        "decided_by_user_id": None,
        "decision_reason": None,
        "created_at": AT,
    }


def test_connect_asks_prod_and_prints_the_answer(cli, scripted):
    answer = {"outcome": "pending", "reason": None, "approval": _approval(), "connections": []}
    scripted.add("POST", f"{LINKS}/finance/request", httpx2.Response(200, json=answer))
    r = cli("connect", "demo", "finance", "--json", session=scripted.session())
    assert r.code == 0, r.stderr
    result = ConnectResult.model_validate(r.json())
    assert (result.environment, result.connection, result.outcome) == ("prod", "finance", "pending")
    assert result.approval is not None
    assert (result.approval.id, result.approval.subject) == (
        "apr_aaaaaaaaaaaaaaaaaaaa",
        "finance",
    )
    (post,) = [q for q in scripted.seen if q.method == "POST"]
    assert post.content == b"{}"
    assert post.headers["Idempotency-Key"]


@pytest.mark.parametrize(
    ("outcome", "reason", "approval", "line"),
    [
        ("linked", None, None, "demo preview now reads finance."),
        ("already_linked", None, None, "demo preview already reads finance; nothing to do."),
        (
            "pending",
            None,
            _approval(),
            "Asked for demo preview to read finance (apr_aaaaaaaaaaaaaaaaaaaa); another active "
            "admin of the org must approve.",
        ),
        (
            "approved_not_linked",
            "ceiling",
            _approval("approved"),
            "finance is approved for demo preview but not linked (ceiling); an org admin can link "
            "it anyway.",
        ),
    ],
)
def test_connect_says_one_line_per_outcome(  # noqa: PLR0913, PLR0917  (parametrized)
    cli, scripted, outcome, reason, approval, line
):
    path = f"/v1/apps/{APP_ID}/environments/{PREVIEW}/connections/finance/request"
    linked = [{"environment_id": PREVIEW, "connection": _connection(), "over_ceiling_since": None}]
    body = {"outcome": outcome, "reason": reason, "approval": approval, "connections": linked}
    scripted.add("POST", path, httpx2.Response(200, json=body))
    r = cli("connect", "demo", "finance", "--env", "preview", session=scripted.session())
    assert r.code == 0, r.stderr
    assert r.stdout.strip() == line


def test_connect_shows_a_refusal(cli, scripted, fake_problem):
    scripted.add("POST", f"{LINKS}/nope/request", fake_problem(404, "NOT_FOUND"))
    r = cli("connect", "demo", "nope", "--json", session=scripted.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "NOT_FOUND"

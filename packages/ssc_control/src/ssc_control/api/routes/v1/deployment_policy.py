"""What the org lets the caller's apps reach and use (SSC-093): ``GET /v1/org/deployment-policy``.

A coding agent reads it before writing code that calls out, so it asks for what exists instead of
guessing. It returns nothing the caller could not see already, by the approvals rule (decision
016): operators and active org admins see the whole org; anyone else sees only what their own
approved requests opened. So:

- ``hosts``: internet hosts approved for an environment (``enable_internet_hosts``);
- ``connections``: the org's data connections by name, kind, classification, owner and audience
  ceiling, never their address; a member sees only those their own approved
  ``connect_data_source`` requests name;
- ``approvals``: which changes wait for a person, and who decides;
- ``database``: whether the company's database has room for another app database, counted from
  the control plane's records (the cell agent's count is the one that refuses, ``DB_TIER_FULL``);
- the platform package list and how to ask for a package.

Until SSC-053 (the org's host allowlist) lands, approved requests are the only record of a host.
Workload credentials are ``FORBIDDEN``; an operator's read is audited as ``operator.access``.
"""

from typing import Final, Literal

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy import text

from ssc_contracts import app_database
from ssc_contracts.audit import AuditAction
from ssc_contracts.errors import ErrorCode
from ssc_contracts.packages import APPROVED_PACKAGES, HOW_TO_ASK
from ssc_control.api.auth import PrincipalKind
from ssc_control.api.routes.common import AUTHENTICATED, problem_responses
from ssc_control.api.routes.v1.approvals import sees_every_request
from ssc_control.api.routes.v1.common import Strict
from ssc_control.api.routes.v1.connections import CeilingDoc
from ssc_control.api.uow import UserUoW
from ssc_control.domain.approval_rules import RequirementKind

router = APIRouter()

PLACES_TOTAL: Final = app_database.ceiling(
    app_database.BASE_TIER_MAX_CONNECTIONS, app_database.SUPERUSER_RESERVE
)
APPROVER: Final = (
    "An active admin of the org other than the person who asked, never through an agent. They "
    "decide by email or chat and an SSC operator records it. Ask through POST /v1/approvals "
    "(the request_connection and request_share tools do it for a connection or a share)."
)
APPROVALS: Final = (
    (
        RequirementKind.CONNECT_DATA_SOURCE,
        "A production deploy of an app that names a data connection under [connections].",
    ),
    (
        RequirementKind.ENABLE_INTERNET_HOSTS,
        "A production deploy of an app that names an internet host under [egress] hosts.",
    ),
    (
        RequirementKind.WIDEN_AUDIENCE,
        "Showing an app that reaches company data to more people.",
    ),
    (
        RequirementKind.AGENT_SHARE,
        "Any sharing change made with an agent's credential.",
    ),
    (
        RequirementKind.EXCEED_CEILING,
        "Showing an app wider than the audience ceiling of a data connection it uses. The "
        "connection's owner or an org admin decides.",
    ),
)

_MINE = " and a.requested_by_user_id = :me"
_HOSTS = (
    "select distinct a.subject_key as host, e.app_id, a.environment_id "
    "from ssc.approval_request a "
    "join ssc.environment e on e.org_id = a.org_id and e.id = a.environment_id "
    "where a.org_id = :org and a.kind = 'enable_internet_hosts' and a.state = 'approved'"
)
_ORDER_HOSTS = " order by 1, 2, 3"
_ALL_CONNECTIONS = text(
    "select name, kind, classification, owner_user_id, ceiling from ssc.connection "
    "where org_id = :org order by name"
)
_OWN_CONNECTIONS = text(
    "select c.name, c.kind, c.classification, c.owner_user_id, c.ceiling from ssc.connection c "  # noqa: S608  (constant SQL fragments)
    "where c.org_id = :org and exists (select 1 from ssc.approval_request a "
    "where a.org_id = c.org_id and a.kind = 'connect_data_source' and a.state = 'approved' "
    "and a.subject_key = c.name" + _MINE + ") order by c.name"
)
_PLACES = text("select count(*) from ssc.app_database where org_id = :org")


class PolicyHost(Strict):
    host: str
    app_id: str
    environment_id: str


class PolicyConnection(Strict):
    name: str
    kind: str
    classification: Literal["internal", "confidential", "restricted"]
    owner_user_id: str | None
    ceiling: CeilingDoc


class PolicyApproval(Strict):
    kind: RequirementKind
    when: str


class PolicyDatabase(Strict):
    places_used: int = Field(description="App databases the org's apps hold.")
    places_total: int = Field(description="App databases the company's database tier holds.")
    room: bool = Field(description="Whether one more app may ask for a database.")


class DeploymentPolicy(Strict):
    scope: Literal["org", "own"] = Field(
        description="`org`: every host and connection in the org (admins and operators). "
        "`own`: only those the caller's own approved requests opened."
    )
    hosts: list[PolicyHost] = Field(description="Internet hosts approved for an environment.")
    connections: list[PolicyConnection] = Field(
        description="Data connections by name; never their address."
    )
    approvals: list[PolicyApproval] = Field(description="Which changes wait for a person.")
    approver: str = Field(description="Who decides an approval request.")
    database: PolicyDatabase
    approved_packages: list[str] = Field(
        description="The system packages a build may install: the platform package list."
    )
    how_to_ask_for_a_package: str


@router.get(
    "/org/deployment-policy",
    response_model=DeploymentPolicy,
    responses=problem_responses(*AUTHENTICATED, ErrorCode.FORBIDDEN),
)
async def get_deployment_policy(uow: UserUoW) -> DeploymentPolicy:
    """The hosts, connections, approvals, database room and packages the caller may see."""
    everything = await sees_every_request(uow)
    params = {"org": uow.org_id, "me": uow.principal.subject}
    hosts_sql = text(_HOSTS + ("" if everything else _MINE) + _ORDER_HOSTS)
    hosts = (await uow.conn.execute(hosts_sql, params)).mappings().all()
    connections_sql = _ALL_CONNECTIONS if everything else _OWN_CONNECTIONS
    connections = (await uow.conn.execute(connections_sql, params)).mappings().all()
    used = int((await uow.conn.execute(_PLACES, params)).scalar_one())
    if uow.principal.kind is PrincipalKind.OPERATOR:
        await uow.audit(AuditAction.OPERATOR_ACCESS, target_kind="org", target_id=uow.org_id)
    return DeploymentPolicy(
        scope="org" if everything else "own",
        hosts=[PolicyHost.model_validate(dict(h)) for h in hosts],
        connections=[PolicyConnection.model_validate(dict(c)) for c in connections],
        approvals=[PolicyApproval(kind=k, when=w) for k, w in APPROVALS],
        approver=APPROVER,
        database=PolicyDatabase(
            places_used=used, places_total=PLACES_TOTAL, room=used < PLACES_TOTAL
        ),
        approved_packages=sorted(APPROVED_PACKAGES),
        how_to_ask_for_a_package=HOW_TO_ASK,
    )

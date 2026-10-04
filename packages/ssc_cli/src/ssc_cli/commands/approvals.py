"""``ssc approvals``: the requests waiting for you, and approve or reject them (SSC-049).

``list`` shows what you may decide now (``--all`` shows everything you may see), ``show`` one
request with the sharing rules it would change, ``approve`` and ``reject`` decide it with a
reason. You decide as yourself, never in an agent session, and never your own request.
Approving a sharing change applies it once every approval it needs is in.
"""

from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.models import Approval, ApprovalDetail, DiffGrant
from ssc_cli.output import print_json, say, table
from ssc_cli.shapes import (
    ApprovalDecisionResult,
    ApprovalGrantRow,
    ApprovalRow,
    ApprovalShowResult,
    ApprovalsResult,
)

MAX_REASON: Final = 500
NAMED: Final = frozenset({"connect_data_source", "enable_internet_hosts"})
KINDS: Final = {
    "widen_audience": "wider sharing",
    "agent_share": "agent request",
    "exceed_ceiling": "beyond a connection's ceiling",
    "connect_data_source": "new data connection",
    "enable_internet_hosts": "new outbound host",
}
APPLIED: Final = {
    "applied": "The change is applied.",
    "waiting": "Another approval for the same change is still open; it applies with the last one.",
    "not_applied": "Approved, but the change no longer fits, so nothing was applied. "
    "The requester has to ask again.",
    "not_applicable": "",
}

approvals_app = typer.Typer(
    name="approvals",
    help="The requests waiting for your approval: list, show, approve or reject.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _reason(value: str) -> str:
    if not value.strip() or len(value) > MAX_REASON:
        raise typer.BadParameter(f"give a reason of 1 to {MAX_REASON} characters")
    return value


IdArg = Annotated[str, typer.Argument(metavar="ID", help="The approval request, apr_...")]
ReasonOpt = Annotated[
    str,
    typer.Option("--reason", "-m", callback=_reason, help="Why. The requester is told."),
]
AllOpt = Annotated[
    bool, typer.Option("--all", help="Everything you may see, not only what you may decide.")
]


def _row(a: Approval) -> ApprovalRow:
    return ApprovalRow(
        id=a.id,
        app=a.app,
        environment=a.environment,
        kind=a.kind,
        subject=a.subject_key if a.kind in NAMED else None,
        state=a.state,
        requested_by=a.requested_by_name,
        requested_via_agent=a.requested_via_agent,
        created_at=a.created_at,
    )


def _grant(g: DiffGrant) -> ApprovalGrantRow:
    return ApprovalGrantRow(
        role=g.role,
        subject_kind=g.subject_kind,
        subject_id=g.subject_id,
        subject_name=g.subject_name,
    )


def _who(g: ApprovalGrantRow) -> str:
    who = "everyone in the org" if g.subject_kind == "org" else g.subject_name or g.subject_id
    return f"{g.role}: {who}"


@approvals_app.command("list")
def list_approvals(ctx: typer.Context, all_: AllOpt = False, json_mode: JsonOpt = False) -> None:
    """List the requests waiting for you, newest first."""
    with handled(json_mode), session(ctx).client() as client:
        page = client.list_approvals(inbox=not all_)
        result = ApprovalsResult(
            api_url=client.api_url,
            scope="all" if all_ else "inbox",
            approvals=[_row(a) for a in page.approvals],
            more=page.next_before is not None,
        )
    if json_mode:
        print_json(result)
        return
    if not result.approvals:
        say("Nothing is waiting for you." if not all_ else "No approval requests.")
        return
    say(
        table(
            ("ID", "APP", "ENV", "ASKED FOR", "WHO", "STATE"),
            [
                (
                    a.id,
                    a.app,
                    a.environment,
                    KINDS.get(a.kind, a.kind) + (f" {a.subject}" if a.subject else ""),
                    a.requested_by + (" (agent)" if a.requested_via_agent else ""),
                    a.state,
                )
                for a in result.approvals
            ],
        )
    )
    if result.more:
        say("\nOlder requests are not shown.")


def _show(client: ApiClient, d: ApprovalDetail) -> ApprovalShowResult:
    diff = d.grant_diff
    return ApprovalShowResult(
        api_url=client.api_url,
        id=d.id,
        app=d.app,
        environment=d.environment,
        kind=d.kind,
        subject=d.subject_key if d.kind in NAMED else None,
        state=d.state,
        requested_by=d.requested_by_name,
        requested_via_agent=d.requested_via_agent,
        decided_by_user_id=d.decided_by_user_id,
        decision_reason=d.decision_reason,
        created_at=d.created_at,
        added=[] if diff is None else [_grant(g) for g in diff.added],
        removed=[] if diff is None else [_grant(g) for g in diff.removed],
        connection=None if d.connection is None else d.connection.name,
        can_decide=d.can_decide,
    )


@approvals_app.command("show")
def show_approval(ctx: typer.Context, approval_id: IdArg, json_mode: JsonOpt = False) -> None:
    """Show one request: who asked, for what, and the sharing rules it would change."""
    with handled(json_mode), session(ctx).client() as client:
        detail = client.get_approval(approval_id)
        result = _show(client, detail)
    if json_mode:
        print_json(result)
        return
    asked = KINDS.get(result.kind, result.kind) + (f" {result.subject}" if result.subject else "")
    asker = result.requested_by + (" (through an agent)" if result.requested_via_agent else "")
    say(f"{result.id}  {result.app} ({result.environment})  {result.state}")
    say(f"Asked by {asker}: {asked}")
    if result.connection:
        say(f"Data connection: {result.connection}")
    for label, rows in (("Add", result.added), ("Remove", result.removed)):
        for g in rows:
            say(f"  {label} {_who(g)}")
    if result.decision_reason:
        say(f"Decision: {result.decision_reason}")
    if result.state == "pending":
        say(
            f"Decide with `ssc approvals approve {result.id} -m ...` or `reject`."
            if result.can_decide
            else "You cannot decide this one."
        )


def _decide(
    ctx: typer.Context, approval_id: str, outcome: str, reason: str, json_mode: bool
) -> None:
    with handled(json_mode), session(ctx).client() as client:
        done = client.decide_approval(approval_id, outcome, reason)
        result = ApprovalDecisionResult(
            api_url=client.api_url,
            id=done.id,
            state=done.state,
            reason=reason,
            applied=done.applied,
            applied_reason=done.applied_reason,
        )
    if json_mode:
        print_json(result)
        return
    say(f"{result.id} is {'approved' if result.state == 'approved' else 'rejected'}.")
    if result.state == "approved" and APPLIED.get(result.applied):
        say(APPLIED[result.applied])
    if result.applied_reason:
        say(f"Why: {result.applied_reason}")


@approvals_app.command("approve")
def approve(
    ctx: typer.Context, approval_id: IdArg, reason: ReasonOpt, json_mode: JsonOpt = False
) -> None:
    """Approve a request. Applies a sharing change once every approval it needs is in."""
    _decide(ctx, approval_id, "approved", reason, json_mode)


@approvals_app.command("reject")
def reject(
    ctx: typer.Context, approval_id: IdArg, reason: ReasonOpt, json_mode: JsonOpt = False
) -> None:
    """Reject a request. The requester is told why."""
    _decide(ctx, approval_id, "denied", reason, json_mode)

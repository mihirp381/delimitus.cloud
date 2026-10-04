"""The text of each mail (SSC-049). Pure functions: plain text, no customer data values, and a
link to the console and nothing that approves by itself."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

MAX_LISTED: Final = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    """A request as a mail describes it."""

    id: str
    kind: str
    subject_key: str
    payload: Mapping[str, Any]
    app: str
    environment: str
    requester: str
    state: str = "pending"
    decision_reason: str | None = None


def _rules(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    grants = payload.get("grants")
    if not isinstance(grants, list):
        return []
    return [cast(Mapping[str, Any], g) for g in cast(list[object], grants) if isinstance(g, dict)]


def describe(kind: str, subject_key: str, payload: Mapping[str, Any]) -> str:
    """What the request asks for, in a sentence, without naming any user or group."""
    rules = _rules(payload)
    org_wide = any(g.get("subject_kind") == "org" for g in rules)
    sharing = f"{len(rules)} sharing rule(s)" + (
        ", the whole organisation included" if org_wide else ""
    )
    match kind:
        case "widen_audience":
            return f"Show the app to more people: {sharing}."
        case "agent_share":
            return f"A sharing change made through an agent: {sharing}."
        case "exceed_ceiling":
            return (
                f"Share the app beyond the audience ceiling of the data connection "
                f"{payload.get('connection')}: {sharing}."
            )
        case "connect_data_source":
            return f"Connect the data source {subject_key}."
        case "enable_internet_hosts":
            return f"Allow the internet host {subject_key}."
        case _:
            return "A change to the app."


def link(console_url: str, approval_id: str) -> str:
    return f"{console_url.rstrip('/')}/approvals/{approval_id}"


def _where(r: Request) -> str:
    return f"{r.app} ({r.environment})"


def arrival(r: Request, console_url: str, *, reminder: bool = False) -> tuple[str, str]:
    """The mail to an approver when a request arrives, or the reminder after three days."""
    subject = f"{'Reminder: ' if reminder else ''}Approval needed for {_where(r)}"
    body = (
        f"{r.requester} {'asked three days ago' if reminder else 'asked'} for your approval to "
        f"change {_where(r)}.\n\n{describe(r.kind, r.subject_key, r.payload)}\n\n"
        f"Review it: {link(console_url, r.id)}\n\n"
        "Nothing changes until someone approves it. You are receiving this because you can "
        "decide it."
    )
    return subject, body


def decision(r: Request, console_url: str) -> tuple[str, str]:
    """The mail to the requester when their request is decided."""
    outcome = {"approved": "approved", "denied": "rejected"}.get(r.state, r.state)
    subject = f"Your request for {_where(r)} was {outcome}"
    reason = f"\n\nReason given: {r.decision_reason}" if r.decision_reason else ""
    body = (
        f"Your request was {outcome}.\n\n{describe(r.kind, r.subject_key, r.payload)}{reason}"
        f"\n\nDetails: {link(console_url, r.id)}"
    )
    return subject, body


def digest(requests: Sequence[Request], console_url: str) -> tuple[str, str]:
    """The daily mail listing what an approver can decide right now."""
    n = len(requests)
    subject = f"{n} approval request(s) waiting for you"
    lines = [
        f"- {_where(r)}: {describe(r.kind, r.subject_key, r.payload)} {link(console_url, r.id)}"
        for r in requests[:MAX_LISTED]
    ]
    more = f"\n...and {n - MAX_LISTED} more." if n > MAX_LISTED else ""
    return subject, "Waiting for your decision:\n\n" + "\n".join(lines) + more

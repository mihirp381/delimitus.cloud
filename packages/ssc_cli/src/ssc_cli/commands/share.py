"""``ssc share`` and ``ssc unshare``: change one subject's grant on one environment.

The sharing rules are one versioned document per environment. Each change reads it with its
ETag, edits the one subject's entry, and writes it back with ``If-Match``. If someone else wrote
in between (412 ``PRECONDITION_STALE``), it reads again and retries, at most three times.

A change that needs approval is not applied (decision 016). Through an agent credential the API
asks for the approvals itself and answers ``202``: the command prints the approval ids and exits
0. A person widening a data-connected app gets ``APPROVAL_REQUIRED`` and a line saying how to ask.

The API keeps the reasons for ``VALIDATION_FAILED`` and ``FORBIDDEN`` in its log (decision 019),
so the ``Fix:`` line for them is worked out here from the sharing rules: which grant is below its
environment's floor or repeated, or who may change that environment's sharing.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import CliError
from ssc_cli.models import GrantIn, GrantOut, GrantsOut, GrantsPending
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import GrantRow, ShareResult
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import prefix_of

MAX_STALE_RETRIES: Final = 3
SUBJECT_KINDS: Final = {"usr": "user", "grp": "group"}


class Env(StrEnum):
    prod = "prod"
    preview = "preview"


class Role(StrEnum):
    user = "user"
    builder = "builder"


# The lowest role each environment accepts (decision 019). The API decides; this picks the
# default role and explains the API's refusals.
FLOOR: Final = {Env.prod: Role.user, Env.preview: Role.builder}
DEFAULT_ROLE: Final = FLOOR
RANK: Final[dict[str, int]] = {Role.user.value: 1, Role.builder.value: 2}

WhoArg = Annotated[str | None, typer.Argument(help="A usr_ or grp_ id.", show_default=False)]
OrgOpt = Annotated[bool, typer.Option("--org", help="Everyone in the org instead of one id.")]
EnvOpt = Annotated[Env, typer.Option("--env", help="Which environment.")]


@dataclass(frozen=True, slots=True)
class Subject:
    kind: str
    id: str | None

    def matches(self, g: GrantOut) -> bool:
        return g.subject_kind == self.kind and g.subject_id == self.id

    def label(self) -> str:
        return self.id or "everyone in the org"


def share(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to the command line)
    ctx: typer.Context,
    app: AppArg,
    who: WhoArg = None,
    org: OrgOpt = False,
    env: EnvOpt = Env.prod,
    role: Annotated[
        Role | None,
        typer.Option("--role", help="user or builder. Default: user on prod, builder on preview."),
    ] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Let a person, a group or the whole org use an app environment."""
    subject = _subject(who, org)
    wanted = GrantIn(
        role=(role or DEFAULT_ROLE[env]).value, subject_kind=subject.kind, subject_id=subject.id
    )

    def change(grants: list[GrantOut]) -> list[GrantIn] | None:
        mine = [g for g in grants if subject.matches(g)]
        if len(mine) == 1 and mine[0].role == wanted.role:
            return None
        return [_to_in(g) for g in grants if not subject.matches(g)] + [wanted]

    done = f"Shared {env.value} with {subject.label()} as {wanted.role}."
    _run(ctx, app, env, change, json_mode=json_mode, done=done)


def unshare(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to the command line)
    ctx: typer.Context,
    app: AppArg,
    who: WhoArg = None,
    org: OrgOpt = False,
    env: EnvOpt = Env.prod,
    json_mode: JsonOpt = False,
) -> None:
    """Remove a person's, a group's or the org's grant on an app environment."""
    subject = _subject(who, org)

    def change(grants: list[GrantOut]) -> list[GrantIn] | None:
        if not any(subject.matches(g) for g in grants):
            return None
        return [_to_in(g) for g in grants if not subject.matches(g)]

    done = f"Removed {subject.label()} from {env.value}."
    _run(ctx, app, env, change, json_mode=json_mode, done=done)


Change = Callable[[list[GrantOut]], list[GrantIn] | None]


def _run(  # noqa: PLR0913
    ctx: typer.Context, app: str, env: Env, change: Change, *, json_mode: bool, done: str
) -> None:
    with handled(json_mode), session(ctx).client() as client:
        result = apply_change(client, app, env, change)
    if json_mode:
        print_json(result)
        return
    if result.pending:
        say(f"Waiting for approval, nothing changed yet: {', '.join(result.pending)}.")
        say("Run the same command again once another admin of the org has approved.")
    else:
        say(done if result.changed else "Nothing to change.")
    say(
        table(
            ("ROLE", "KIND", "SUBJECT"),
            [(g.role, g.subject_kind, dash(g.subject_id)) for g in result.grants],
        )
    )


def apply_change(client: ApiClient, app_ref: str, env_name: Env, change: Change) -> ShareResult:
    app = resolve_app(client, app_ref)
    env = environment(app, env_name.value)
    attempt = 0
    while True:
        current, etag = client.get_grants(app.id, env.id)
        desired = change(current.grants)
        if desired is None:
            return _result(app.id, env_name, current, changed=False)
        try:
            after = client.put_grants(app.id, env.id, desired, etag)
        except CliError as e:
            if e.body.code == ErrorCode.PRECONDITION_STALE and attempt < MAX_STALE_RETRIES:
                attempt += 1
                continue
            if e.body.code == ErrorCode.APPROVAL_REQUIRED:
                e.fix = approval_fix(env.id, desired)
            elif e.body.code == ErrorCode.VALIDATION_FAILED:
                e.fix = rules_fix(app_ref, env_name, current.grants, desired)
            elif e.body.code == ErrorCode.FORBIDDEN:
                e.fix = forbidden_fix(env_name)
            raise
        if isinstance(after, GrantsPending):
            return _result(app.id, env_name, current, changed=False, pending=after.approval_ids)
        return _result(app.id, env_name, after, changed=True)


def rules_fix(app_ref: str, env: Env, current: list[GrantOut], desired: list[GrantIn]) -> str:
    """Which grants break the sharing rules, and what to run about each."""
    floor = FLOOR[env]
    allowed = " or ".join(r for r, n in RANK.items() if n >= RANK[floor.value])
    stored = {(g.role, g.subject_kind, g.subject_id) for g in current}
    fixes: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    for g in desired:
        who = g.subject_id or "everyone in the org"
        rank = RANK.get(g.role)
        if rank is not None and rank < RANK[floor.value]:
            if (g.role, g.subject_kind, g.subject_id) in stored:
                arg = g.subject_id or "--org"
                fixes.append(
                    f"the {g.role} grant for {who} was saved before the rule and blocks every "
                    f"change to {env.value}; remove it first with "
                    f"`ssc unshare {app_ref} {arg} --env {env.value}`"
                )
            else:
                fixes.append(
                    f"{env.value} takes {allowed} grants, so {who} cannot be a {g.role} there; "
                    f"use `--role {floor.value}`"
                )
        if (g.subject_kind, g.subject_id) in seen:
            fixes.append(f"{who} has two grants; keep one")
        seen.add((g.subject_kind, g.subject_id))
    if not fixes:
        return (
            "prod takes user or builder grants, preview takes builder grants only, and each "
            "person, group or the org has at most one grant per environment."
        )
    return "; ".join(fixes) + "."


def forbidden_fix(env: Env) -> str:
    """Who may change an environment's sharing (decision 019)."""
    return (
        f"only an org admin, the app's owner or a builder on {env.value} can change who uses "
        f"{env.value}; a builder on one environment cannot change the other. Ask one of them to "
        "make the change or to make you a builder there."
    )


def approval_fix(environment_id: str, desired: list[GrantIn]) -> str:
    """How to ask for the ``widen_audience`` approval of exactly this grant set."""
    ask = {
        "environment_id": environment_id,
        "kind": "widen_audience",
        "payload": {"grants": [g.model_dump(mode="json") for g in desired]},
    }
    return (
        f"ask for approval with POST /v1/approvals {json.dumps(ask)}, then run this command "
        "again once another admin of the org has approved it."
    )


def _result(
    app_id: str, env_name: Env, out: GrantsOut, *, changed: bool, pending: list[str] | None = None
) -> ShareResult:
    return ShareResult(
        app_id=app_id,
        environment=env_name.value,
        environment_id=out.environment_id,
        grants_version=out.grants_version,
        changed=changed,
        grants=[
            GrantRow(id=g.id, role=g.role, subject_kind=g.subject_kind, subject_id=g.subject_id)
            for g in out.grants
        ],
        pending=pending or [],
    )


def _to_in(g: GrantOut) -> GrantIn:
    return GrantIn(role=g.role, subject_kind=g.subject_kind, subject_id=g.subject_id)


def _subject(who: str | None, org: bool) -> Subject:
    if org == (who is not None):
        raise typer.BadParameter("give exactly one of a usr_/grp_ id or --org", param_hint="WHO")
    if who is None:
        return Subject("org", None)
    try:
        kind = SUBJECT_KINDS.get(prefix_of(who))
    except ValueError:
        kind = None
    if kind is None:
        raise typer.BadParameter(
            f"{who!r} is not a usr_ or grp_ id; sharing by email is not available yet",
            param_hint="WHO",
        )
    return Subject(kind, who)

"""``ssc share`` and ``ssc unshare``: change grants on one environment.

``share`` sets one subject's grant. ``unshare`` removes the grants of every subject it names in
one write, so grants that each block the others' removal (decision 019) go together.

The sharing rules are one versioned document per environment. Each change reads it with its
ETag, edits the named subjects' entries, and writes it back with ``If-Match``. If someone else wrote
in between (412 ``PRECONDITION_STALE``), it reads again and retries, at most three times.

A change that needs approval is not applied (decision 016). Through an agent credential the API
asks for the approvals itself and answers ``202``: the command prints the approval ids and exits
0. A person widening a data-connected app gets ``APPROVAL_REQUIRED`` and a line saying how to ask.

The API keeps the reasons for ``VALIDATION_FAILED`` and ``FORBIDDEN`` in its log (decision 019),
so the ``Fix:`` line for them is worked out here from the sharing rules: which grant is below its
environment's floor or repeated, or who may change that environment's sharing.

A person can be named by email and a group by name. Both are display data that several people or
groups can share, so the API answers a list and the command goes on only when exactly one fits;
otherwise it names the candidates' ids. Only org admins may look people up by email. ``--group``
reads every name as a group name, for groups whose name has an ``@`` or looks like an id.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import (
    GROUP_NOT_FOUND,
    SUBJECT_AMBIGUOUS,
    USER_NOT_FOUND,
    CliError,
    ExitCode,
    local_error,
)
from ssc_cli.models import GrantIn, GrantOut, GrantsOut, GrantsPending
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import GrantRow, ShareResult, SubjectRow
from ssc_contracts.errors import ErrorCode
from ssc_contracts.ids import PREFIXES, prefix_of

MAX_STALE_RETRIES: Final = 3
SUBJECT_KINDS: Final = {"usr": "user", "grp": "group"}
# What GET /v1/users?email= and GET /v1/groups?name= accept.
EMAIL: Final = re.compile(r"[^@\s]+@[^@\s]+")
MIN_EMAIL: Final = 3
MAX_EMAIL: Final = 320
MAX_GROUP_NAME: Final = 200
ACTIVE: Final = "active"


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

WhoArg = Annotated[
    str | None,
    typer.Argument(
        help="A usr_ or grp_ id, a person's email address, or a group's name.", show_default=False
    ),
]
WhosArg = Annotated[
    list[str] | None,
    typer.Argument(
        help="Each a usr_ or grp_ id, a person's email address, or a group's name.",
        show_default=False,
    ),
]
OrgOpt = Annotated[bool, typer.Option("--org", help="Everyone in the org instead of one id.")]
GroupOpt = Annotated[
    bool, typer.Option("--group", help="Read each name as a group's name, whatever it looks like.")
]
EnvOpt = Annotated[Env, typer.Option("--env", help="Which environment.")]


@dataclass(frozen=True, slots=True)
class Subject:
    kind: str
    id: str | None
    label: str

    def matches(self, g: GrantOut) -> bool:
        return g.subject_kind == self.kind and g.subject_id == self.id


ORG: Final = Subject("org", None, "everyone in the org")


@dataclass(frozen=True, slots=True)
class Lookup:
    """A person by email or a group by name, found through the API when the command runs."""

    kind: str
    key: str


type Who = Subject | Lookup


def share(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to the command line)
    ctx: typer.Context,
    app: AppArg,
    who: WhoArg = None,
    org: OrgOpt = False,
    group: GroupOpt = False,
    env: EnvOpt = Env.prod,
    role: Annotated[
        Role | None,
        typer.Option("--role", help="user or builder. Default: user on prod, builder on preview."),
    ] = None,
    json_mode: JsonOpt = False,
) -> None:
    """Let a person, a group or the whole org use an app environment."""
    if org == (who is not None):
        raise typer.BadParameter("give exactly one of a person, a group or --org", param_hint="WHO")
    targets = _whos([who] if who is not None else [], org=org, group=group)
    wanted_role = (role or DEFAULT_ROLE[env]).value

    def change(subjects: list[Subject], grants: list[GrantOut]) -> list[GrantIn] | None:
        (subject,) = subjects
        mine = [g for g in grants if subject.matches(g)]
        if len(mine) == 1 and mine[0].role == wanted_role:
            return None
        wanted = GrantIn(role=wanted_role, subject_kind=subject.kind, subject_id=subject.id)
        return [_to_in(g) for g in grants if not subject.matches(g)] + [wanted]

    def done(subjects: list[Subject]) -> str:
        return f"Shared {env.value} with {subjects[0].label} as {wanted_role}."

    _run(ctx, app, env, targets, change, json_mode=json_mode, done=done, active_only=True)


def unshare(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to the command line)
    ctx: typer.Context,
    app: AppArg,
    who: WhosArg = None,
    org: OrgOpt = False,
    group: GroupOpt = False,
    env: EnvOpt = Env.prod,
    json_mode: JsonOpt = False,
) -> None:
    """Remove the grants of people, groups or the org on an app environment, in one change."""
    targets = _whos(who or [], org=org, group=group)

    def named(subjects: list[Subject], g: GrantOut) -> bool:
        return any(s.matches(g) for s in subjects)

    had: list[Subject] = []

    def change(subjects: list[Subject], grants: list[GrantOut]) -> list[GrantIn] | None:
        had[:] = [s for s in subjects if any(s.matches(g) for g in grants)]
        if not had:
            return None
        return [_to_in(g) for g in grants if not named(subjects, g)]

    def done(subjects: list[Subject]) -> str:
        text = f"Removed {', '.join(s.label for s in had)} from {env.value}"
        missing = [s.label for s in subjects if s not in had]
        return f"{text}; {', '.join(missing)} had no grant." if missing else f"{text}."

    # A deactivated person's grant is still worth removing.
    _run(ctx, app, env, targets, change, json_mode=json_mode, done=done, active_only=False)


Change = Callable[[list[Subject], list[GrantOut]], list[GrantIn] | None]


def _run(  # noqa: PLR0913
    ctx: typer.Context,
    app: str,
    env: Env,
    whos: list[Who],
    change: Change,
    *,
    json_mode: bool,
    done: Callable[[list[Subject]], str],
    active_only: bool,
) -> None:
    with handled(json_mode), session(ctx).client() as client:
        subjects, result = apply_change(client, app, env, whos, change, active_only=active_only)
    if json_mode:
        print_json(result)
        return
    if result.pending:
        say(f"Waiting for approval, nothing changed yet: {', '.join(result.pending)}.")
        say("Run the same command again once another admin of the org has approved.")
    else:
        say(done(subjects) if result.changed else "Nothing to change.")
    say(
        table(
            ("ROLE", "KIND", "SUBJECT"),
            [(g.role, g.subject_kind, dash(g.subject_id)) for g in result.grants],
        )
    )


def apply_change(  # noqa: PLR0913
    client: ApiClient,
    app_ref: str,
    env_name: Env,
    whos: list[Who],
    change: Change,
    *,
    active_only: bool,
) -> tuple[list[Subject], ShareResult]:
    app = resolve_app(client, app_ref)
    env = environment(app, env_name.value)
    subjects = [resolve_subject(client, w, env_name, active_only=active_only) for w in whos]
    attempt = 0
    while True:
        current, etag = client.get_grants(app.id, env.id)
        desired = change(subjects, current.grants)
        if desired is None:
            return subjects, _result(app.id, env_name, subjects, current, changed=False)
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
            pending = after.approval_ids
            return subjects, _result(
                app.id, env_name, subjects, current, changed=False, pending=pending
            )
        return subjects, _result(app.id, env_name, subjects, after, changed=True)


def resolve_subject(client: ApiClient, who: Who, env: Env, *, active_only: bool) -> Subject:
    """The one person or group ``who`` names. ``active_only`` passes over deactivated people."""
    if isinstance(who, Subject):
        return who
    if who.kind == "user":
        return _person(client, who.key, active_only=active_only)
    return _group(client, who.key, env)


def _person(client: ApiClient, email: str, *, active_only: bool) -> Subject:
    try:
        found = client.find_users(email).users
    except CliError as e:
        if e.body.code == ErrorCode.FORBIDDEN:
            e.fix = (
                "only an org admin can look people up by email, so a builder shares by usr_ id: "
                "ask the person for theirs (their `ssc whoami` shows it as subject), or ask an "
                "org admin to share by email."
            )
        raise
    fits = [u for u in found if u.status == ACTIVE] if active_only else found
    if not fits:
        detail = f"No one in the org has the address {email}."
        if found:
            ids = ", ".join(u.id for u in found)
            detail = (
                f"Everyone with the address {email} is deactivated ({ids}), so a grant would let "
                "no one in. Give the usr_ id to share anyway."
            )
        raise local_error(USER_NOT_FOUND, "No such person.", detail)
    if len(fits) > 1:
        options = [f"{u.id} ({u.display_name}, {u.status})" for u in fits]
        raise _ambiguous(f"{len(fits)} people have the address {email}", options, "usr_")
    return Subject("user", fits[0].id, f"{email} ({fits[0].id})")


def _group(client: ApiClient, name: str, env: Env) -> Subject:
    try:
        found = client.find_groups(name).groups
    except CliError as e:
        if e.body.code == ErrorCode.FORBIDDEN:
            e.fix = forbidden_fix(env)
        raise
    if not found:
        raise local_error(
            GROUP_NOT_FOUND,
            "No such group.",
            f"The org has no group named {name!r}. Group names come from the company directory "
            "and must match whole (case does not matter).",
        )
    if len(found) > 1:
        options = [f"{g.id} ({g.name}, {g.member_count} active members)" for g in found]
        raise _ambiguous(f"{len(found)} groups are named {name!r}", options, "grp_")
    g = found[0]
    return Subject("group", g.id, f"group {g.name} ({g.id})")


def _ambiguous(what: str, options: list[str], prefix: str) -> CliError:
    e = local_error(
        SUBJECT_AMBIGUOUS, "More than one match.", f"{what}: {'; '.join(options)}.", ExitCode.USAGE
    )
    e.fix = f"run the command again with the {prefix} id you mean."
    return e


def rules_fix(app_ref: str, env: Env, current: list[GrantOut], desired: list[GrantIn]) -> str:
    """Which grants break the sharing rules, and what to run about each.

    Every stored grant below the floor blocks every write, so the ``ssc unshare`` it suggests names
    all of them at once, plus the subjects this command removes. A grant it only replaces is left
    out, since the write keeps that subject.
    """
    floor = FLOOR[env]
    allowed = " or ".join(r for r, n in RANK.items() if n >= RANK[floor.value])

    def below(role: str) -> bool:
        rank = RANK.get(role)
        return rank is not None and rank < RANK[floor.value]

    stored = {(g.role, g.subject_kind, g.subject_id) for g in current}
    kept = {(g.subject_kind, g.subject_id) for g in desired}
    fixes: list[str] = []
    blocking: list[str] = []
    changed: list[str] = []
    for g in current:
        arg = g.subject_id or "--org"
        if below(g.role):
            blocking.append(arg)
            fixes.append(
                f"the {g.role} grant for {g.subject_id or 'everyone in the org'} was saved before "
                f"the rule and blocks every change to {env.value}"
            )
        elif (g.subject_kind, g.subject_id) not in kept:
            changed.append(arg)
    seen: set[tuple[str, str | None]] = set()
    for g in desired:
        who = g.subject_id or "everyone in the org"
        if below(g.role) and (g.role, g.subject_kind, g.subject_id) not in stored:
            fixes.append(
                f"{env.value} takes {allowed} grants, so {who} cannot be a {g.role} there; "
                f"use `--role {floor.value}`"
            )
        if (g.subject_kind, g.subject_id) in seen:
            fixes.append(f"{who} has two grants; keep one")
        seen.add((g.subject_kind, g.subject_id))
    if blocking:
        args = list(dict.fromkeys(blocking + changed))
        fixes.append(
            f"remove {'them' if len(args) > 1 else 'it'} first with "
            f"`ssc unshare {app_ref} {' '.join(args)} --env {env.value}`"
        )
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


def _result(  # noqa: PLR0913
    app_id: str,
    env_name: Env,
    subjects: list[Subject],
    out: GrantsOut,
    *,
    changed: bool,
    pending: list[str] | None = None,
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
        subject_kind=subjects[0].kind,
        subject_id=subjects[0].id,
        subjects=[SubjectRow(kind=s.kind, id=s.id) for s in subjects],
    )


def _to_in(g: GrantOut) -> GrantIn:
    return GrantIn(role=g.role, subject_kind=g.subject_kind, subject_id=g.subject_id)


def _whos(whos: list[str], *, org: bool, group: bool) -> list[Who]:
    """What was typed, checked before any request: ids, email addresses, group names, --org."""
    if not whos and not org:
        raise typer.BadParameter("name a person, a group or --org", param_hint="WHO")
    if group and not whos:
        raise typer.BadParameter("--group needs a group name", param_hint="WHO")
    found = [_who(w, group=group) for w in whos] + ([ORG] if org else [])
    if len(set(found)) < len(found):
        raise typer.BadParameter("each subject once", param_hint="WHO")
    return found


def _who(who: str, *, group: bool) -> Who:
    if group:
        if not 1 <= len(who) <= MAX_GROUP_NAME:
            raise typer.BadParameter(
                f"a group name has 1 to {MAX_GROUP_NAME} characters", param_hint="WHO"
            )
        return Lookup("group", who)
    try:
        prefix = prefix_of(who)
    except ValueError:
        prefix = None
    if prefix in SUBJECT_KINDS:
        return Subject(SUBJECT_KINDS[prefix], who, who)
    if prefix in PREFIXES:
        raise typer.BadParameter(
            f"{who!r} is an id, but not of a person (usr_) or a group (grp_)", param_hint="WHO"
        )
    if who.startswith(tuple(f"{p}_" for p in SUBJECT_KINDS)):
        raise typer.BadParameter(f"{who!r} is not a whole usr_ or grp_ id", param_hint="WHO")
    if "@" in who:
        if not (MIN_EMAIL <= len(who) <= MAX_EMAIL and EMAIL.fullmatch(who)):
            raise typer.BadParameter(f"{who!r} is not an email address", param_hint="WHO")
        return Lookup("user", who)
    if not 1 <= len(who) <= MAX_GROUP_NAME:
        raise typer.BadParameter(
            f"a group name has 1 to {MAX_GROUP_NAME} characters", param_hint="WHO"
        )
    return Lookup("group", who)

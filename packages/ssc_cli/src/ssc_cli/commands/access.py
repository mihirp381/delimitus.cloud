"""``ssc access explain``: why a person can or cannot open an app environment (SSC-021).

The API runs the gateway's own evaluator on the org's current sharing rules and names the grants
that decided it. The gateway applies the same answer once the next snapshot is published.
"""

import re
from typing import Annotated, Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.share import EMAIL, MAX_EMAIL, MIN_EMAIL, Env, EnvOpt, Lookup, resolve_subject
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import CliError
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import AccessGrantRow, AccessResult
from ssc_contracts.errors import ErrorCode

USER_ID: Final = re.compile(r"usr_[a-z0-9]{20}")

access_app = typer.Typer(
    name="access",
    help="Explain who can open an app.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _person(value: str | None) -> str | None:
    if value is None or USER_ID.fullmatch(value):
        return value
    if MIN_EMAIL <= len(value) <= MAX_EMAIL and EMAIL.fullmatch(value):
        return value
    raise typer.BadParameter("give a person's usr_ id or email address")


PersonArg = Annotated[
    str | None,
    typer.Argument(
        metavar="[PERSON]",
        callback=_person,
        help="A usr_ id or an email address. Default: you.",
        show_default=False,
    ),
]


@access_app.command("explain")
def explain(
    ctx: typer.Context,
    app: AppArg,
    person: PersonArg = None,
    env: EnvOpt = Env.prod,
    json_mode: JsonOpt = False,
) -> None:
    """Say whether a person can open an app environment, and which grants decide it."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        where = environment(target, env.value)
        user_id = person
        if person is not None and not USER_ID.fullmatch(person):
            try:
                user_id = resolve_subject(client, Lookup("user", person), env, active_only=False).id
            except CliError as e:
                if e.body.code == ErrorCode.FORBIDDEN:
                    e.fix = (
                        "only an org admin can look people up by email; give the person's usr_ "
                        "id instead (their `ssc whoami` shows it as subject)."
                    )
                raise
        try:
            out = client.explain_access(target.id, where.id, user_id)
        except CliError as e:
            if e.body.code == ErrorCode.FORBIDDEN:
                e.fix = (
                    f"only an org admin, the app's owner or a builder on {env.value} can see who "
                    f"can open {env.value}."
                )
            raise
    result = AccessResult(
        app_id=target.id,
        slug=target.slug,
        environment=where.name,
        environment_id=where.id,
        user_id=out.user_id,
        allowed=out.allowed,
        role=out.role,
        floor=out.floor,
        reason=out.reason,
        grants=[
            AccessGrantRow(
                grant_id=g.grant_id,
                role=g.role,
                subject_kind=g.subject_kind,
                subject_id=g.subject_id,
                group_name=g.group_name,
            )
            for g in out.grants
        ],
        evaluated_from=out.evaluated_from,
        published_version=out.published_version,
    )
    if json_mode:
        print_json(result)
        return
    say(_sentence(result))
    if result.grants:
        say(
            table(
                ("ROLE", "KIND", "SUBJECT", "GROUP", "GRANT"),
                [
                    (g.role, g.subject_kind, dash(g.subject_id), dash(g.group_name), g.grant_id)
                    for g in result.grants
                ],
            )
        )
    published = dash(None if result.published_version is None else str(result.published_version))
    say(
        f"Worked out from the current sharing rules; the gateway follows them from the next "
        f"snapshot (newest published: {published})."
    )


def _sentence(r: AccessResult) -> str:
    who, where = r.user_id, f"{r.environment} of {r.slug}"
    match r.reason:
        case "granted":
            return f"{who} can open {where} as {r.role}, through the grants below."
        case "below_floor":
            return (
                f"{who} cannot open {where}: {r.environment} takes {r.floor} grants or higher, "
                "and the grants below are lower."
            )
        case "no_grant":
            return f"{who} cannot open {where}: no grant names them, a group of theirs or the org."
        case "app_not_active":
            return f"{who} cannot open {where}: the app is stopped, so no one can."
        case "user_not_active":
            return f"{who} cannot open {where}: they are deactivated."
        case _:
            verdict = "can" if r.allowed else "cannot"
            return f"{who} {verdict} open {where} ({r.reason})."

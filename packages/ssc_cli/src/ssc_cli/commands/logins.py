"""``ssc logins``: the logins no person could be found for, and link one to a person (SSC-019,
decision 024, GA-2.5).

A login lands here when its identity provider subject matches no linked person and its email
matches no one, or more than one. An org admin links it to an active person by ``usr_`` id or
email; that person's next login with it signs them in. An address-shaped subject (Google SAML)
cannot be linked: fix the person in the directory instead.
"""

from typing import Annotated

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import SUBJECT_AMBIGUOUS, USER_NOT_FOUND, ExitCode, local_error
from ssc_cli.output import print_json, say, table
from ssc_cli.shapes import LoginLinkedResult, UnlinkedLoginRow, UnlinkedLoginsResult

REASONS = {"no_match": "no one with this email", "ambiguous_email": "several people, same email"}

logins_app = typer.Typer(
    name="logins",
    help="Logins no person could be found for: list them, or link one to a person.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@logins_app.command("list")
def list_logins(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """List unlinked logins, newest first. Org admins only."""
    with handled(json_mode), session(ctx).client() as client:
        listed = client.list_unlinked_logins()
        result = UnlinkedLoginsResult(
            api_url=client.api_url,
            unlinked_logins=[
                UnlinkedLoginRow(
                    id=u.id,
                    email=u.email,
                    reason=u.reason,
                    attempts=u.attempts,
                    last_seen_at=u.last_seen_at,
                    linkable=u.linkable,
                )
                for u in listed.unlinked_logins
            ],
        )
    if json_mode:
        print_json(result)
        return
    if not result.unlinked_logins:
        say("Every login so far matched a person.")
        return
    say(
        table(
            ("ID", "EMAIL", "WHY", "TRIES", "LAST SEEN", "LINKABLE"),
            [
                (
                    u.id,
                    u.email,
                    REASONS.get(u.reason, u.reason),
                    str(u.attempts),
                    u.last_seen_at,
                    "yes" if u.linkable else "no: fix it in the directory",
                )
                for u in result.unlinked_logins
            ],
        )
    )
    say("\nLink one with `ssc logins link <ID> --to <email or usr_ id>`.")


def _person(client: ApiClient, who: str) -> str:
    if who.startswith("usr_"):
        return who
    found = [u for u in client.find_users(who).users if u.status == "active"]
    if not found:
        raise local_error(
            USER_NOT_FOUND, "No such person.", f"No active person in the org has the address {who}."
        )
    if len(found) > 1:
        ids = ", ".join(f"{u.id} ({u.display_name})" for u in found)
        raise local_error(
            SUBJECT_AMBIGUOUS,
            "More than one person fits.",
            f"{len(found)} active people have the address {who}: {ids}. Give the usr_ id.",
            ExitCode.USAGE,
        )
    return found[0].id


@logins_app.command("link")
def link(
    ctx: typer.Context,
    unlinked_login_id: Annotated[str, typer.Argument(metavar="ID", help="From `ssc logins list`.")],
    to: Annotated[str, typer.Option("--to", help="The active person: email or usr_ id.")],
    json_mode: JsonOpt = False,
) -> None:
    """Link a login to an active person; their next login with it signs them in."""
    with handled(json_mode), session(ctx).client() as client:
        user_id = _person(client, to.strip())
        done = client.link_unlinked_login(unlinked_login_id, user_id)
        result = LoginLinkedResult(
            api_url=client.api_url,
            unlinked_login_id=unlinked_login_id,
            user_id=done.user_id,
            identity_link_id=done.identity_link_id,
        )
    if json_mode:
        print_json(result)
    else:
        say(f"Linked {unlinked_login_id} to {result.user_id}. Their next login signs them in.")

"""``ssc apps`` and ``ssc apps create``."""

from typing import Annotated

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.status import app_result
from ssc_cli.output import print_json, say, table
from ssc_cli.shapes import AppRow, AppsResult

apps_app = typer.Typer(
    name="apps",
    help="List the apps you can see, or create one.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@apps_app.callback(invoke_without_command=True)
def apps(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """List the apps you can see."""
    if ctx.invoked_subcommand is not None:
        return
    with handled(json_mode), session(ctx).client() as client:
        listed = client.list_apps()
    result = AppsResult(
        apps=[
            AppRow(id=a.id, slug=a.slug, owner_user_id=a.owner_user_id, status=a.status)
            for a in listed.apps
        ]
    )
    if json_mode:
        print_json(result)
    elif not result.apps:
        say("No apps yet. Create one with `ssc apps create <slug>`.")
    else:
        say(
            table(
                ("SLUG", "ID", "STATUS", "OWNER"),
                [(a.slug, a.id, a.status, a.owner_user_id) for a in result.apps],
            )
        )


@apps_app.command("create")
def create(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(help="Lower-case name used in the app's address.")],
    json_mode: JsonOpt = False,
) -> None:
    """Create an app with a prod and a preview environment."""
    with handled(json_mode), session(ctx).client() as client:
        result = app_result(client, client.create_app(slug), with_deployments=False)
    if json_mode:
        print_json(result)
    else:
        envs = ", ".join(e.name for e in result.environments)
        say(f"Created {result.slug} ({result.id}) with environments {envs}.")

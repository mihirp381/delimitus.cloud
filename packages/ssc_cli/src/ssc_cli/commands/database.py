"""``ssc database rotate``: a new password for an environment's database login.

The new value goes straight into the cell's secrets and is never shown; a deployment of the live
release puts it live.
"""

from typing import Annotated

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.share import Env
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import CliError
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import DatabaseRotateResult
from ssc_cli.wait import DEFAULT_TIMEOUT, Budget, wait_for_operation
from ssc_contracts.errors import ErrorCode

database_app = typer.Typer(
    name="database",
    help="An app's database. Its password and URL are never shown.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)

EnvOpt = Annotated[Env, typer.Option("--env", help="Which environment.")]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Wait until the new password is live.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]


@database_app.command("rotate")
def rotate(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    env: EnvOpt,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Give an environment's database login a new password."""
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        target = resolve_app(client, app)
        where = environment(target, env.value)
        try:
            out = client.rotate_database(target.id, where.id)
        except CliError as e:
            if e.body.code == ErrorCode.DEPLOYMENT_IN_FLIGHT:
                e.fix = (
                    f"Another deployment is running in {where.name}. Wait for it "
                    f"(`ssc status {target.slug}`), then run this again."
                )
            raise
        state = "pending" if out.operation_id else None
        if wait and out.operation_id:
            state = wait_for_operation(
                client,
                out.operation_id,
                sleep=s.sleep,
                budget=Budget(timeout),
                next_step=f"Follow it with `ssc status {target.slug}`.",
            ).state
    result = DatabaseRotateResult(
        app_id=target.id,
        slug=target.slug,
        environment=where.name,
        environment_id=where.id,
        rotated_at=out.rotated_at,
        operation_id=out.operation_id,
        state=state,
    )
    if json_mode:
        print_json(result)
        return
    rotated = f"Rotated the {where.name} database password of {target.slug}"
    if out.operation_id is None:
        say(f"{rotated}. Nothing runs there, so its next deployment takes the new password.")
    elif state == "healthy":
        say(f"{rotated}; it is live.")
    else:
        say(f"{rotated}; deployment {out.operation_id} puts it live.")
        say(f"Follow it with `ssc status {target.slug}`.")

"""``ssc secret set`` and ``ssc secret list``: an app's secrets, whose values never reach the API
(SSC-026).

``set`` reads the value from stdin, or from a hidden prompt at a terminal, never from the command
line. It asks the API for a grant, PUTs the value straight to the cell's secret intake, then
records the version the intake answered; a deployment then puts it live. There is no ``secret
get``: nothing in SSC hands a value back, and only the app's own environment can read it.
"""

from typing import Annotated, Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.share import Env
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import BAD_SECRET_INPUT, CliError, ExitCode, local_error
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import SecretRow, SecretSetResult, SecretsResult
from ssc_cli.wait import DEFAULT_TIMEOUT, Budget, wait_for_operation
from ssc_contracts.app_env import secret_name_problem
from ssc_contracts.errors import ErrorCode

MAX_VALUE_BYTES: Final = 64 * 1024

secret_app = typer.Typer(
    name="secret",
    help="Set an app's secrets. Values go straight to its cell and are never shown again.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _name(value: str) -> str:
    problem = secret_name_problem(value)
    if problem is not None:
        raise typer.BadParameter(f"{value} {problem}")
    return value


NameArg = Annotated[
    str,
    typer.Argument(
        metavar="NAME",
        callback=_name,
        help="The secret's name, which is also the environment variable the app reads.",
    ),
]
EnvOpt = Annotated[Env, typer.Option("--env", help="Which environment.")]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Wait until the new version is live.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]


@secret_app.command("set")
def set_secret(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    name: NameArg,
    env: EnvOpt,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Read a secret's value from stdin and store it as the secret's next version."""
    s = session(ctx)
    with handled(json_mode):
        value = _read_value(name)
        with s.client() as client:
            target = resolve_app(client, app)
            where = environment(target, env.value)
            try:
                granted = client.grant_secret_upload(target.id, where.id, name)
            except CliError as e:
                _explain(e, env)
                raise
            version = client.upload_secret(granted.upload, value)
            del value
            out = client.set_secret(target.id, where.id, name, version)
            state = "pending" if out.operation_id else None
            if wait and out.operation_id:
                state = wait_for_operation(
                    client,
                    out.operation_id,
                    sleep=s.sleep,
                    budget=Budget(timeout),
                    next_step=f"Follow it with `ssc status {target.slug}`.",
                ).state
    result = SecretSetResult(
        app_id=target.id,
        slug=target.slug,
        environment=where.name,
        environment_id=where.id,
        name=name,
        version=out.version,
        changed=out.changed,
        operation_id=out.operation_id,
        state=state,
    )
    if json_mode:
        print_json(result)
        return
    if not out.changed:
        say(f"{name} in {where.name} of {target.slug} already has version {out.version}.")
    elif out.operation_id is None:
        say(f"Stored {name} version {out.version} for {where.name} of {target.slug}.")
        say("The next deployment of that environment puts it live.")
    elif state == "healthy":
        say(f"{name} version {out.version} is live in {where.name} of {target.slug}.")
    else:
        say(f"Stored {name} version {out.version}; deploying it ({out.operation_id}).")
        say(f"Follow it with `ssc status {target.slug}`.")


@secret_app.command("list")
def list_secrets(ctx: typer.Context, app: AppArg, env: EnvOpt, json_mode: JsonOpt = False) -> None:
    """List an environment's secrets: names and versions, never values."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        where = environment(target, env.value)
        page = client.list_secrets(target.id, where.id)
    rows = [
        SecretRow(
            name=s.name, version=s.version, live_version=s.live_version, updated_at=s.updated_at
        )
        for s in page.items
    ]
    result = SecretsResult(
        app_id=target.id,
        slug=target.slug,
        environment=where.name,
        environment_id=where.id,
        secrets=rows,
    )
    if json_mode:
        print_json(result)
        return
    if not rows:
        say(f"{where.name} of {target.slug} has no secrets.")
        return
    say(
        table(
            ["NAME", "VERSION", "LIVE", "UPDATED"],
            [[r.name, r.version, dash(r.live_version), r.updated_at] for r in rows],
        )
    )


def _read_value(name: str) -> bytes:
    stdin = typer.get_text_stream("stdin")
    if stdin.isatty():
        text = typer.prompt(
            f"Value for {name}", hide_input=True, confirmation_prompt=True, default=""
        )
        raw = text.encode()
    else:
        raw = typer.get_binary_stream("stdin").read(MAX_VALUE_BYTES + 3)
        raw = raw.removesuffix(b"\n").removesuffix(b"\r")
    if not raw:
        raise _bad_input(f"Pipe the value of {name} into `ssc secret set`; it was empty.")
    if len(raw) > MAX_VALUE_BYTES:
        raise _bad_input(f"A secret is at most {MAX_VALUE_BYTES} bytes.")
    return raw


def _bad_input(detail: str) -> CliError:
    return local_error(BAD_SECRET_INPUT, "That is not a secret value.", detail, ExitCode.USAGE)


def _explain(e: CliError, env: Env) -> None:
    if e.body.code == ErrorCode.AGENT_SESSION_REFUSED:
        e.fix = "a person sets secrets from their own sign-in (`ssc login`), never an agent."
    elif e.body.code == ErrorCode.FORBIDDEN:
        e.fix = f"only an org admin, the app's owner or a builder on {env.value} sets secrets."
    elif e.body.code == ErrorCode.SECRETS_UNAVAILABLE:
        e.fix = "retry in a few minutes; nothing was stored."

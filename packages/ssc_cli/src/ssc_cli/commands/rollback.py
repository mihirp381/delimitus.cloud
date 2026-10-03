"""``ssc rollback``: deploy an earlier release again.

A rollback keeps the environment's current sharing and secrets and never touches schedules
(decision 014). The environment defaults to the one the release was built for, since a release
built for one environment is refused in another.

A rollback does not undo database migrations. When the environment's database may have run
migrations the release lacks, the API refuses with ``SCHEMA_AHEAD`` until ``--confirm`` (SSC-043);
its problem carries fixed text, so the migrations are read from ``migrations-ahead`` and named in
the ``Fix:`` line.
"""

import re
from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import (
    ENVIRONMENT_NOT_FOUND,
    ENVIRONMENT_REQUIRED,
    RELEASE_NOT_FOUND,
    CliError,
    ExitCode,
    local_error,
)
from ssc_cli.models import AppOut, EnvironmentOut, OperationAccepted, ReleaseOut
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import RollbackResult
from ssc_cli.wait import DEFAULT_TIMEOUT, Budget, wait_for_operation
from ssc_contracts.errors import ErrorCode

ROLLBACK: Final = "rollback"
RELEASE_PREFIX: Final = "rel_"
NUMBER: Final = re.compile(r"[Rr]?([1-9][0-9]{0,8})")


def _release_ref(value: str) -> str:
    if value.startswith(RELEASE_PREFIX) or NUMBER.fullmatch(value):
        return value
    raise typer.BadParameter("give a release as R<number>, <number> or a rel_ id")


AppArg = Annotated[str, typer.Argument(help="App slug or app_ id.")]
ReleaseArg = Annotated[
    str,
    typer.Argument(
        metavar="RELEASE", callback=_release_ref, help="R<number>, <number> or a rel_ id."
    ),
]
EnvOpt = Annotated[
    str | None,
    typer.Option(
        "--env", metavar="NAME", help="Default: the environment the release was built for."
    ),
]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Wait until the release is live again.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]
ConfirmOpt = Annotated[
    bool,
    typer.Option(
        "--confirm",
        help="Roll back although the database has migrations the release does not have.",
    ),
]


def rollback(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    release: ReleaseArg,
    env: EnvOpt = None,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    confirm: ConfirmOpt = False,
    json_mode: JsonOpt = False,
) -> None:
    """Deploy an earlier release of an app again."""
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        target = resolve_app(client, app)
        rel = _find_release(client, target, release)
        where = environment(target, env) if env is not None else _built_for(target, rel)
        op = _start(client, target, where, rel, confirm=confirm)
        state = op.state
        if wait:
            state = wait_for_operation(
                client,
                op.operation_id,
                sleep=s.sleep,
                budget=Budget(timeout),
                next_step=f"Follow it with `ssc status {target.slug}`.",
            ).state
    result = RollbackResult(
        app_id=target.id,
        slug=target.slug,
        environment=where.name,
        environment_id=where.id,
        release_id=rel.release_id,
        release_number=rel.number,
        operation_id=op.operation_id,
        state=state,
        url=where.url,
    )
    if json_mode:
        print_json(result)
        return
    if state == "healthy":
        say(f"{where.name} of {target.slug} runs {rel.label} again.")
    else:
        say(f"Rolling {where.name} of {target.slug} back to {rel.label} ({op.operation_id}).")
        say(f"Follow it with `ssc status {target.slug}`.")
    if where.url:
        say(f"URL: {where.url}")


def _start(
    client: ApiClient, app: AppOut, where: EnvironmentOut, rel: ReleaseOut, *, confirm: bool
) -> OperationAccepted:
    """Post the rollback; on ``SCHEMA_AHEAD``, name the migrations in the fix."""
    try:
        return client.create_deployment(app.id, where.id, rel.release_id, ROLLBACK, confirm=confirm)
    except CliError as e:
        if e.body.code == ErrorCode.SCHEMA_AHEAD:
            ahead = client.migrations_ahead(app.id, where.id, rel.release_id).ledgers
            names = "; ".join(f"{a.ledger}: {', '.join(a.names)}" for a in ahead)
            e.fix = (
                f"The {where.name} database may have run migrations {rel.label} does not have "
                f"({names or 'none listed'}). If {rel.label} works with them, run "
                f"`ssc rollback {app.slug} {rel.label} --env {where.name} --confirm`; otherwise "
                "deploy a fix forward."
            )
        raise


def _find_release(client: ApiClient, app: AppOut, ref: str) -> ReleaseOut:
    if ref.startswith(RELEASE_PREFIX):
        return client.get_release(app.id, ref)
    match = NUMBER.fullmatch(ref)
    number = int(match.group(1)) if match else 0
    page = client.list_releases(app.id, limit=1, before=number + 1)
    if page.items and page.items[0].number == number:
        return page.items[0]
    raise local_error(
        RELEASE_NOT_FOUND,
        "No such release.",
        f"App {app.slug} has no release R{number}. Run `ssc releases {app.slug}` to list them.",
    )


def _built_for(app: AppOut, release: ReleaseOut) -> EnvironmentOut:
    if release.built_for_environment_id is None:
        raise local_error(
            ENVIRONMENT_REQUIRED,
            "Say which environment to roll back.",
            f"{release.label} was not made by a build for one environment. Pass --env.",
            ExitCode.USAGE,
        )
    for e in app.environments:
        if e.id == release.built_for_environment_id:
            return e
    raise local_error(
        ENVIRONMENT_NOT_FOUND,
        "No such environment.",
        f"{release.label} was built for {release.built_for_environment_id}, which app "
        f"{app.slug} does not have.",
    )

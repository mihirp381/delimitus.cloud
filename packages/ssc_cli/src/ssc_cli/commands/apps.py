"""``ssc apps``, ``ssc apps create``, ``ssc apps delete`` and ``ssc apps delete-status``.

``delete`` removes an app for good (GA-12): an org admin names it and types its slug again with
``--confirm``. The app is stopped as soon as the API answers and stays in the list as
``deleting`` while the steps run; once the last one has ended it is gone and its slug is free. A
step that fails ends the run there, with everything before it removed: ``delete`` again
continues. ``delete-status`` reads one run, also after the app is gone.
"""

from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.lifecycle import DEFAULT_TIMEOUT, TimeoutOpt
from ssc_cli.commands.status import AppArg, app_result
from ssc_cli.errors import (
    APP_DELETE_FAILED,
    APP_NOT_FOUND,
    WAIT_TIMED_OUT,
    CliError,
    ErrorBody,
    ExitCode,
)
from ssc_cli.models import AppDeleteRun
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import APP_PREFIX, resolve_app
from ssc_cli.shapes import (
    AppDeleteResult,
    AppDeleteRunResult,
    AppDeleteStepRow,
    AppRow,
    AppsResult,
)
from ssc_cli.wait import Budget
from ssc_contracts.errors import ErrorCode

DELETED: Final = "deleted"
DELETING: Final = "deleting"
STEP_HEADERS: Final = ("STEP", "STATE", "REMOVED", "MS", "TRIES", "ERROR")

ConfirmOpt = Annotated[
    str,
    typer.Option(
        "--confirm", metavar="SLUG", help="The app's slug again: what is about to be deleted."
    ),
]
RunArg = Annotated[str, typer.Argument(help="The run id `ssc apps delete` printed.")]
GoneAppArg = Annotated[str, typer.Argument(help="App slug, or its app_ id once the app is gone.")]

apps_app = typer.Typer(
    name="apps",
    help="List the apps you can see, create one, or delete one.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@apps_app.callback(invoke_without_command=True)
def apps(
    ctx: typer.Context,
    mine: Annotated[
        bool,
        typer.Option(
            "--mine", help="Only the apps you can deploy to: as an org admin, owner or builder."
        ),
    ] = False,
    json_mode: JsonOpt = False,
) -> None:
    """List the apps you can see."""
    if ctx.invoked_subcommand is not None:
        return
    with handled(json_mode), session(ctx).client() as client:
        listed = client.list_apps(mine=mine)
    result = AppsResult(
        apps=[
            AppRow(id=a.id, slug=a.slug, owner_user_id=a.owner_user_id, status=a.status)
            for a in listed.apps
        ]
    )
    if json_mode:
        print_json(result)
    elif not result.apps and mine:
        say("You cannot deploy to any app yet. Create one with `ssc apps create <slug>`.")
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


@apps_app.command("delete")
def delete(
    ctx: typer.Context,
    app: AppArg,
    confirm: ConfirmOpt,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Delete an app for good, with all it holds (org admins)."""
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        target = resolve_app(client, app)
        try:
            accepted = client.delete_app(target.id, confirm)
        except CliError as e:
            e.fix = _delete_fix(e.body.code, target.slug)
            raise
        run = _follow(client, target.id, accepted.run_id, sleep=s.sleep, budget=Budget(timeout))
        steps = _rows(run)
        if run.state == "failed":
            if not json_mode:
                say(_table(steps))
            raise _failed(run, target.slug)
    result = AppDeleteResult(
        app_id=target.id,
        slug=target.slug,
        status=DELETED,
        run_id=run.run_id,
        state=run.state,
        steps=steps,
        total_ms=run.total_ms,
    )
    if json_mode:
        print_json(result)
        return
    say(f"{target.slug} is deleted: its address no longer answers and its places are free.")
    say(_table(steps))
    say(f"Delete run {run.run_id} {run.state} in {_num(run.total_ms)} ms.")
    say(f"Read it again with `ssc apps delete-status {target.id} {run.run_id}`.")


@apps_app.command("delete-status")
def delete_status(
    ctx: typer.Context, app: GoneAppArg, run_id: RunArg, json_mode: JsonOpt = False
) -> None:
    """Show one removal of an app, step by step (org admins)."""
    with handled(json_mode), session(ctx).client() as client:
        run = client.get_app_delete_run(_app_id(client, app), run_id)
    result = AppDeleteRunResult(
        app_id=run.app_id,
        run_id=run.run_id,
        state=run.state,
        steps=_rows(run),
        started_at=run.started_at,
        finished_at=run.finished_at,
        total_ms=run.total_ms,
    )
    if json_mode:
        print_json(result)
        return
    say(f"Delete run {result.run_id} of {result.app_id}: {result.state}.")
    say(_table(result.steps))
    say(f"Started {result.started_at}, finished {dash(result.finished_at)}.")


def _app_id(client: ApiClient, ref: str) -> str:
    """The app's id. An ``app_`` id is taken as it is: a deleted app answers no lookup."""
    if ref.startswith(APP_PREFIX):
        return ref
    try:
        return resolve_app(client, ref).id
    except CliError as e:
        if e.body.code == APP_NOT_FOUND:
            e.fix = (
                "a deleted app has no slug any more; give the app_ id `ssc apps delete` printed."
            )
        raise


def _rows(run: AppDeleteRun) -> list[AppDeleteStepRow]:
    return [
        AppDeleteStepRow(
            name=st.name,
            state=st.state,
            removed=st.removed,
            elapsed_ms=st.elapsed_ms,
            attempts=st.attempts,
            error=st.error,
        )
        for st in run.steps
    ]


def _table(steps: list[AppDeleteStepRow]) -> str:
    return table(
        STEP_HEADERS,
        [
            (
                st.name,
                st.state,
                _num(st.removed),
                _num(st.elapsed_ms),
                str(st.attempts),
                dash(st.error),
            )
            for st in steps
        ],
    )


def _num(value: int | None) -> str:
    return dash(None if value is None else str(value))


def _follow(
    client: ApiClient, app_id: str, run_id: str, *, sleep: Sleep, budget: Budget
) -> AppDeleteRun:
    """The run once it has ended, ``completed`` or ``failed``."""
    instance = f"/v1/apps/{app_id}/delete/{run_id}"
    reread = f"read it with `ssc apps delete-status {app_id} {run_id}`."
    while True:
        try:
            run = client.get_app_delete_run(app_id, run_id)
        except CliError as e:
            body = e.body.model_copy(
                update={
                    "detail": f"{e.body.detail} The app is stopped already and denies every "
                    f"request; delete run {run_id} carries on without ssc.",
                    "instance": e.body.instance or instance,
                }
            )
            raise CliError(body, e.exit_code, fix=e.fix or reread) from e
        if run.state != "running":
            return run
        if budget.used_up:
            body = ErrorBody(
                code=WAIT_TIMED_OUT,
                title="Stopped following the removal.",
                detail=f"Run {run_id} was still running after {budget.seconds:g} seconds and "
                "carries on without ssc. The app is stopped already and denies every request.",
                status=None,
                instance=instance,
            )
            raise CliError(body, ExitCode.FAILED, fix=reread)
        budget.sleep(sleep)


def _failed(run: AppDeleteRun, slug: str) -> CliError:
    failed = [
        f"{st.name} ({st.error or 'no reason given'})" for st in run.steps if st.state == "failed"
    ]
    body = ErrorBody(
        code=APP_DELETE_FAILED,
        title="A step of the removal failed.",
        detail=f"{slug} stays stopped and denies every request, and what the earlier steps "
        f"removed stays removed, but run {run.run_id} ended failed: "
        f"{', '.join(failed) or 'no step says why'}.",
        status=None,
        instance=f"/v1/apps/{run.app_id}/delete/{run.run_id}",
    )
    fix = (
        f"run `ssc apps delete {slug} --confirm {slug}` again to continue from the failed step; "
        f"`ssc apps delete-status {run.app_id} {run.run_id}` shows this run."
    )
    return CliError(body, ExitCode.FAILED, fix=fix)


def _delete_fix(code: str, slug: str) -> str | None:
    if code == ErrorCode.FORBIDDEN:
        return "only an active org admin can delete an app; ask one to do it."
    if code == ErrorCode.AGENT_SESSION_REFUSED:
        return "an org admin deletes an app from their own sign-in (`ssc login`), never an agent."
    if code == ErrorCode.CONFIRMATION_MISMATCH:
        return f"give the app's slug exactly: `ssc apps delete {slug} --confirm {slug}`."
    if code == ErrorCode.APP_OPERATION_IN_FLIGHT:
        return (
            f"a deploy, promote or rollback of {slug} is still running; run this again once "
            f"`ssc status {slug}` shows it has ended."
        )
    if code == ErrorCode.KILL_SWITCH_IN_FLIGHT:
        return (
            f"the kill switch or an earlier removal of {slug} is still running; run this again "
            "once it ends."
        )
    return None

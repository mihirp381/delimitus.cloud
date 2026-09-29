"""``ssc disable`` and ``ssc enable``: an org admin stops an app with the kill switch, or puts
it back (SSC-025).

The app is stopped as soon as the API answers ``disable``: from then on it denies every request,
whatever happens to the steps that follow. The command then follows the kill switch run until
every step has ended. ``--quarantine`` also freezes the app's sharing rules. ``enable`` makes a
stopped app active again, and the platform starts it back up.
"""

from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.status import AppArg, app_result
from ssc_cli.errors import KILL_SWITCH_FAILED, WAIT_TIMED_OUT, CliError, ErrorBody, ExitCode
from ssc_cli.models import KillSwitchRun
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import resolve_app
from ssc_cli.shapes import DisableResult, KillSwitchStepRow
from ssc_cli.wait import Budget
from ssc_contracts.errors import ErrorCode

DEFAULT_TIMEOUT: Final = 300
MODE_STATUS: Final = {"disable": "disabled", "quarantine": "quarantined"}
ADMINS_ONLY: Final = "only an active org admin can stop or enable an app; ask one to do it."

QuarantineOpt = Annotated[
    bool,
    typer.Option("--quarantine", help="Also freeze the app's sharing rules until it is enabled."),
]
TimeoutOpt = Annotated[
    int,
    typer.Option(
        "--timeout", min=1, metavar="SECONDS", help="Give up following the steps after this."
    ),
]


def disable(
    ctx: typer.Context,
    app: AppArg,
    quarantine: QuarantineOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Stop an app now with the kill switch (org admins)."""
    mode = "quarantine" if quarantine else "disable"
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        target = resolve_app(client, app)
        try:
            accepted = client.pull_kill_switch(target.id, mode)
        except CliError as e:
            e.fix = _disable_fix(e.body.code, target.slug, mode, target.status)
            raise
        run = _follow(client, target.id, accepted.run_id, sleep=s.sleep, budget=Budget(timeout))
    result = DisableResult(
        app_id=target.id,
        slug=target.slug,
        mode=mode,
        status=MODE_STATUS[mode],
        run_id=run.run_id,
        state=run.state,
        steps=[
            KillSwitchStepRow(
                name=st.name,
                state=st.state,
                elapsed_ms=st.elapsed_ms,
                attempts=st.attempts,
                error=st.error,
            )
            for st in run.steps
        ],
        total_ms=run.total_ms,
    )
    if json_mode:
        print_json(result)
        return
    say(f"{target.slug} is {result.status} and denies every request.")
    say(
        table(
            ("STEP", "STATE", "MS", "TRIES", "ERROR"),
            [
                (st.name, st.state, _num(st.elapsed_ms), str(st.attempts), dash(st.error))
                for st in result.steps
            ],
        )
    )
    say(f"Kill switch run {run.run_id} {run.state} in {_num(run.total_ms)} ms.")
    say(f"Undo with `ssc enable {target.slug}`.")


def enable(ctx: typer.Context, app: AppArg, json_mode: JsonOpt = False) -> None:
    """Make a disabled or quarantined app active again (org admins)."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        try:
            out = client.enable_app(target.id)
        except CliError as e:
            e.fix = _enable_fix(e.body.code, target.slug)
            raise
        result = app_result(client, out, with_deployments=False)
    if json_mode:
        print_json(result)
        return
    say(f"{result.slug} is {result.status} again; its environments start back up shortly.")
    say(f"Follow it with `ssc status {result.slug}`.")


def _num(value: int | None) -> str:
    return dash(None if value is None else str(value))


def _follow(
    client: ApiClient, app_id: str, run_id: str, *, sleep: Sleep, budget: Budget
) -> KillSwitchRun:
    instance = f"/v1/apps/{app_id}/kill-switch/{run_id}"
    while True:
        try:
            run = client.get_kill_switch_run(app_id, run_id)
        except CliError as e:
            raise _still_stopped(e, run_id, instance) from e
        if run.state == "failed":
            failed = [
                f"{st.name} ({st.error or 'no reason given'})"
                for st in run.steps
                if st.state == "failed"
            ]
            body = ErrorBody(
                code=KILL_SWITCH_FAILED,
                title="A kill switch step failed.",
                detail=f"The app stays stopped and denies every request, but run {run_id} "
                f"ended failed: {', '.join(failed) or 'no step says why'}.",
                status=None,
                instance=instance,
            )
            raise CliError(body, ExitCode.FAILED)
        if run.state != "running":
            return run
        if budget.used_up:
            body = ErrorBody(
                code=WAIT_TIMED_OUT,
                title="Stopped following the kill switch.",
                detail=f"Run {run_id} was still running after {budget.seconds:g} seconds and "
                "carries on without ssc. The app is stopped already and denies every request.",
                status=None,
                instance=instance,
            )
            raise CliError(body, ExitCode.FAILED)
        budget.sleep(sleep)


def _still_stopped(e: CliError, run_id: str, instance: str) -> CliError:
    """``e``, raised while following a run, saying the app is stopped already and naming the run."""
    body = e.body.model_copy(
        update={
            "detail": f"{e.body.detail} The app is stopped already and denies every request; "
            f"kill switch run {run_id} carries on without ssc.",
            "instance": e.body.instance or instance,
        }
    )
    return CliError(body, e.exit_code, fix=e.fix)


def _disable_fix(code: str, slug: str, mode: str, status: str) -> str | None:
    if code == ErrorCode.FORBIDDEN:
        return ADMINS_ONLY
    if code == ErrorCode.APP_NOT_ACTIVE and "quarantined" in {status, MODE_STATUS[mode]}:
        return f"{slug} is already quarantined; `ssc enable {slug}` undoes it."
    if code == ErrorCode.APP_NOT_ACTIVE and status == "disabled":
        return f"{slug} is disabled already; `ssc disable {slug} --quarantine` quarantines it too."
    if code == ErrorCode.APP_NOT_ACTIVE:
        return f"{slug} is stopped already; `ssc status {slug}` shows how."
    if code == ErrorCode.KILL_SWITCH_IN_FLIGHT:
        return "an earlier pull of the kill switch is still running; run this again once it ends."
    return None


def _enable_fix(code: str, slug: str) -> str | None:
    if code == ErrorCode.FORBIDDEN:
        return ADMINS_ONLY
    if code == ErrorCode.APP_ALREADY_ACTIVE:
        return f"{slug} is active already; there is nothing to enable."
    if code == ErrorCode.KILL_SWITCH_IN_FLIGHT:
        return f"the kill switch is still stopping {slug}; run this again once it ends."
    return None

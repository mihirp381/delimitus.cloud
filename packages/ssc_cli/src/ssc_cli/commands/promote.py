"""``ssc promote``: put in prod what preview runs now (SSC-042, decision 017).

Promote builds, for prod, the source of the release live in preview, then deploys that build's
release to prod like any forward deploy (behind the production gate). ``--wait`` does both and
waits until prod is live. Without it the command stops once the build has made its release and
names the command that puts it live: ``--build``, which waits for that prod build and deploys it.
Each POST carries its own ``Idempotency-Key``.
"""

import re
from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import BUILD_NOT_FOUND, CliError, local_error
from ssc_cli.models import AppOut, EnvironmentOut
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import PromoteResult
from ssc_cli.wait import DEFAULT_TIMEOUT, Budget, wait_for_build, wait_for_operation

PROD: Final = "prod"
PREVIEW: Final = "preview"
DEPLOY: Final = "deploy"
APPROVAL_REQUIRED: Final = "APPROVAL_REQUIRED"
BUILD_ID: Final = re.compile(r"bld_[a-z0-9]{20}")


def _build_id(value: str | None) -> str | None:
    if value is not None and not BUILD_ID.fullmatch(value):
        raise typer.BadParameter("give a bld_ id")
    return value


AppArg = Annotated[str, typer.Argument(help="App slug or app_ id.")]
BuildOpt = Annotated[
    str | None,
    typer.Option(
        "--build",
        metavar="BUILD_ID",
        callback=_build_id,
        help="Deploy the release of this earlier prod build instead of promoting again.",
    ),
]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Deploy to prod and wait until it is live.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]


def promote(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    build: BuildOpt = None,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = DEFAULT_TIMEOUT,
    json_mode: JsonOpt = False,
) -> None:
    """Build what preview runs for prod, then deploy it to prod."""
    s = session(ctx)
    budget = Budget(timeout)
    source: str | None = None
    op_id: str | None = None
    state: str | None = None
    with handled(json_mode), s.client() as client:
        target = resolve_app(client, app)
        prod = environment(target, PROD)
        if build is None:
            source = _live_release(client, environment(target, PREVIEW))
            build_id = client.promote(target.id, source).build_id
            if not json_mode:
                say(f"Building for prod what preview runs ({build_id}).")
        else:
            build_id = _prod_build(client, target, prod, build)
        resume = f"ssc promote {target.slug} --build {build_id} --wait"
        release_id, number = wait_for_build(
            client,
            build_id,
            sleep=s.sleep,
            budget=budget,
            next_step=f"Put its release live once it is built with `{resume}`.",
        )
        deploying = wait or build is not None
        if deploying:
            op = client.create_deployment(target.id, prod.id, release_id, DEPLOY)
            op_id, state = op.operation_id, op.state
            if wait:
                state = _wait_live(client, op.operation_id, s.sleep, budget, target.slug, resume)
    result = PromoteResult(
        app_id=target.id,
        slug=target.slug,
        environment=PROD,
        environment_id=prod.id,
        source_release_id=source,
        build_id=build_id,
        release_id=release_id,
        release_number=number,
        operation_id=op_id,
        state=state,
        url=prod.url,
        next_command=None if deploying else resume,
    )
    if json_mode:
        print_json(result)
        return
    if not deploying:
        say(f"Built R{number} for prod. Put it live with `{resume}`.")
        return
    if state == "healthy":
        say(f"prod of {target.slug} runs R{number}.")
    else:
        say(f"Deploying R{number} to prod ({op_id}). Follow it with `ssc status {target.slug}`.")
    if prod.url:
        say(f"Prod: {prod.url}")


def _wait_live(  # noqa: PLR0913, PLR0917
    client: ApiClient, op_id: str, sleep: Sleep, budget: Budget, slug: str, resume: str
) -> str:
    """The deployment's state once healthy. A deployment held for approval names ``resume``, which
    deploys this release again without another build."""
    try:
        return wait_for_operation(
            client,
            op_id,
            sleep=sleep,
            budget=budget,
            next_step=f"Follow it with `ssc status {slug}`.",
        ).state
    except CliError as e:
        if e.body.code == APPROVAL_REQUIRED:
            e.fix = (
                "Production needs approval first. An org admin other than you decides the open "
                f"approval requests for this app; then run `{resume}`, which deploys this release "
                "without building again."
            )
        raise


def _live_release(client: ApiClient, preview: EnvironmentOut) -> str | None:
    """The release preview runs healthy now; the API refuses the promote when there is none."""
    if preview.current_deployment_id is None:
        return None
    op = client.get_operation(preview.current_deployment_id)
    return op.release_id if op.state == "healthy" else None


def _prod_build(client: ApiClient, app: AppOut, prod: EnvironmentOut, build_id: str) -> str:
    found = client.get_build(build_id)
    if (found.app_id, found.environment_id) != (app.id, prod.id):
        raise local_error(
            BUILD_NOT_FOUND,
            "That build is not a prod build of this app.",
            f"Build {build_id} belongs to another app or environment.",
        )
    return build_id

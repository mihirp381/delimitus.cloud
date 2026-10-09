"""``ssc repo connect``, ``show`` and ``disconnect``: the GitHub repository whose pushes deploy an
app's preview (SSC-047, GA-7.3).

The repository must be reachable through a GitHub App installation an SSC operator bound to the
org; the API refuses ``REPOSITORY_NOT_INSTALLED`` otherwise, with the steps that fix it. Connect
replaces the whole link, so the required checks are exactly the ``--check`` options given. Values
are checked here as the API checks them, so a bad one is a usage error before any request; a test
holds these patterns to ``docs/api/openapi.json``. Connecting and disconnecting are a person's:
the API refuses an agent session.
"""

import re
from typing import Annotated, Final

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import CliError
from ssc_cli.models import RepoLinkIn, RepoLinkOut, RequiredCheckIn
from ssc_cli.output import print_json, say
from ssc_cli.resolve import resolve_app
from ssc_cli.shapes import RepoDisconnected, RepoResult, RequiredCheckRow
from ssc_contracts.errors import ErrorCode

REPOSITORY: Final = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
BRANCH: Final = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
WORKFLOW: Final = re.compile(r"^\.github/workflows/[A-Za-z0-9._/-]+\.ya?ml$")
CHECK_NAME: Final = re.compile(r"^[^\x00-\x1f\x7f]+$")
MAX_CHECKS: Final = 10
MAX_WORKFLOW: Final = 255
MAX_NAME: Final = 200

repo_app = typer.Typer(
    name="repo",
    help="Connect an app to a GitHub repository whose pushes deploy preview.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


def _repository(value: str) -> str:
    if REPOSITORY.fullmatch(value) is None:
        raise typer.BadParameter(f"{value!r} is not a GitHub repository as owner/name.")
    return value


def _branch(value: str | None) -> str | None:
    if value is not None and BRANCH.fullmatch(value) is None:
        raise typer.BadParameter(f"{value!r} is not a branch name.")
    return value


def parse_check(value: str) -> RequiredCheckIn:
    """``<workflow file> <check name>``, split at the first whitespace, as the console reads
    each line of its required checks."""
    line = value.strip()
    parts = line.split(maxsplit=1)
    workflow = parts[0] if parts else ""
    name = parts[1].strip() if len(parts) > 1 else ""
    if (
        WORKFLOW.fullmatch(workflow) is None
        or len(workflow) > MAX_WORKFLOW
        or CHECK_NAME.fullmatch(name) is None
        or len(name) > MAX_NAME
    ):
        raise typer.BadParameter(
            f'"{value}" is not a workflow file under .github/workflows and a check name.'
        )
    return RequiredCheckIn(name=name, workflow=workflow)


def _checks(values: list[str] | None) -> list[str] | None:
    if values and len(values) > MAX_CHECKS:
        raise typer.BadParameter(f"at most {MAX_CHECKS} --check options; got {len(values)}.")
    for value in values or ():
        parse_check(value)
    return values


RepositoryArg = Annotated[
    str,
    typer.Argument(
        metavar="REPOSITORY", callback=_repository, help="The GitHub repository, as owner/name."
    ),
]
BranchOpt = Annotated[
    str | None,
    typer.Option(
        "--branch",
        metavar="BRANCH",
        callback=_branch,
        help="The branch whose pushes deploy preview. Default: the repository's default branch.",
    ),
]
CheckOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--check",
        metavar="'WORKFLOW NAME'",
        callback=_checks,
        help="A check promote requires: the workflow file, a space, then the check's name as "
        "GitHub shows it, like '.github/workflows/ci.yml test'. Repeat for each, at most 10.",
    ),
]


@repo_app.command("connect")
def connect(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    repository: RepositoryArg,
    branch: BranchOpt = None,
    checks: CheckOpt = None,
    json_mode: JsonOpt = False,
) -> None:
    """Connect an app to a repository, or change its branch or required checks.

    Each call replaces the whole link: the branch is the repository's default branch unless
    --branch names one, and the required checks are exactly the --check options given, so
    leaving --check out clears them. Every push to the branch then deploys preview; prod still
    changes only through promote. Needs a builder on prod.
    """
    body = RepoLinkIn(
        repository=repository,
        branch=branch,
        required_checks=[parse_check(c) for c in checks or ()],
    )
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        try:
            link = client.connect_repository(target.id, body)
        except CliError as e:
            _explain(e, target.slug, change=True, link_needed=False)
            raise
    _report(link, target.slug, json_mode, f"Connected {target.slug} to")


@repo_app.command("show")
def show(ctx: typer.Context, app: AppArg, json_mode: JsonOpt = False) -> None:
    """Show the repository an app is connected to, its branch and the checks promote requires."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        try:
            link = client.get_repository(target.id)
        except CliError as e:
            _explain(e, target.slug, change=False, link_needed=True)
            raise
    _report(link, target.slug, json_mode, f"{target.slug} is connected to")


@repo_app.command("disconnect")
def disconnect(ctx: typer.Context, app: AppArg, json_mode: JsonOpt = False) -> None:
    """Disconnect an app's repository: pushes stop deploying preview and promote stops checking
    its required checks. What is deployed stays. Needs a builder on prod."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        try:
            client.disconnect_repository(target.id)
        except CliError as e:
            _explain(e, target.slug, change=True, link_needed=True)
            raise
    if json_mode:
        print_json(RepoDisconnected(app_id=target.id, slug=target.slug, disconnected=True))
        return
    say(
        f"Disconnected {target.slug} from its repository. "
        "Pushes no longer deploy preview; what is deployed stays."
    )


def _report(link: RepoLinkOut, slug: str, json_mode: bool, lead: str) -> None:
    rows = [RequiredCheckRow(workflow=c.workflow, name=c.name) for c in link.required_checks]
    if json_mode:
        print_json(
            RepoResult(
                app_id=link.app_id,
                slug=slug,
                repository=link.repository,
                repository_id=link.repository_id,
                branch=link.branch,
                required_checks=rows,
                check_name=link.check_name,
                updated_at=link.updated_at,
            )
        )
        return
    say(f"{lead} {link.repository}, branch {link.branch}.")
    say(
        f"Every push to {link.branch} deploys preview and reports on the commit as "
        f'"{link.check_name}".'
    )
    if not rows:
        say("No required checks.")
        return
    say("Required before promote:")
    for r in rows:
        say(f"  {r.workflow} {r.name}")


def _explain(e: CliError, slug: str, *, change: bool, link_needed: bool) -> None:
    """A ``Fix:`` line for the refusals this command can say more about. The app was found
    before the call, so ``NOT_FOUND`` where a link is needed means nothing is connected."""
    if e.body.code == ErrorCode.AGENT_SESSION_REFUSED:
        e.fix = (
            "a person connects repositories from their own sign-in (`ssc login`), never an agent."
        )
    elif e.body.code == ErrorCode.FORBIDDEN and change:
        e.fix = "only an org admin, the app's owner or a builder on prod connects a repository."
    elif e.body.code == ErrorCode.NOT_FOUND and link_needed:
        e.fix = f"connect one with `ssc repo connect {slug} OWNER/NAME`."

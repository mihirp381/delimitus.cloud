"""``ssc status``."""

from typing import Annotated

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.models import AppOut
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import resolve_app
from ssc_cli.shapes import AppResult, DeploymentRow, EnvironmentRow

AppArg = Annotated[str, typer.Argument(help="App slug or app_ id.")]


def status(ctx: typer.Context, app: AppArg, json_mode: JsonOpt = False) -> None:
    """Show an app's environments and what is deployed to each."""
    with handled(json_mode), session(ctx).client() as client:
        result = app_result(client, resolve_app(client, app), with_deployments=True)
    if json_mode:
        print_json(result)
        return
    say(f"{result.slug}  {result.id}  {result.status}  owner {result.owner_user_id}")
    say()
    rows = [
        (
            e.name,
            dash(e.deployment.state if e.deployment else None),
            dash(e.deployment.release_id if e.deployment else None),
            dash(e.current_deployment_id),
            dash(e.deployment.finished_at if e.deployment else None),
        )
        for e in result.environments
    ]
    say(table(("ENVIRONMENT", "STATE", "RELEASE", "DEPLOYMENT", "FINISHED"), rows))


def app_result(client: ApiClient, app: AppOut, *, with_deployments: bool) -> AppResult:
    envs: list[EnvironmentRow] = []
    for e in app.environments:
        deployment = None
        if with_deployments and e.current_deployment_id:
            op = client.get_operation(e.current_deployment_id)
            deployment = DeploymentRow(
                operation_id=op.operation_id,
                kind=op.kind,
                state=op.state,
                release_id=op.release_id,
                started_at=op.started_at,
                finished_at=op.finished_at,
            )
        envs.append(
            EnvironmentRow(
                id=e.id,
                name=e.name,
                config_version=e.config_version,
                grants_version=e.grants_version,
                current_deployment_id=e.current_deployment_id,
                deployment=deployment,
            )
        )
    return AppResult(
        id=app.id,
        slug=app.slug,
        owner_user_id=app.owner_user_id,
        status=app.status,
        created_at=app.created_at,
        environments=envs,
    )

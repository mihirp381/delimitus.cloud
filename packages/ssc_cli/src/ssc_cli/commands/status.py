"""``ssc status``."""

from typing import Annotated

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import CliError
from ssc_cli.models import AppOut
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import resolve_app
from ssc_cli.shapes import AppResult, DatabaseRow, DeploymentRow, EnvironmentRow
from ssc_contracts.app_database import POOL_FIX_IT

AppArg = Annotated[str, typer.Argument(help="App slug or app_ id.")]


def status(ctx: typer.Context, app: AppArg, json_mode: JsonOpt = False) -> None:
    """Show an app's environments, what is deployed to each, and each one's database."""
    with handled(json_mode), session(ctx).client() as client:
        result = app_result(
            client, resolve_app(client, app), with_deployments=True, with_databases=True
        )
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
            dash(e.url),
            database_text(e.database),
        )
        for e in result.environments
    ]
    headers = ("ENVIRONMENT", "STATE", "RELEASE", "DEPLOYMENT", "FINISHED", "URL", "DATABASE")
    say(table(headers, rows))
    databases = [e.database for e in result.environments if e.database is not None]
    if databases:
        say()
        places = next((d for d in databases if d.places_total is not None), None)
        if places is not None:
            say(
                f"Database places on your company's instance: {places.places_used} of "
                f"{places.places_total}, previews included."
            )
        say(POOL_FIX_IT)


def database_text(database: DatabaseRow | None) -> str:
    """Size and connections in use against the limit, or a dash when there is no database."""
    if database is None:
        return "-"
    size = "size ?" if database.size_bytes is None else f"{database.size_bytes / 1e6:.1f} MB"
    used = "?" if database.connections is None else str(database.connections)
    limit = "?" if database.connection_limit is None else str(database.connection_limit)
    return f"{size}, {used}/{limit} connections"


def _database(client: ApiClient, app_id: str, environment_id: str) -> DatabaseRow | None:
    try:
        out = client.get_database(app_id, environment_id)
    except CliError:
        return None
    if not out.present or out.database is None:
        return None
    return DatabaseRow(
        database=out.database,
        connection_limit=out.connection_limit,
        pool_size=out.pool_size,
        size_bytes=out.size_bytes,
        connections=out.connections,
        places_used=out.places_used,
        places_total=out.places_total,
    )


def app_result(
    client: ApiClient, app: AppOut, *, with_deployments: bool, with_databases: bool = False
) -> AppResult:
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
                url=e.url,
                database=_database(client, app.id, e.id) if with_databases else None,
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

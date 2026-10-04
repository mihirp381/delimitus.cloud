"""``ssc connections``: the data connections your org has set up and, for one app, which of them
an environment may reach (``GET /v1/connections``, ``GET .../environments/{id}/connections``).
An org admin sees every connection; anyone else sees only those their own approved requests
named. A connection whose ceiling is narrower than the environment's audience shows
``over ceiling``: narrow the sharing, or ask for it to be allowed (``exceed_ceiling``)."""

from typing import Annotated

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.share import Env, EnvOpt
from ssc_cli.models import CeilingDoc, ConnectionOut
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import ConnectionRow, ConnectionsResult

AppOpt = Annotated[
    str | None,
    typer.Argument(
        metavar="[APP]",
        help="App slug or app_ id. Without one, every connection you may see.",
        show_default=False,
    ),
]


def _ceiling(c: CeilingDoc) -> list[str]:
    if c.audience == "org":
        return ["org"]
    return [f"{s.kind}:{s.id}" for s in c.subjects]


def _row(c: ConnectionOut, over_ceiling_since: str | None = None) -> ConnectionRow:
    return ConnectionRow(
        name=c.name,
        classification=c.classification,
        ceiling=_ceiling(c.ceiling),
        setup_status=c.setup_status,
        status=c.status,
        over_ceiling_since=over_ceiling_since,
    )


def connections(
    ctx: typer.Context, app: AppOpt = None, env: EnvOpt = Env.prod, json_mode: JsonOpt = False
) -> None:
    """Show the data connections your org has, or the ones an app environment may reach."""
    with handled(json_mode), session(ctx).client() as client:
        if app is None:
            result = ConnectionsResult(
                api_url=client.api_url,
                app_id=None,
                slug=None,
                environment=None,
                connections=[_row(c) for c in client.list_connections().connections],
            )
        else:
            target = resolve_app(client, app)
            where = environment(target, env.value)
            linked = client.environment_connections(target.id, where.id)
            result = ConnectionsResult(
                api_url=client.api_url,
                app_id=target.id,
                slug=target.slug,
                environment=where.name,
                connections=[_row(c.connection, c.over_ceiling_since) for c in linked.connections],
            )
    if json_mode:
        print_json(result)
        return
    if not result.connections:
        say(
            "No data connections."
            if result.slug is None
            else f"{result.slug} {result.environment} reaches no data connections."
        )
        return
    say(
        table(
            ("CONNECTION", "CLASSIFICATION", "CEILING", "SETUP", "STATUS", "OVER CEILING"),
            [
                (
                    c.name,
                    c.classification,
                    ", ".join(c.ceiling),
                    c.setup_status,
                    c.status,
                    dash(c.over_ceiling_since),
                )
                for c in result.connections
            ],
        )
    )
    if any(c.over_ceiling_since for c in result.connections):
        say(
            "\nOver ceiling: this environment is shared more widely than a connection allows. "
            "Narrow the sharing, or ask for it to be allowed (an exceed_ceiling approval)."
        )

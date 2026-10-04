"""``ssc policy``: what the org lets your apps reach and use (``GET /v1/org/deployment-policy``),
the same answer the ``get_org_deployment_policy`` agent tool gives. An org admin sees the whole
org; anyone else sees only what their own approved requests opened."""

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.output import print_json, say, table
from ssc_cli.shapes import (
    PolicyApprovalRow,
    PolicyConnectionRow,
    PolicyDatabaseRow,
    PolicyHostRow,
    PolicyResult,
)


def policy(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Show the internet hosts, data connections, approvals, database room and system packages
    your org allows."""
    with handled(json_mode), session(ctx).client() as client:
        p = client.deployment_policy()
        result = PolicyResult(
            api_url=client.api_url,
            scope=p.scope,
            hosts=[PolicyHostRow(**h.model_dump()) for h in p.hosts],
            connections=[PolicyConnectionRow(**c.model_dump()) for c in p.connections],
            approvals=[PolicyApprovalRow(**a.model_dump()) for a in p.approvals],
            approver=p.approver,
            database=PolicyDatabaseRow(**p.database.model_dump()),
            approved_packages=p.approved_packages,
            how_to_ask_for_a_package=p.how_to_ask_for_a_package,
        )
    if json_mode:
        print_json(result)
        return
    if result.scope == "own":
        say("Showing what your own approved requests opened; an org admin sees the whole org.\n")
    hosts = [(h.host, h.app_id, h.environment_id) for h in result.hosts]
    say(table(("HOST", "APP", "ENVIRONMENT"), hosts) if hosts else "No internet hosts approved.")
    say()
    connections = [(c.name, c.kind, c.classification) for c in result.connections]
    say(
        table(("CONNECTION", "KIND", "CLASSIFICATION"), connections)
        if connections
        else "No data connections."
    )
    db = result.database
    room = "room for another app" if db.room else "no room for another app"
    say(f"\nCompany database: {db.places_used} of {db.places_total} places used, {room}.")
    say("\nWaits for an approval:")
    for a in result.approvals:
        say(f"- {a.kind}: {a.when}")
    say(result.approver)
    say(f"\nSystem packages a build may install: {', '.join(result.approved_packages)}.")
    say(result.how_to_ask_for_a_package)

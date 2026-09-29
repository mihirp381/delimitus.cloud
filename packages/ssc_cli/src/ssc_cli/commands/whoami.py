"""``ssc whoami``."""

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.shapes import WhoamiResult


def whoami(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Show the org and person the token belongs to, and their org role."""
    with handled(json_mode), session(ctx).client() as client:
        me = client.whoami()
        result = WhoamiResult(
            api_url=client.api_url,
            org_id=me.org_id,
            subject=me.subject,
            kind=me.kind,
            credential_id=me.credential_id,
            is_agent=me.is_agent,
            client_id=me.client_id,
            role=me.role,
        )
    if json_mode:
        print_json(result)
        return
    say(
        table(
            ("FIELD", "VALUE"),
            [
                ("api", result.api_url),
                ("org", result.org_id),
                ("subject", result.subject),
                ("kind", result.kind),
                ("role", dash(result.role)),
                ("agent", "yes" if result.is_agent else "no"),
                ("client id", dash(result.client_id)),
            ],
        )
    )

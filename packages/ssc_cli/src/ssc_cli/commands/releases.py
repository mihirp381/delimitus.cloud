"""``ssc releases``: an app's numbered releases, where each was built for and where it runs."""

from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.models import AppOut
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.resolve import resolve_app
from ssc_cli.shapes import ReleaseRow, ReleasesResult

MAX_PAGE: Final = 100
MAX_BEFORE: Final = 2**31 - 1
SHORT_COMMIT: Final = 12

AppArg = Annotated[str, typer.Argument(help="App slug or app_ id.")]
LimitOpt = Annotated[
    int, typer.Option("--limit", min=1, max=MAX_PAGE, help="How many releases to list.")
]
BeforeOpt = Annotated[
    int | None,
    typer.Option(
        "--before",
        min=1,
        max=MAX_BEFORE,
        metavar="NUMBER",
        help="List releases numbered below this.",
    ),
]


def releases(
    ctx: typer.Context,
    app: AppArg,
    limit: LimitOpt = 20,
    before: BeforeOpt = None,
    json_mode: JsonOpt = False,
) -> None:
    """List an app's releases, newest first."""
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        page = client.list_releases(target.id, limit=limit, before=before)
        names = {e.id: e.name for e in target.environments}
        live = _live_in(client, target)
        rows = [
            ReleaseRow(
                release_id=r.release_id,
                number=r.number,
                label=r.label,
                built_for=names.get(r.built_for_environment_id or ""),
                built_for_environment_id=r.built_for_environment_id,
                live_in=live.get(r.release_id, []),
                source_digest=r.source_digest,
                source_commit=r.source_commit,
                image_digest=r.image_digest,
                created_at=r.created_at,
                actor_kind=r.actor.kind,
                actor_id=r.actor.id,
                via_agent=r.actor.via_agent,
            )
            for r in page.items
        ]
    result = ReleasesResult(
        app_id=target.id, slug=target.slug, releases=rows, next_before=page.next_before
    )
    if json_mode:
        print_json(result)
        return
    if not rows:
        say(f"{target.slug} has no releases yet. Run `ssc deploy --app {target.slug}`.")
        return
    say(
        table(
            ("RELEASE", "ID", "BUILT FOR", "LIVE IN", "COMMIT", "CREATED", "BY"),
            [
                (
                    r.label,
                    r.release_id,
                    dash(r.built_for),
                    dash(", ".join(r.live_in)),
                    dash(r.source_commit[:SHORT_COMMIT] if r.source_commit else None),
                    r.created_at,
                    r.actor_id + (" (agent)" if r.via_agent else ""),
                )
                for r in rows
            ],
        )
    )
    if result.next_before is not None:
        say(f"More: ssc releases {target.slug} --before {result.next_before}")


def _live_in(client: ApiClient, app: AppOut) -> dict[str, list[str]]:
    """Release id to the environments whose live deployment runs it."""
    live: dict[str, list[str]] = {}
    for e in app.environments:
        if e.current_deployment_id:
            op = client.get_operation(e.current_deployment_id)
            if op.release_id:
                live.setdefault(op.release_id, []).append(e.name)
    return live

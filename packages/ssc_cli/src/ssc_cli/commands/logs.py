"""``ssc logs``: what an app environment printed, its builds, or its deployments (SSC-024).

Lines are redacted in the cell and again by the API. ``--follow`` keeps asking for the lines after
the last one; each request waits in the API up to ``FOLLOW_WAIT`` seconds for a new line, so one
shows within seconds of being written. A follow refused with ``LOGS_RATE_LIMITED`` waits out
``Retry-After`` (at most a minute) and asks again from the same cursor; any other refusal ends it.
Builders, the app's owner and org admins may read; anyone else, a ``user``-role grant included, is
``FORBIDDEN``.
"""

import json
import re
from enum import StrEnum
from typing import Annotated, Final

import typer

from ssc_cli.api import MAX_RETRY_AFTER, ApiClient, Sleep
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.commands.share import Env
from ssc_cli.commands.status import AppArg
from ssc_cli.errors import CliError
from ssc_cli.models import LogLineOut
from ssc_cli.output import print_json, say
from ssc_cli.resolve import environment, resolve_app
from ssc_cli.shapes import LogLineRow, LogsResult
from ssc_contracts.errors import ErrorCode
from ssc_shared.logs import MAX_SINCE_SECONDS

FOLLOW_WAIT: Final = 15
FOLLOW_POLLS: int | None = None
"""How many follow requests to make; None for as many as it takes (tests bound it)."""
UNITS: Final = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SINCE: Final = re.compile(r"([1-9][0-9]{0,6})([smhd]?)")


class Source(StrEnum):
    app = "app"
    build = "build"
    deploy = "deploy"


def _check_since(value: str) -> str:
    since_seconds(value)
    return value


def since_seconds(value: str) -> int:
    """``90``, ``90s``, ``10m``, ``2h`` or ``1d`` as seconds, at most seven days."""
    m = _SINCE.fullmatch(value.strip())
    if m is None:
        raise typer.BadParameter("use seconds, or a number with s, m, h or d, like 10m")
    seconds = int(m[1]) * UNITS[m[2]]
    if seconds > MAX_SINCE_SECONDS:
        raise typer.BadParameter("at most 7d")
    return seconds


EnvOpt = Annotated[Env, typer.Option("--env", help="Which environment.")]
SourceOpt = Annotated[
    Source,
    typer.Option("--source", help="app: what the app printed and its requests; build; or deploy."),
]
SinceOpt = Annotated[
    str,
    typer.Option(
        "--since",
        metavar="DURATION",
        callback=_check_since,
        help="How far back, like 10m, 2h or 1d.",
    ),
]
FollowOpt = Annotated[
    bool, typer.Option("--follow", "-f", help="Keep printing new lines until interrupted.")
]


def logs(  # noqa: PLR0913, PLR0917  (Typer maps each parameter to an option)
    ctx: typer.Context,
    app: AppArg,
    env: EnvOpt = Env.prod,
    source: SourceOpt = Source.app,
    since: SinceOpt = "1h",
    follow: FollowOpt = False,
    json_mode: JsonOpt = False,
) -> None:
    """Show an environment's logs; --follow keeps printing new lines."""
    seconds = since_seconds(since)
    with handled(json_mode), session(ctx).client() as client:
        target = resolve_app(client, app)
        where = environment(target, env.value)
        try:
            page = client.get_logs(target.id, where.id, source=source.value, since=seconds)
            if not follow:
                result = LogsResult(
                    app_id=target.id,
                    slug=target.slug,
                    environment=where.name,
                    environment_id=where.id,
                    source=source.value,
                    lines=[_row(line) for line in page.lines],
                    cursor=page.cursor,
                )
                if json_mode:
                    print_json(result)
                else:
                    for line in result.lines:
                        say(_text(line))
                return
            _show(page.lines, json_mode)
            _follow(client, target.id, where.id, source, page.cursor, json_mode, session(ctx).sleep)
        except CliError as e:
            _explain(e, env)
            raise


def _follow(  # noqa: PLR0913, PLR0917  (the follow loop's whole state)
    client: ApiClient,
    app_id: str,
    environment_id: str,
    source: Source,
    cursor: str | None,
    json_mode: bool,
    sleep: Sleep,
) -> None:
    polls = 0
    try:
        while FOLLOW_POLLS is None or polls < FOLLOW_POLLS:
            polls += 1
            try:
                page = client.get_logs(
                    app_id, environment_id, source=source.value, after=cursor, wait=FOLLOW_WAIT
                )
            except CliError as e:
                if e.body.code != ErrorCode.LOGS_RATE_LIMITED:
                    raise
                sleep(min(e.retry_after or MAX_RETRY_AFTER, MAX_RETRY_AFTER))
                continue
            _show(page.lines, json_mode)
            cursor = page.cursor or cursor
    except KeyboardInterrupt:
        return


def _explain(e: CliError, env: Env) -> None:
    if e.body.code == ErrorCode.FORBIDDEN:
        e.fix = f"only an org admin, the app's owner or a builder on {env.value} reads its logs."
    elif e.body.code == ErrorCode.LOGS_RATE_LIMITED:
        e.fix = "the cell is reading many logs; wait a little and run it again."
    elif e.body.code == ErrorCode.LOGS_UNAVAILABLE:
        e.fix = "retry in a few minutes; `--source deploy` still reads."


def _show(lines: list[LogLineOut], json_mode: bool) -> None:
    for line in lines:
        row = _row(line)
        say(json.dumps(row.model_dump(mode="json"), sort_keys=True) if json_mode else _text(row))


def _row(line: LogLineOut) -> LogLineRow:
    return LogLineRow(
        timestamp=line.timestamp, severity=line.severity, source=line.source, text=line.text
    )


def _text(line: LogLineRow) -> str:
    return f"{line.timestamp}  {line.severity:<8}  {line.text}"

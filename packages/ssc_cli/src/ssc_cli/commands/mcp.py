"""``ssc mcp``: serve the agent tools over stdio. Needs the ``ssc-cli[mcp]`` extra."""

import typer

from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import MCP_NOT_INSTALLED, ExitCode, local_error


def mcp(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Serve the agent tools (MCP) on stdin and stdout. Needs an agent's token."""
    with handled(json_mode):
        try:
            from ssc_cli import mcp_local  # noqa: PLC0415  (mcp is an optional extra)
        except ImportError as e:
            if (e.name or "").split(".")[0] != "mcp":
                raise
            raise local_error(
                MCP_NOT_INSTALLED,
                "The mcp extra is not installed.",
                "Install ssc with it: `uv tool install 'ssc-cli[mcp]'`.",
                ExitCode.FAILED,
            ) from None
        mcp_local.serve(session(ctx))

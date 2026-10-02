"""``python -m ssc_control.identity``: the auth host and the directory sync (SSC-019).

* ``serve [--host H] [--port P]``: the auth host (settings: ``identity.settings``).
* ``sync [--org ID] [--loop]``: one directory sync tick for every org with a connection (or one
  org); ``--loop`` repeats every :data:`INTERVAL_SECONDS` until stopped. Registration in the
  control-plane worker waits for SSC-017.
* ``connect --org ID --operator ID --workos-org ID --directory ID --sso ID [--sso ID]
  --join-rule idp_id|email [--admin-group ID]``: record an org's directory connection.

Every command connects with ``SSC_DATABASE_DSN``; ``sync`` reads ``SSC_WORKOS_API_KEY``,
``SSC_WORKOS_CLIENT_ID`` and optionally ``SSC_WORKOS_BASE``.
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Final

from ssc_contracts.audit import ActorKind
from ssc_control.audit.chain import Actor
from ssc_control.db import all_org_ids, bound_org, check_org_id, make_engine
from ssc_control.identity import connections, sync
from ssc_control.identity.workos import DEFAULT_BASE, WorkOSClient

INTERVAL_SECONDS: Final = 60
log = logging.getLogger("ssc.identity")


def _workos(env: dict[str, str]) -> WorkOSClient:
    return WorkOSClient(
        api_key=env["SSC_WORKOS_API_KEY"],
        client_id=env["SSC_WORKOS_CLIENT_ID"],
        base=env.get("SSC_WORKOS_BASE", DEFAULT_BASE),
    )


async def run_sync(dsn: str, client: WorkOSClient, *, org: str | None, loop: bool) -> int:
    engine = make_engine(dsn)
    failures = 0
    try:
        while True:
            for org_id in [org] if org else await all_org_ids(engine):
                try:
                    report = await sync.tick(engine, client, org_id)
                except Exception:
                    log.exception("directory sync of %s raised", org_id)
                    failures += 1
                    continue
                if report is not None:
                    log.info("directory sync %s", report)
                    failures += report.error is not None
            if not loop:
                return 1 if failures else 0
            await asyncio.sleep(INTERVAL_SECONDS)
    finally:
        await client.aclose()
        await engine.dispose()


async def run_connect(dsn: str, args: argparse.Namespace) -> str:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, args.org) as conn:
            return await connections.connect(
                conn,
                args.org,
                workos_organization_id=args.workos_org,
                workos_directory_id=args.directory,
                sso_connection_ids=args.sso,
                join_rule=args.join_rule,
                admin_group_ref=args.admin_group,
                actor=Actor(kind=ActorKind.OPERATOR, id=args.operator),
            )
    finally:
        await engine.dispose()


def serve(host: str, port: int) -> None:
    import uvicorn  # noqa: PLC0415  (only the serving process needs it)

    from ssc_control.identity.authhost import AuthHost, create_auth_app  # noqa: PLC0415
    from ssc_control.identity.cell_callers import DevCallers, GoogleCallers  # noqa: PLC0415
    from ssc_control.identity.settings import AuthSettings  # noqa: PLC0415
    from ssc_control.identity.tokens import Signer  # noqa: PLC0415

    s = AuthSettings.from_env()
    callers = DevCallers(s.dev_cell_secret) if s.dev_cell_secret else GoogleCallers(s.auth_url)
    app = create_auth_app(
        AuthHost(
            settings=s,
            engine=make_engine(s.database_dsn),
            workos=WorkOSClient(
                api_key=s.workos_api_key, client_id=s.workos_client_id, base=s.workos_base
            ),
            signer=Signer(s.signing_pem, s.signing_kid, s.auth_url),
            callers=callers,
        )
    )
    uvicorn.run(app, host=host, port=port, proxy_headers=True, log_level="info")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.identity")
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="0.0.0.0")  # noqa: S104  (a container listens on all)
    p_serve.add_argument("--port", type=int, default=8080)
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--org", type=check_org_id)
    p_sync.add_argument("--loop", action="store_true")
    p_conn = sub.add_parser("connect")
    p_conn.add_argument("--org", type=check_org_id, required=True)
    p_conn.add_argument("--operator", required=True)
    p_conn.add_argument("--workos-org", required=True)
    p_conn.add_argument("--directory", required=True)
    p_conn.add_argument("--sso", action="append", required=True)
    p_conn.add_argument("--join-rule", choices=["idp_id", "email"], required=True)
    p_conn.add_argument("--admin-group")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "serve":
        serve(args.host, args.port)
        return 0
    env = dict(os.environ)
    dsn = env.get("SSC_DATABASE_DSN")
    if not dsn:
        sys.stderr.write("SSC_DATABASE_DSN is not set\n")
        return 2
    if args.command == "sync":
        return asyncio.run(run_sync(dsn, _workos(env), org=args.org, loop=args.loop))
    try:
        connection_id = asyncio.run(run_connect(dsn, args))
    except connections.ConnectError as e:
        sys.stderr.write(f"not connected: {e}\n")
        return 1
    sys.stdout.write(connection_id + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

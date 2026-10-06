"""``python -m ssc_control.identity``: the auth host and the directory sync (SSC-019).

* ``serve [--host H] [--port P]``: the auth host (settings: ``identity.settings``).
* ``sync [--org ID] [--loop]``: one directory sync tick for every org with a connection (or one
  org); ``--loop`` repeats every :data:`INTERVAL_SECONDS` until stopped. In production the
  worker runs the same tick every minute (``identity.jobs``).
* ``connect --org ID --operator ID --workos-org ID --directory ID --sso ID [--sso ID]
  --join-rule idp_id|email [--admin-group ID]``: record an org's directory connection, after
  checking in WorkOS that a linked admin is an active user of the directory (SSC-097).
* ``create-org --name N --founder-name N --founder-email E --founder-idp-id ID --operator op_x
  --workos-org ID --directory ID --sso ID [--sso ID] --join-rule idp_id|email
  [--admin-group ID] --cell-label L``: the org, the founder check, the connection and the cell
  label in one transaction (``identity.operator``). Prints the org id.
* ``restore-admin --org ID --user ID --operator op_x --reason TEXT [--outside-admin-group]
  [--already-applied-at TIME]``: make a person an active admin again, audited
  (``identity.operator``).

Every command connects with ``SSC_DATABASE_DSN``; ``sync``, ``connect`` and ``create-org`` read
``SSC_WORKOS_API_KEY``, ``SSC_WORKOS_CLIENT_ID`` and optionally ``SSC_WORKOS_BASE``.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Final

from ssc_contracts.audit import ActorKind
from ssc_control.audit.chain import Actor
from ssc_control.db import all_org_ids, bound_org, check_org_id, make_engine
from ssc_control.identity import connections, operator, sync
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


async def run_connect(dsn: str, client: WorkOSClient, args: argparse.Namespace) -> str:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, args.org) as conn:
            checked = await connections.check_founder(
                conn,
                client,
                args.org,
                workos_directory_id=args.directory,
                join_rule=args.join_rule,
            )
            for result in checked:
                sys.stderr.write(result.line + "\n")
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
        await client.aclose()
        await engine.dispose()


async def run_create_org(dsn: str, client: WorkOSClient, args: argparse.Namespace) -> str:
    engine = make_engine(dsn)
    try:
        made = await operator.create_org_with_directory(
            engine,
            client,
            name=args.name,
            founder_name=args.founder_name,
            founder_email=args.founder_email,
            founder_idp_id=args.founder_idp_id,
            workos_organization_id=args.workos_org,
            workos_directory_id=args.directory,
            sso_connection_ids=args.sso,
            join_rule=args.join_rule,
            admin_group_ref=args.admin_group,
            cell_label=args.cell_label,
            actor=Actor(kind=ActorKind.OPERATOR, id=args.operator),
        )
        for result in made.founder:
            sys.stderr.write(result.line + "\n")
        return made.org.org_id
    finally:
        await client.aclose()
        await engine.dispose()


async def run_restore_admin(dsn: str, args: argparse.Namespace) -> operator.Restored:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, args.org) as conn:
            return await operator.restore_admin(
                conn,
                args.org,
                args.user,
                actor=Actor(kind=ActorKind.OPERATOR, id=args.operator),
                reason=args.reason,
                outside_admin_group=args.outside_admin_group,
                already_applied_at=args.already_applied_at,
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


def _parser() -> argparse.ArgumentParser:
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
    p_make = sub.add_parser("create-org")
    p_make.add_argument("--name", required=True)
    p_make.add_argument("--founder-name", required=True)
    p_make.add_argument("--founder-email", required=True)
    p_make.add_argument("--founder-idp-id", required=True)
    p_make.add_argument("--operator", type=operator.check_operator_id, required=True)
    p_make.add_argument("--workos-org", required=True)
    p_make.add_argument("--directory", required=True)
    p_make.add_argument("--sso", action="append", required=True)
    p_make.add_argument("--join-rule", choices=["idp_id", "email"], required=True)
    p_make.add_argument("--admin-group")
    p_make.add_argument("--cell-label", type=operator.check_cell_label, required=True)
    p_restore = sub.add_parser("restore-admin")
    p_restore.add_argument("--org", type=check_org_id, required=True)
    p_restore.add_argument("--user", required=True)
    p_restore.add_argument("--operator", type=operator.check_operator_id, required=True)
    p_restore.add_argument("--reason", type=operator.check_reason, required=True)
    p_restore.add_argument("--outside-admin-group", action="store_true")
    p_restore.add_argument("--already-applied-at", type=datetime.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "serve":
        serve(args.host, args.port)
        return 0
    env = dict(os.environ)
    dsn = env.get("SSC_DATABASE_DSN")
    if not dsn:
        sys.stderr.write("SSC_DATABASE_DSN is not set\n")
        return 2
    return _run(args, dsn, env)


def _run(args: argparse.Namespace, dsn: str, env: dict[str, str]) -> int:
    if args.command == "restore-admin":
        try:
            restored = asyncio.run(run_restore_admin(dsn, args))
        except operator.RestoreError as e:
            sys.stderr.write(f"not restored: {e}\n")
            return 1
        for warning in restored.warnings:
            sys.stderr.write(f"warning: {warning}\n")
        what = "recorded" if restored.recorded_only else "restored"
        sys.stdout.write(f"{what} {restored.user_id} (admin, active)\n")
        return 0
    if "SSC_WORKOS_API_KEY" not in env or "SSC_WORKOS_CLIENT_ID" not in env:
        sys.stderr.write("SSC_WORKOS_API_KEY and SSC_WORKOS_CLIENT_ID must be set\n")
        return 2
    if args.command == "sync":
        return asyncio.run(run_sync(dsn, _workos(env), org=args.org, loop=args.loop))
    try:
        if args.command == "create-org":
            created = asyncio.run(run_create_org(dsn, _workos(env), args))
        else:
            created = asyncio.run(run_connect(dsn, _workos(env), args))
    except connections.ConnectError as e:
        sys.stderr.write(f"not {'created' if args.command == 'create-org' else 'connected'}: {e}\n")
        return 1
    sys.stdout.write(created + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""``python -m ssc_control.onboarding``: the operator's onboarding of one customer (ticket 19D).

* ``start --customer-name N --domain D --admin-email E --stage S --operator op_x [--label L]
  [--org-id ORG]``: the row, its building cell, the WorkOS organisation and its two links, then
  the command that builds the cell.
* ``status --id ID_OR_LABEL`` and ``list``: both tracks and the reason; the links while the
  identity track waits.
* ``links --id ID_OR_LABEL``: new links (WorkOS links expire five minutes after they are made).
* ``confirm --id ID_OR_LABEL --join-rule email --operator op_x``: Google Workspace's join rule.
* ``abandon --id ID_OR_LABEL --reason TEXT --operator op_x``: retires the cell; never deletes.
* ``tick``: one tick now, as the worker runs every five minutes.

Every command connects with ``SSC_DATABASE_DSN``; ``start``, ``links`` and ``tick`` also need
``SSC_WORKOS_API_KEY`` and ``SSC_WORKOS_CLIENT_ID`` (``SSC_WORKOS_BASE`` optional).
"""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db import check_org_id, make_engine
from ssc_control.identity.operator import check_operator_id, check_reason
from ssc_control.identity.workos import DEFAULT_BASE, WorkOSClient
from ssc_control.onboarding import service
from ssc_shared import redaction
from ssc_shared.hosts import check_cell_label

type WorkOSFactory = Callable[[dict[str, str]], WorkOSClient]

WORKOS_COMMANDS: Final = frozenset({"start", "links", "tick"})
WAITING: Final = {
    "JOIN_RULE_CONFIRM": (
        "Google Workspace joins logins by email (decision 002): check the domain is the "
        "customer's, then run confirm --join-rule email"
    ),
    "IDP_UNSUPPORTED": (
        "the connection or the directory is not one SSC supports (Okta, or Google Workspace "
        "SAML with its directory): have it fixed in WorkOS; the next tick reads it again"
    ),
    "ADMIN_NOT_IN_DIRECTORY": (
        "the admin email is not one active member of the connected directory: have the "
        "customer add the admin; the next tick reads it again"
    ),
    "WORKOS_ERROR": "WorkOS did not answer as expected: the next tick retries",
}


def workos_from_env(env: dict[str, str]) -> WorkOSClient:
    return WorkOSClient(
        api_key=env["SSC_WORKOS_API_KEY"],
        client_id=env["SSC_WORKOS_CLIENT_ID"],
        base=env.get("SSC_WORKOS_BASE", DEFAULT_BASE),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.onboarding")
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("--customer-name", required=True)
    p_start.add_argument("--domain", required=True)
    p_start.add_argument("--admin-email", required=True)
    p_start.add_argument("--stage", choices=["staging", "prod"], required=True)
    p_start.add_argument("--label", type=check_cell_label)
    p_start.add_argument("--org-id", type=check_org_id)
    p_status = sub.add_parser("status")
    sub.add_parser("list")
    p_links = sub.add_parser("links")
    p_confirm = sub.add_parser("confirm")
    p_confirm.add_argument("--join-rule", choices=["email"], required=True)
    p_abandon = sub.add_parser("abandon")
    p_abandon.add_argument("--reason", type=check_reason, required=True)
    sub.add_parser("tick")
    for p in (p_status, p_links, p_confirm, p_abandon):
        p.add_argument("--id", dest="key", metavar="ID_OR_LABEL", required=True)
    for p in (p_start, p_confirm, p_abandon):
        p.add_argument("--operator", type=check_operator_id, required=True)
    return parser


def _links(ob: service.Onboarding) -> list[str]:
    return [f"sso link: {ob.sso_link}", f"directory link: {ob.dsync_link}"]


def _status(ob: service.Onboarding) -> list[str]:
    lines = [ob.line]
    if ob.reason:
        lines.append(f"waiting: {ob.reason}: {WAITING[ob.reason]}")
    if ob.status == "open" and ob.idp_state == "awaiting_idp":
        lines += _links(ob)
    return lines


def _started(ob: service.Onboarding) -> list[str]:
    lines = [ob.line]
    if ob.reason:
        lines.append(f"waiting: {ob.reason}: {WAITING[ob.reason]}")
    else:
        lines += _links(ob)
        lines.append(
            "the links expire five minutes after they are made; new ones: "
            f"uv run python -m ssc_control.onboarding links --id {ob.id}"
        )
    lines.append(
        f"build the cell: uv run python -m ssc_infra.onboard {ob.label} --stage {ob.stage} "
        f"--settings settings.json --set org_id={ob.org_id} --probe-image <probe image>"
    )
    return lines


def _ids(ids: tuple[str, ...]) -> str:
    return " ".join(ids) or "none"


async def _workos_lines(
    engine: AsyncEngine, client: WorkOSClient, args: argparse.Namespace
) -> list[str]:
    if args.command == "start":
        ob = await service.start(
            engine,
            client,
            customer_name=args.customer_name,
            domain=args.domain,
            admin_email=args.admin_email,
            stage=args.stage,
            operator=args.operator,
            label=args.label,
            org_id=args.org_id,
        )
        return _started(ob)
    if args.command == "links":
        return [
            (ob := await service.refresh_links(engine, client, id_or_label=args.key)).line,
            *_links(ob),
        ]
    r = await service.tick(engine, client, datetime.now(UTC))
    moved = f"changed {_ids(r.changed)}; live {_ids(r.live)}; failed {_ids(r.failed)}"
    return [f"checked {r.checked}; {moved}"]


async def _lines(engine: AsyncEngine, args: argparse.Namespace) -> list[str]:
    if args.command == "list":
        return [ob.line for ob in await service.list_all(engine)]
    if args.command == "status":
        return _status(await service.status(engine, args.key))
    if args.command == "confirm":
        ob = await service.confirm(
            engine, id_or_label=args.key, join_rule=args.join_rule, operator=args.operator
        )
        return [ob.line]
    done = await service.abandon(
        engine, id_or_label=args.key, operator=args.operator, reason=args.reason
    )
    return [done.onboarding.line, *done.lines]


async def run(dsn: str, args: argparse.Namespace, client: WorkOSClient | None) -> list[str]:
    engine = make_engine(dsn)
    try:
        if client is not None:
            return await _workos_lines(engine, client, args)
        return await _lines(engine, args)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None, *, workos: WorkOSFactory = workos_from_env) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    redaction.install()
    env = dict(os.environ)
    dsn = env.get("SSC_DATABASE_DSN")
    if not dsn:
        sys.stderr.write("SSC_DATABASE_DSN is not set\n")
        return 2
    client = None
    if args.command in WORKOS_COMMANDS:
        if "SSC_WORKOS_API_KEY" not in env or "SSC_WORKOS_CLIENT_ID" not in env:
            sys.stderr.write("SSC_WORKOS_API_KEY and SSC_WORKOS_CLIENT_ID must be set\n")
            return 2
        client = workos(env)
    try:
        out = asyncio.run(run(dsn, args, client))
    except ValueError as e:
        sys.stderr.write(f"refused: {e}\n")
        return 1
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""``python -m ssc_control.github``: what an SSC operator does for the GitHub App (SSC-047).

* ``bind --org ID --operator ID --installation N``: bind a GitHub App installation to the org,
  after the customer installed the App on their account and support checked the installation is
  theirs. Audited as ``github.installation_bound`` with the operator as actor. An installation
  bound to another org is refused.

Connects with ``SSC_DATABASE_DSN``.
"""

import argparse
import asyncio
import os
import sys

from ssc_contracts.audit import ActorKind
from ssc_control.audit.chain import Actor
from ssc_control.db import bound_org, check_org_id, make_engine
from ssc_control.github.links import BindError, bind_installation


def _installation(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("an installation id is a positive integer")
    return int(value)


async def run_bind(dsn: str, *, org: str, operator: str, installation: int) -> bool:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org) as conn:
            return await bind_installation(
                conn, org, installation, actor=Actor(kind=ActorKind.OPERATOR, id=operator)
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.github")
    sub = parser.add_subparsers(dest="command", required=True)
    p_bind = sub.add_parser("bind")
    p_bind.add_argument("--org", type=check_org_id, required=True)
    p_bind.add_argument("--operator", required=True)
    p_bind.add_argument("--installation", type=_installation, required=True)
    args = parser.parse_args(argv)
    dsn = os.environ.get("SSC_DATABASE_DSN")
    if not dsn:
        sys.stderr.write("SSC_DATABASE_DSN is not set\n")
        return 2
    try:
        bound = asyncio.run(
            run_bind(dsn, org=args.org, operator=args.operator, installation=args.installation)
        )
    except BindError as e:
        sys.stderr.write(f"not bound: {e}\n")
        return 1
    sys.stdout.write("bound\n" if bound else "already bound\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

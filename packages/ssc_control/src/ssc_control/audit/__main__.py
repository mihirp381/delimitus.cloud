"""``python -m ssc_control.audit verify --org <org_id>``: exit 0 when the chain holds, 1 when not.

Connects with ``SSC_DATABASE_DSN``, the setting the API uses.
"""

import argparse
import asyncio
import os
import sys

from ssc_control.audit.verify import VerifyReport, verify
from ssc_control.db.bind import bound_org, check_org_id
from ssc_control.db.engine import make_engine

DSN_ENV = "SSC_DATABASE_DSN"


async def run_verify(dsn: str, org_id: str) -> VerifyReport:
    engine = make_engine(dsn)
    try:
        snapshot = engine.execution_options(isolation_level="REPEATABLE READ")
        async with bound_org(snapshot, org_id) as conn:
            return await verify(conn, org_id)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.audit")
    commands = parser.add_subparsers(dest="command", required=True)
    verify_cmd = commands.add_parser("verify", help="walk one org's chain from genesis")
    verify_cmd.add_argument("--org", required=True, type=check_org_id)
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        parser.error(f"{DSN_ENV} is not set")
    report = asyncio.run(run_verify(dsn, args.org))
    if report.first_broken is None:
        sys.stdout.write(f"ok: {report.checked} events, head at seq {report.head_seq}\n")
        return 0
    broken = report.first_broken
    sys.stdout.write(f"broken: seq {broken.seq} cause {broken.cause}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""``python -m ssc_control.audit``: the audit log from a shell (decision 012).

* ``verify --org <id> [--anchors]``: exit 0 when the chain holds (and, with ``--anchors``, every
  anchor agrees with it and the newest is under 36 hours old), 1 when not.
* ``anchor --org <id>``: write today's anchor now, as the daily job would.
* ``reanchor --org <id> --restored-to <rfc3339> --ref <ticket>``: step 4 of the restore
  procedure; exit 1, writing nothing, when the restored chain does not hold.

Connects with ``SSC_DATABASE_DSN``, the setting the API uses. Anchors live in the blob store the
API and the worker use (``SSC_BLOB_*``); without one these commands exit 2.
"""

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime

from ssc_control.audit.anchor import (
    STALE_AFTER,
    Anchor,
    AnchorConflictError,
    AnchorReport,
    ReanchorRefusedError,
    reanchor,
    verify_anchors,
    write_anchor,
)
from ssc_control.audit.verify import VerifyReport, verify
from ssc_control.db.bind import bound_org, check_org_id
from ssc_control.db.engine import make_engine
from ssc_control.storage import StorageConfigError, blob_store_from_env
from ssc_shared.blobstore import BlobStore

DSN_ENV = "SSC_DATABASE_DSN"


async def run_verify(dsn: str, org_id: str) -> VerifyReport:
    engine = make_engine(dsn)
    try:
        snapshot = engine.execution_options(isolation_level="REPEATABLE READ")
        async with bound_org(snapshot, org_id) as conn:
            return await verify(conn, org_id)
    finally:
        await engine.dispose()


async def run_verify_anchors(
    dsn: str, org_id: str, blob: BlobStore, *, now: datetime
) -> tuple[VerifyReport, AnchorReport]:
    engine = make_engine(dsn)
    try:
        return await verify_anchors(engine, org_id, blob, now=now)
    finally:
        await engine.dispose()


async def run_anchor(dsn: str, org_id: str, blob: BlobStore) -> Anchor:
    engine = make_engine(dsn)
    try:
        async with bound_org(engine, org_id) as conn:
            return await write_anchor(conn, org_id, blob, "daily")
    finally:
        await engine.dispose()


async def run_reanchor(
    dsn: str, org_id: str, blob: BlobStore, *, restored_to: datetime, ref: str
) -> Anchor:
    engine = make_engine(dsn)
    try:
        now = datetime.now(UTC)
        return await reanchor(engine, org_id, blob, restored_to=restored_to, ref=ref, now=now)
    finally:
        await engine.dispose()


def rfc3339(value: str) -> datetime:
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an RFC 3339 time: {value!r}") from exc
    if at.utcoffset() is None:
        raise argparse.ArgumentTypeError("the time needs a UTC offset, such as Z or +00:00")
    return at


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")


def _chain_lines(report: VerifyReport) -> list[str]:
    if report.first_broken is None:
        return [f"ok: {report.checked} events, head at seq {report.head_seq}"]
    broken = report.first_broken
    return [f"broken: seq {broken.seq} cause {broken.cause}"]


def _anchor_lines(report: AnchorReport) -> list[str]:
    newest = report.newest
    at = "none yet" if newest is None else f"newest seq {newest.seq} at {newest.object_key}"
    lines = [f"anchors: {report.checked} checked, {report.superseded} superseded, {at}"]
    for problem in report.problems:
        if problem.cause == "stale":
            hours = int(STALE_AFTER.total_seconds() // 3600)
            lines.append(f"anchor stale: no anchor in the last {hours} hours")
        else:
            lines.append(f"anchor {problem.cause}: {problem.key} seq {problem.seq}")
    return lines


def _blob_store(parser: argparse.ArgumentParser) -> BlobStore:
    try:
        blob = blob_store_from_env(os.environ)
    except StorageConfigError as exc:
        parser.error(str(exc))
    if blob is None:
        parser.error("anchors need a blob store; SSC_BLOB_BACKEND is none")
    return blob


def _verify(parser: argparse.ArgumentParser, dsn: str, args: argparse.Namespace) -> int:
    if not args.anchors:
        report = asyncio.run(run_verify(dsn, args.org))
        _say(_chain_lines(report)[0])
        return 0 if report.ok else 1
    blob = _blob_store(parser)
    chain, anchors = asyncio.run(run_verify_anchors(dsn, args.org, blob, now=datetime.now(UTC)))
    for line in _chain_lines(chain) + _anchor_lines(anchors):
        _say(line)
    return 0 if chain.ok and anchors.ok else 1


def _anchor(parser: argparse.ArgumentParser, dsn: str, args: argparse.Namespace) -> int:
    blob = _blob_store(parser)
    try:
        anchor = asyncio.run(run_anchor(dsn, args.org, blob))
    except AnchorConflictError as exc:
        _say(f"refused: {exc}")
        return 1
    _say(f"anchored: seq {anchor.seq} at {anchor.object_key}")
    return 0


def _reanchor(parser: argparse.ArgumentParser, dsn: str, args: argparse.Namespace) -> int:
    blob = _blob_store(parser)
    try:
        anchor = asyncio.run(
            run_reanchor(dsn, args.org, blob, restored_to=args.restored_to, ref=args.ref)
        )
    except ReanchorRefusedError as exc:
        for line in _chain_lines(exc.chain) + _anchor_lines(exc.anchors):
            _say(line)
        _say("refused: nothing written")
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    _say(f"reanchored: seq {anchor.seq} at {anchor.object_key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.audit")
    commands = parser.add_subparsers(dest="command", required=True)
    verify_cmd = commands.add_parser("verify", help="walk one org's chain from genesis")
    verify_cmd.add_argument("--org", required=True, type=check_org_id)
    verify_cmd.add_argument(
        "--anchors", action="store_true", help="also check every anchor in the blob store"
    )
    anchor_cmd = commands.add_parser("anchor", help="write today's anchor of one org now")
    anchor_cmd.add_argument("--org", required=True, type=check_org_id)
    reanchor_cmd = commands.add_parser(
        "reanchor", help="re-anchor one org after a point-in-time restore (decision 012)"
    )
    reanchor_cmd.add_argument("--org", required=True, type=check_org_id)
    reanchor_cmd.add_argument("--restored-to", required=True, type=rfc3339)
    reanchor_cmd.add_argument("--ref", required=True, help="the incident or change ticket")
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        parser.error(f"{DSN_ENV} is not set")
    command = {"verify": _verify, "anchor": _anchor, "reanchor": _reanchor}[args.command]
    return command(parser, dsn, args)


if __name__ == "__main__":
    sys.exit(main())

"""``python -m ssc_control.cells``: the operator's record of the cells (ticket 19).

* ``import-env --stage S --operator op_x [--file PATH]``: the cells of ``SSC_CELLS`` (or the file),
  written as ``ready``; a second run changes nothing.
* ``request --label L --stage S [--org-id ORG] --operator op_x``: a ``building`` cell.
* ``ready --label L --jwks-file PATH --project-number N --operator op_x``: its public keys.
* ``fail --label L --reason TEXT --operator op_x``: a build that did not finish.
* ``retire --label L --operator op_x``: no longer served; never deleted.
* ``status --label L`` and ``list``: one line per cell, never its keys.

Every command connects with ``SSC_DATABASE_DSN``.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_contracts.audit import ActorKind
from ssc_control.audit.chain import Actor
from ssc_control.cells import service
from ssc_control.db import check_org_id, make_engine
from ssc_control.identity.operator import check_operator_id, check_reason
from ssc_control.runtime.cells import CELLS_ENV, check_public_jwks, parse_cells
from ssc_shared import redaction
from ssc_shared.hosts import check_cell_label


def _project_number(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("a project number is a positive integer")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.cells")
    sub = parser.add_subparsers(dest="command", required=True)
    stages = ["staging", "prod"]
    p_import = sub.add_parser("import-env")
    p_import.add_argument("--stage", choices=stages, required=True)
    p_import.add_argument("--file", type=Path)
    p_request = sub.add_parser("request")
    p_request.add_argument("--stage", choices=stages, required=True)
    p_request.add_argument("--org-id", type=check_org_id)
    p_ready = sub.add_parser("ready")
    p_ready.add_argument("--jwks-file", type=Path, required=True)
    p_ready.add_argument("--project-number", type=_project_number, required=True)
    p_fail = sub.add_parser("fail")
    p_fail.add_argument("--reason", type=check_reason, required=True)
    p_retire = sub.add_parser("retire")
    p_status = sub.add_parser("status")
    sub.add_parser("list")
    for p in (p_request, p_ready, p_fail, p_retire, p_status):
        p.add_argument("--label", type=check_cell_label, required=True)
    for p in (p_import, p_request, p_ready, p_fail, p_retire):
        p.add_argument("--operator", type=check_operator_id, required=True)
    return parser


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as e:
        raise service.CellError(f"cannot read {path}: {e.strerror}") from None


def _jwks(path: Path) -> dict[str, object]:
    try:
        doc: object = json.loads(_read(path))
    except ValueError:
        raise service.CellError(f"{path} is not JSON") from None
    return dict(check_public_jwks(doc, str(path)))


async def _imported(engine: AsyncEngine, args: argparse.Namespace, actor: Actor) -> str:
    raw = _read(args.file) if args.file else os.environ.get(CELLS_ENV, "")
    if not raw:
        raise service.CellError(f"{CELLS_ENV} is not set and no --file was given")
    cells = parse_cells(raw)
    imported, unchanged = await service.import_cells(engine, cells, stage=args.stage, actor=actor)
    return f"imported {' '.join(imported) or 'none'}; unchanged {' '.join(unchanged) or 'none'}"


async def _lines(engine: AsyncEngine, args: argparse.Namespace) -> str:
    actor = Actor(kind=ActorKind.OPERATOR, id=getattr(args, "operator", "op_none"))
    commands: dict[str, Callable[[], Awaitable[service.Cell]]] = {
        "request": lambda: service.request(
            engine, args.label, stage=args.stage, org_id=args.org_id, actor=actor
        ),
        "ready": lambda: service.ready(
            engine,
            args.label,
            jwks=_jwks(args.jwks_file),
            project_number=args.project_number,
            actor=actor,
        ),
        "fail": lambda: service.fail(engine, args.label, reason=args.reason, actor=actor),
        "retire": lambda: service.retire(engine, args.label, actor=actor),
        "status": lambda: service.get(engine, args.label),
    }
    if args.command == "import-env":
        return await _imported(engine, args, actor)
    if args.command == "list":
        return "\n".join(c.line for c in await service.list_all(engine))
    return (await commands[args.command]()).line


async def run(dsn: str, args: argparse.Namespace) -> str:
    engine = make_engine(dsn)
    try:
        return await _lines(engine, args)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    redaction.install()
    dsn = os.environ.get("SSC_DATABASE_DSN")
    if not dsn:
        sys.stderr.write("SSC_DATABASE_DSN is not set\n")
        return 2
    try:
        out = asyncio.run(run(dsn, args))
    except ValueError as e:
        sys.stderr.write(f"refused: {e}\n")
        return 1
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

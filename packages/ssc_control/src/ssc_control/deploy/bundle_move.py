"""``python -m ssc_control.deploy.bundle_move [--org ORG] [--apply]``: move the source bundles
the control store still holds into each org's cell bucket (decision 015, amended).

Lists ``bundles/<org>/`` in the control store (``SSC_BLOB_*``) for ``--org``, or for every org in
``org_index``. Without ``--apply`` it only says what it would move. With it, each object is
copied to the org's cell bucket (``SSC_CELL_BUCKET_TEMPLATE``, ``storage.org_bundle_store``)
under the same key, the copy's size and SHA-256 are checked against the source and the key's
digest, and only then is the source deleted. An object the cell already holds with the same
size and SHA-256 is not copied again, only its source deleted, so a run that stopped halfway is
run again. A source whose SHA-256 is not its key's, or a copy that does not match, is left where
it is and counted ``failed``.

Prints one line per object, then ``moved N, already there N, failed N, skipped N`` (a dry run:
``would move N, ...``). Exit 0, or 1 when anything failed. Run with the worker's settings:
``SSC_DATABASE_DSN``, ``SSC_BLOB_*`` and ``SSC_CELL_BUCKET_TEMPLATE``; without a blob store or a
cell template it exits 2.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from ssc_control.db.bind import check_org_id
from ssc_control.db.engine import make_engine
from ssc_control.db.orgs import all_org_ids
from ssc_control.deploy.bundles import BUNDLE_KEY
from ssc_control.storage import (
    CellStores,
    StorageConfigError,
    blob_store_from_env,
    cell_stores_from_env,
    org_bundle_store,
)
from ssc_shared.blobstore import BlobError, BlobInfo, BlobStore

DSN_ENV: Final = "SSC_DATABASE_DSN"
NO_STORES: Final = "moving bundles needs SSC_BLOB_* (the source) and SSC_CELL_BUCKET_TEMPLATE"

type Outcome = Literal["moved", "would_move", "present", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class Moved:
    moved: int = 0
    would_move: int = 0
    present: int = 0
    """Already in the cell with the same size and SHA-256; the source is deleted (or would be)."""
    failed: int = 0
    skipped: int = 0
    """Keys under ``bundles/<org>/`` that are not bundle keys; left alone."""

    def __add__(self, other: Moved) -> Moved:
        return Moved(
            self.moved + other.moved,
            self.would_move + other.would_move,
            self.present + other.present,
            self.failed + other.failed,
            self.skipped + other.skipped,
        )

    def summary(self, *, apply: bool) -> str:
        first = f"moved {self.moved}" if apply else f"would move {self.would_move}"
        return (
            f"{first}, already there {self.present}, failed {self.failed}, skipped {self.skipped}"
        )


def _same(info: BlobInfo | None, size: int, sha256: str) -> bool:
    return info is not None and info.size == size and info.sha256 == sha256


@dataclass(frozen=True, slots=True)
class Mover:
    """From ``source`` to each org's cell bucket in ``cells``; a dry run unless ``apply``.
    ``say`` is told one line per object."""

    source: BlobStore
    cells: CellStores
    apply: bool
    say: Callable[[str], None]

    async def run(self, dsn: str, org_id: str | None) -> Moved | None:
        """``org`` for ``org_id``, or for every org; None when ``org_id`` names no org."""
        engine = make_engine(dsn)
        try:
            orgs = await all_org_ids(engine)
            if org_id is not None:
                if org_id not in orgs:
                    return None
                orgs = [org_id]
            total = Moved()
            for org in orgs:
                total += await self.org(engine, org)
            return total
        finally:
            await engine.dispose()

    async def org(self, engine: AsyncEngine, org_id: str) -> Moved:
        """Move ``bundles/<org_id>/`` to the org's cell bucket."""
        cell = await org_bundle_store(
            engine, org_id, blob_store=self.source, cell_stores=self.cells
        )
        if cell is None or cell is self.source:
            raise StorageConfigError(NO_STORES)
        counts: dict[Outcome, int] = dict.fromkeys(
            ("moved", "would_move", "present", "failed", "skipped"), 0
        )
        listed = [info async for info in self.source.list(f"bundles/{org_id}/")]
        for info in listed:
            outcome, why = await self._one(cell, info)
            counts[outcome] += 1
            self.say(f"{outcome}: {info.key} ({info.size} bytes): {why}")
        return Moved(**{k: v for k, v in counts.items()})

    async def _one(self, cell: BlobStore, info: BlobInfo) -> tuple[Outcome, str]:
        match = BUNDLE_KEY.fullmatch(info.key)
        if match is None:
            return "skipped", "not a bundle key"
        if info.sha256 != match[3]:
            return "failed", "the source's sha256 is not its key's; left in place"
        if _same(await cell.stat(info.key), info.size, match[3]):
            if not self.apply:
                return "present", "already in the cell"
            await self.source.delete(info.key)
            return "present", "already in the cell; source deleted"
        if not self.apply:
            return "would_move", "would copy to the cell"
        return await self._copy(cell, info, match[3])

    async def _copy(self, cell: BlobStore, info: BlobInfo, sha256: str) -> tuple[Outcome, str]:
        try:
            await cell.put(info.key, self.source.get(info.key), size=info.size, sha256=sha256)
        except BlobError as exc:
            return "failed", f"copy refused ({type(exc).__name__}); source kept"
        if not _same(await cell.stat(info.key), info.size, sha256):
            return "failed", "the copy does not match; source kept"
        await self.source.delete(info.key)
        return "moved", "copied, checked, source deleted"


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ssc_control.deploy.bundle_move")
    parser.add_argument("--org", type=check_org_id, help="one org; every org without it")
    parser.add_argument(
        "--apply", action="store_true", help="move the objects; without it, only list them"
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        parser.error(f"{DSN_ENV} is not set")
    try:
        source, cells = blob_store_from_env(os.environ), cell_stores_from_env(os.environ)
    except StorageConfigError as exc:
        parser.error(str(exc))
    if source is None or cells is None:
        parser.error(NO_STORES)
    moved = asyncio.run(Mover(source, cells, args.apply, _say).run(dsn, args.org))
    if moved is None:
        _say(f"no such org: {args.org}")
        return 1
    _say(moved.summary(apply=args.apply))
    return 1 if moved.failed else 0


if __name__ == "__main__":
    sys.exit(main())

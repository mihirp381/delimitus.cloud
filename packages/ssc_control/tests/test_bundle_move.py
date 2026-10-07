"""``python -m ssc_control.deploy.bundle_move``: bundles the control store still holds move to
each org's cell bucket (decision 015, amended), against postgres:18 and filesystem stores.

  * a dry run lists and changes nothing                -> test_a_dry_run_only_lists
  * --apply copies to the org's own cell, checks size and sha256, deletes the source; running
    it again moves nothing                             -> test_apply_moves_each_org_into_its_cell
  * an object already in the cell is not copied again  -> test_an_object_already_in_the_cell_...
  * a source whose sha256 is not its key's stays        -> test_a_corrupt_source_is_left_and_fails
  * --org limits the run; an unknown org exits 1       -> test_one_org_and_an_unknown_org
  * no blob store or no cell template exits 2           -> test_the_command_needs_both_stores
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from ssc_testkit import Dsns
from test_bundle_gc import Org, make_org

from ssc_control.db import bind_org_sync
from ssc_control.deploy import bundle_move
from ssc_control.deploy.bundle_move import Moved, Mover
from ssc_control.deploy.bundles import bundle_key
from ssc_control.storage import cell_stores
from ssc_shared.blobstore import BlobStore
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.clock import SystemClock

KEYS = {"k1": b"m" * 32}


class Stores:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.control = self.store("control")
        self.buckets: dict[str, FsBlobStore] = {}
        self.cells = cell_stores("cells-{cell}", bucket=self.bucket)

    def store(self, name: str) -> FsBlobStore:
        signer = UrlSigner(KEYS, active="k1", clock=SystemClock())
        return FsBlobStore(self.root / name, signer=signer, base_url="http://blobs.test")

    def bucket(self, name: str) -> BlobStore:
        self.buckets[name] = self.store(name)
        return self.buckets[name]

    def cell_of(self, dsn: str, o: Org) -> FsBlobStore:
        with psycopg.connect(dsn) as conn:
            bind_org_sync(conn, o.id)
            row = conn.execute("select cell_label from ssc.org where id = %s", (o.id,)).fetchone()
        assert row is not None
        self.cells(str(row[0]))
        return self.buckets[f"cells-{row[0]}"]


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    return Stores(tmp_path)


def body(label: str) -> bytes:
    return f"source of {label}\n".encode()


async def seed(store: BlobStore, o: Org, label: str) -> str:
    data = body(label)
    key = bundle_key(o.id, o.app, "sha256:" + hashlib.sha256(data).hexdigest())
    await store.put(key, data)
    return key


async def keys_in(store: FsBlobStore) -> set[str]:
    return {i.key async for i in store.list("")}


def mover(stores: Stores, lines: list[str], *, apply: bool) -> Mover:
    return Mover(stores.control, stores.cells, apply, lines.append)


async def test_a_dry_run_only_lists(dsns: Dsns, stores: Stores) -> None:
    o = await make_org(dsns.app)
    key = await seed(stores.control, o, "a")
    lines: list[str] = []
    assert await mover(stores, lines, apply=False).run(dsns.app, o.id) == Moved(would_move=1)
    assert lines == [f"would_move: {key} ({len(body('a'))} bytes): would copy to the cell"]
    assert await keys_in(stores.control) == {key}
    assert await keys_in(stores.cell_of(dsns.app, o)) == set()


async def test_apply_moves_each_org_into_its_cell(dsns: Dsns, stores: Stores) -> None:
    a, b = await make_org(dsns.app), await make_org(dsns.app)
    keys = {o.id: {await seed(stores.control, o, f"{o.id}-{n}") for n in (1, 2)} for o in (a, b)}
    # Something else under the org's prefix is not a bundle and stays.
    await stores.control.put(f"bundles/{a.id}/notes.txt", b"x")
    lines: list[str] = []
    moved = await mover(stores, lines, apply=True).run(dsns.app, None)
    assert moved == Moved(moved=4, skipped=1)
    assert moved.summary(apply=True) == "moved 4, already there 0, failed 0, skipped 1"
    for o in (a, b):
        cell = stores.cell_of(dsns.app, o)
        assert await keys_in(cell) == keys[o.id]
        for key in keys[o.id]:
            assert b"".join([c async for c in cell.get(key)]).startswith(b"source of ")
    assert await keys_in(stores.control) == {f"bundles/{a.id}/notes.txt"}
    # Again: nothing left to move.
    assert await mover(stores, [], apply=True).run(dsns.app, None) == Moved(skipped=1)


async def test_an_object_already_in_the_cell_is_not_copied_again(
    dsns: Dsns, stores: Stores
) -> None:
    o = await make_org(dsns.app)
    key = await seed(stores.control, o, "twice")
    cell = stores.cell_of(dsns.app, o)
    await seed(cell, o, "twice")  # a run that stopped after the copy
    before = await cell.stat(key)
    lines: list[str] = []
    assert await mover(stores, lines, apply=True).run(dsns.app, o.id) == Moved(present=1)
    assert lines[0].endswith("already in the cell; source deleted")
    assert await keys_in(stores.control) == set()
    assert await cell.stat(key) == before


def test_a_corrupt_source_is_left_and_fails(
    dsns: Dsns, stores: Stores, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    o = asyncio.run(make_org(dsns.app))
    key = bundle_key(o.id, o.app, "sha256:" + "0" * 64)
    asyncio.run(stores.control.put(key, b"not those bytes"))
    use(monkeypatch, dsns, stores)
    assert bundle_move.main(["--apply"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"failed: {key} (15 bytes): the source's sha256 is not its key's; left in place",
        "moved 0, already there 0, failed 1, skipped 0",
    ]
    assert asyncio.run(keys_in(stores.control)) == {key}
    assert asyncio.run(keys_in(stores.cell_of(dsns.app, o))) == set()


def test_one_org_and_an_unknown_org(
    dsns: Dsns, stores: Stores, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = asyncio.run(make_org(dsns.app)), asyncio.run(make_org(dsns.app))
    ka, kb = asyncio.run(seed(stores.control, a, "a")), asyncio.run(seed(stores.control, b, "b"))
    use(monkeypatch, dsns, stores)
    assert bundle_move.main(["--org", a.id, "--apply"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "moved 1, already there 0, failed 0, skipped 0"
    )
    assert asyncio.run(keys_in(stores.control)) == {kb}
    assert asyncio.run(keys_in(stores.cell_of(dsns.app, a))) == {ka}
    assert bundle_move.main(["--org", "org_" + "z" * 20]) == 1
    assert capsys.readouterr().out == f"no such org: org_{'z' * 20}\n"


def use(monkeypatch: pytest.MonkeyPatch, dsns: Dsns, stores: Stores) -> None:
    """The command reads these stores instead of building GCS ones from the environment."""
    monkeypatch.setenv("SSC_DATABASE_DSN", dsns.app)
    monkeypatch.setattr(bundle_move, "blob_store_from_env", lambda _env: stores.control)
    monkeypatch.setattr(bundle_move, "cell_stores_from_env", lambda _env: stores.cells)


@pytest.mark.parametrize("missing", ["blob", "cells"])
def test_the_command_needs_both_stores(
    dsns: Dsns, stores: Stores, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    use(monkeypatch, dsns, stores)
    none: Callable[[object], None] = lambda _env: None  # noqa: E731
    target = "blob_store_from_env" if missing == "blob" else "cell_stores_from_env"
    monkeypatch.setattr(bundle_move, target, none)
    with pytest.raises(SystemExit) as exc:
        bundle_move.main([])
    assert exc.value.code == 2

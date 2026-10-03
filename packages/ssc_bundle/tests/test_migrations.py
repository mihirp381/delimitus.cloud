"""SSC-043: the migration ledgers the build finds in the source, in the order each tool applies
them, so the last name of each is the release's latest."""

import json

import pytest

from ssc_bundle.analyze import analyze
from ssc_bundle.migrations import MAX_NAME, ledgers
from ssc_contracts.manifest import Manifest


def found(files: dict[str, str | None]) -> dict[str, tuple[str, ...]]:
    return ledgers({p: None if v is None else v.encode() for p, v in files.items()})


def alembic(revision: str, down: str) -> str:
    return f'"""A migration."""\n\nrevision = "{revision}"\ndown_revision = {down}\n'


def test_prisma_names_each_folder_in_timestamp_order() -> None:
    assert found(
        {
            "prisma/schema.prisma": "",
            "prisma/migrations/migration_lock.toml": "",
            "prisma/migrations/20261002090000_add_total/migration.sql": "",
            "prisma/migrations/20261001090000_init/migration.sql": "",
        }
    ) == {"prisma": ("20261001090000_init", "20261002090000_add_total")}
    assert found({"db/migrations/0001_x/migration.sql": ""}) == {}
    assert found(
        {"db/migrations/migration_lock.toml": "", "db/migrations/0001_x/migration.sql": ""}
    ) == {"prisma": ("0001_x",)}
    assert found({"apps/api/prisma/migrations/0001_x/migration.sql": ""}) == {"prisma": ("0001_x",)}


def test_alembic_follows_down_revision_not_file_names() -> None:
    files = {
        "alembic/env.py": "",
        "alembic/versions/__init__.py": "",
        "alembic/versions/a_third.py": alembic("c3", '"b2"'),
        "alembic/versions/b_first.py": alembic("a1", "None"),
        "alembic/versions/c_second.py": alembic("b2", "'a1'"),
    }
    assert found(files) == {"alembic": ("a1", "b2", "c3")}


def test_alembic_merges_and_unreadable_files() -> None:
    files: dict[str, str | None] = {
        "migrations/env.py": "",
        "migrations/versions/base.py": alembic("base", "None"),
        "migrations/versions/left.py": alembic("left", '"base"'),
        "migrations/versions/right.py": alembic("right", '"base"'),
        "migrations/versions/merge.py": alembic("merge", '("left", "right")'),
        "migrations/versions/typed.py": (
            'revision: str = "typed"\ndown_revision: Union[str, None] = "merge"\n'
        ),
        "migrations/versions/huge_one.py": None,
    }
    assert found(files) == {
        "alembic": ("base", "huge_one", "left", "right", "merge", "typed"),
    }
    assert found({"versions/x.py": alembic("x", "None")}) == {}


def test_django_needs_manage_py_and_a_migrations_package() -> None:
    files = {
        "manage.py": "",
        "shop/migrations/__init__.py": "",
        "shop/migrations/0002_order_total.py": "",
        "shop/migrations/0001_initial.py": "",
        "blog/migrations/__init__.py": "",
        "blog/migrations/0001_initial.py": "",
        "loose/migrations/0001_initial.py": "",
    }
    assert found(files) == {
        "django": ("blog.0001_initial", "shop.0001_initial", "shop.0002_order_total")
    }
    del files["manage.py"]
    assert found(files) == {}


def test_drizzle_reads_the_journal_in_idx_order() -> None:
    journal = {
        "version": "7",
        "entries": [
            {"idx": 1, "tag": "0001_add_total"},
            {"idx": 0, "tag": "0000_init"},
            {"idx": True, "tag": "not_an_index"},
            {"idx": 2},
            "junk",
        ],
    }
    assert found({"drizzle/meta/_journal.json": json.dumps(journal)}) == {
        "drizzle": ("0000_init", "0001_add_total")
    }
    assert found({"drizzle/meta/_journal.json": "{not json"}) == {}
    assert found({"drizzle/meta/_journal.json": "[]"}) == {}


def test_knex_reads_its_directory_from_the_knexfile() -> None:
    files = {
        "knexfile.js": "module.exports = { client: 'pg', migrations: { directory: './db/m' } };",
        "db/m/20261001_init.js": "",
        "db/m/20261002_add_total.ts": "",
        "db/m/types.d.ts": "",
        "db/m/README.md": "",
        "db/m/nested/20261003_no.js": "",
        "migrations/20261004_default_folder.js": "",
    }
    assert found(files) == {"knex": ("20261001_init.js", "20261002_add_total.ts")}
    assert found({"knexfile.ts": "export default {}", "migrations/0001_a.js": ""}) == {
        "knex": ("0001_a.js",)
    }
    escape = {"knexfile.js": "migrations: { directory: '../elsewhere' }", "elsewhere/a.js": ""}
    assert found(escape) == {}


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/pkg/prisma/migrations/0001_x/migration.sql",
        ".venv/lib/prisma/migrations/0001_x/migration.sql",
        "app/__pycache__/prisma/migrations/0001_x/migration.sql",
    ],
)
def test_dependencies_and_caches_never_count(path: str) -> None:
    assert found({path: ""}) == {}


def test_a_name_too_long_is_left_out() -> None:
    long = "x" * (MAX_NAME + 1)
    files = {
        "prisma/migrations/0001_ok/migration.sql": "",
        f"prisma/migrations/{long}/migration.sql": "",
    }
    assert found(files) == {"prisma": ("0001_ok",)}


def test_analyze_carries_the_ledgers() -> None:
    files = [
        ("package.json", b'{"scripts": {"start": "node server.js"}}'),
        ("prisma/migrations/20261001090000_init/migration.sql", b"select 1;"),
    ]
    analysis = analyze(files, Manifest.model_validate({"schema": "ssc/v1"}))
    assert analysis.migrations == {"prisma": ("20261001090000_init",)}
    assert analyze([], Manifest.model_validate({"schema": "ssc/v1"})).migrations == {}

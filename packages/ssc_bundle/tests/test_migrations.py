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


def test_knex_without_a_knexfile_reads_the_folder_its_code_migrates() -> None:
    """B-10: ``knex.migrate.latest()`` in the code and ``migrations/`` at the root."""
    files = {
        "package.json": json.dumps({"dependencies": {"knex": "3.1.0", "pg": "8.13.0"}}),
        "server.js": "const db = knex(config);\nawait db.migrate.latest();\n",
        "migrations/20261001_init.js": "",
        "migrations/20261002_add_total.js": "",
        "migrations/nested/20261003_no.js": "",
    }
    assert found(files) == {"knex": ("20261001_init.js", "20261002_add_total.js")}
    named = {
        "index.ts": "await knex.migrate.latest({ directory: './db/m' });",
        "db/m/0001_a.ts": "",
        "migrations/0002_not_these.js": "",
    }
    assert found(named) == {"knex": ("0001_a.ts",)}
    assert found({"migrations/0001_a.js": "", "server.js": "listen()"}) == {}


def test_sequelize_reads_its_rc_file_or_the_default_folder() -> None:
    files = {
        "package.json": json.dumps(
            {"dependencies": {"sequelize": "6.37.0"}, "devDependencies": {"sequelize-cli": "6"}}
        ),
        "migrations/20261001000000-create-user.js": "",
        "migrations/20261002000000-add-total.js": "",
        "migrations/README.md": "",
    }
    assert found(files) == {
        "sequelize": ("20261001000000-create-user.js", "20261002000000-add-total.js")
    }
    rc = {
        ".sequelizerc": "const path = require('path');\nmodule.exports = {\n"
        "  'config': path.resolve('config', 'database.json'),\n"
        "  'migrations-path': path.resolve('db', 'migrations'),\n};\n",
        "db/migrations/20261001000000-init.cjs": "",
        "migrations/20261009000000-not-these.js": "",
    }
    assert found(rc) == {"sequelize": ("20261001000000-init.cjs",)}
    assert found({"migrations/20261001000000-a.js": ""}) == {}


def test_typeorm_names_each_migration_class_in_timestamp_order() -> None:
    files = {
        "src/migrations/1696000000000-AddTotal.ts": (
            "import { MigrationInterface, QueryRunner } from 'typeorm';\n"
            "export class AddTotal1696000000000 implements MigrationInterface {}\n"
        ),
        "src/migrations/1695000000000-Init.ts": (
            "export class Init1695000000000 implements MigrationInterface {}\n"
        ),
        "src/entity/User.ts": "export class User {}\n",
        "src/types.d.ts": "export class Fake1 implements MigrationInterface {}\n",
    }
    assert found(files) == {"typeorm": ("Init1695000000000", "AddTotal1696000000000")}


def test_node_pg_migrate_reads_its_folder_from_the_scripts() -> None:
    files = {
        "package.json": json.dumps({"dependencies": {"node-pg-migrate": "7.9.0"}}),
        "migrations/1696000000000_init.js": "",
        "migrations/1696000000001_add-total.sql": "",
        "migrations/notes.txt": "",
    }
    assert found(files) == {"node-pg-migrate": ("1696000000000_init", "1696000000001_add-total")}
    scripted = {
        "package.json": json.dumps(
            {
                "scripts": {"migrate": "node-pg-migrate -m db/migrations up"},
                "devDependencies": {"node-pg-migrate": "7.9.0"},
            }
        ),
        "db/migrations/1696000000000_init.ts": "",
        "migrations/1696000000009_not_these.js": "",
    }
    assert found(scripted) == {"node-pg-migrate": ("1696000000000_init",)}


def test_flyway_orders_versioned_scripts_by_version() -> None:
    files = {
        "db/migration/V1__init.sql": "",
        "db/migration/V2__add_total.sql": "",
        "db/migration/V10__later.sql": "",
        "db/migration/V1_1__fix.sql": "",
        "db/migration/R__views.sql": "",
        "db/migration/U2__undo_total.sql": "",
        "db/migration/V3_notes.sql": "",
    }
    assert found(files) == {"flyway": ("V1__init", "V1_1__fix", "V2__add_total", "V10__later")}


def test_apple_double_files_are_not_migrations() -> None:
    """B-9: a macOS tar's ``._`` twin of each file is no migration of any ledger."""
    files = {
        "knexfile.js": "module.exports = {};",
        "migrations/20261001_init.js": "",
        "migrations/._20261001_init.js": "",
        "._knexfile.js": "",
        "prisma/migrations/0001_a/migration.sql": "",
        "prisma/migrations/._0002_b/migration.sql": "",
        "prisma/migrations/0003_c/._migration.sql": "",
        "manage.py": "",
        "shop/migrations/__init__.py": "",
        "shop/migrations/0001_initial.py": "",
        "shop/._migrations/0002_x.py": "",
        "db/env.py": "",
        "db/versions/a1.py": alembic("a1", "None"),
        "db/versions/._a1.py": None,
        "__MACOSX/migrations/20261009_x.js": "",
    }
    assert found(files) == {
        "prisma": ("0001_a",),
        "alembic": ("a1",),
        "django": ("shop.0001_initial",),
        "knex": ("20261001_init.js",),
    }


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

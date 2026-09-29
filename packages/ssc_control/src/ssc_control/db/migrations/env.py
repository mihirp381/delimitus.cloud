"""Alembic environment for the control database.

Runs as the migrator role (``ssc_migrate``), which owns every object. The version table lives in
schema ``ssc`` so the application role, which has no privilege there, cannot touch the ledger.
Configured programmatically by ``ssc_control.db.migrate``; there is no alembic.ini.
"""

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

SCHEMA = "ssc"
VERSION_TABLE = "alembic_version"


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "sqlalchemy.url is not set; run migrations through ssc_control.db.migrate"
        )
    return url


def run_offline() -> None:
    context.configure(
        url=_url(),
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(_url(), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            # Alembic creates the version table before the first revision runs, and it needs
            # the schema to exist. Committed on its own so a failed revision never rolls it back.
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS ssc"))
            conn.commit()
            context.configure(
                connection=conn,
                version_table=VERSION_TABLE,
                version_table_schema=SCHEMA,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()

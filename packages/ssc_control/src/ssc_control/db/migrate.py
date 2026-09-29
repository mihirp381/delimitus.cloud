"""Run Alembic programmatically. There is no alembic.ini; the DSN is the only input."""

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config

from ssc_control.db.engine import sqlalchemy_url

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"


def alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolates '%', so a percent-encoded password must be doubled.
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(dsn).replace("%", "%%"))
    return cfg


def upgrade(dsn: str, revision: str = "head") -> None:
    """Apply migrations as the migrator role. ``dsn`` must authenticate as ``ssc_migrate``."""
    command.upgrade(alembic_config(dsn), revision)


def downgrade(dsn: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(dsn), revision)

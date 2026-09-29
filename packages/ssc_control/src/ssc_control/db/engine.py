"""Engines for the control database. One driver, psycopg 3, for both sync and async use."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DRIVER = "postgresql+psycopg"


def sqlalchemy_url(dsn: str) -> str:
    """Normalise any ``postgresql://`` form to the psycopg 3 dialect SQLAlchemy expects."""
    url = make_url(dsn)
    if url.get_backend_name() != "postgresql":
        raise ValueError(f"control database must be PostgreSQL, got {url.get_backend_name()!r}")
    return url.set(drivername=DRIVER).render_as_string(hide_password=False)


def make_engine(dsn: str) -> AsyncEngine:
    return create_async_engine(sqlalchemy_url(dsn), pool_pre_ping=True)


def make_sync_engine(dsn: str) -> Engine:
    return create_engine(sqlalchemy_url(dsn), pool_pre_ping=True)

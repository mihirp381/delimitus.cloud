"""Per-app Postgres on the customer's own Cloud SQL instance (SSC-040).

Every app environment that declares ``[state] postgres = true`` gets one database and a login
role with ``CONNECTION_LIMIT`` connections. The base tier holds 25 connections: 3 are reserved
for Cloud SQL's own superuser, ``ADMIN_RESERVE`` are kept for the cell agent and an operator, and
the rest is split ``CONNECTION_LIMIT`` per environment, which makes ten on ``db-f1-micro``. A
stateful environment runs ``MAX_INSTANCES`` instance with a pool of ``POOL_SIZE``, so it holds one
connection; the second is kept free for the incoming revision during a deploy or rotation, while
the old one still serves, and for a migration run at start. The cell agent counts the
environments on the instance itself and refuses one past ``ceiling`` with ``DB_TIER_FULL``.
"""

from typing import Final

from ssc_contracts.app_env import DATABASE_CA, DATABASE_URL, PGPASSWORD

CONNECTION_LIMIT: Final = 2
"""Connections one environment's login role may hold at once, across all its instances."""

POOL_SIZE: Final = 1
"""Connections one instance of a stateful app should keep in its pool."""

MAX_INSTANCES: Final = 1
"""The most instances a stateful environment runs, so a new revision always finds a connection."""

ADMIN_RESERVE: Final = 2
"""Ordinary connections never given to apps: the cell agent's and an operator's."""

BASE_TIER: Final = "db-f1-micro"
BASE_TIER_MAX_CONNECTIONS: Final = 25
SUPERUSER_RESERVE: Final = 3
BIGGER_TIER: Final = "db-g1-small"
BIGGER_TIER_MONTHLY_USD: Final = 26

SECRETS: Final = (DATABASE_URL, PGPASSWORD, DATABASE_CA)
"""The secrets the cell agent writes for an app database; it alone ever sees their values."""

POOL_FIX_IT: Final = (
    f"The app runs {MAX_INSTANCES} instance and the database refuses connection "
    f"{CONNECTION_LIMIT + 1}. Set the pool size to {POOL_SIZE}: the other connection must stay "
    f"free for the new version during a deploy or a rotation, while the old one still serves, "
    f"and for a migration run at start. node-pg: new Pool({{ connectionString: "
    f"process.env.DATABASE_URL, max: {POOL_SIZE} }}). Prisma 7: new PrismaPg({{ "
    f"connectionString: process.env.DATABASE_URL, max: {POOL_SIZE} }}). Django: one worker "
    f"with one thread (gunicorn --workers 1 --threads 1), which holds one connection."
)


def ceiling(max_connections: int, reserved_connections: int) -> int:
    """How many app environments an instance holds: its ordinary connections less
    ``ADMIN_RESERVE``, ``CONNECTION_LIMIT`` each. Ten on ``db-f1-micro`` (25, 3 reserved)."""
    return max(0, (max_connections - reserved_connections - ADMIN_RESERVE) // CONNECTION_LIMIT)


def tier_for(places_total: int | None) -> str | None:
    """The instance's tier as told by its places: ``BASE_TIER`` when it holds what that tier holds
    (ten), else None, since only the base tier is ever created."""
    base = ceiling(BASE_TIER_MAX_CONNECTIONS, SUPERUSER_RESERVE)
    return BASE_TIER if places_total == base else None

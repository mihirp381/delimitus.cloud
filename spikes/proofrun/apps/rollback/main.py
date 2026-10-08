"""The proof run's rollback app (GA-4.5). Deployed twice with ``ssc deploy`` by the kit.

- ``/health``: 200 at once.
- ``/``: the migration names this release carries, read from ``alembic/versions``. The app never
  runs them and never connects to its database: the platform reads the names from the source.
"""

import re
from pathlib import Path

from fastapi import FastAPI

app = FastAPI()
VERSIONS = Path(__file__).parent / "alembic" / "versions"
REVISION = re.compile(r"""^revision\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def migrations() -> list[str]:
    """The ``revision`` of each file in the folder, sorted."""
    found = (REVISION.search(p.read_text()) for p in sorted(VERSIONS.glob("*.py")))
    return [m[1] for m in found if m]


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/")
def index() -> dict[str, list[str]]:
    return {"migrations": migrations()}

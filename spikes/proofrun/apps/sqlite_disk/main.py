"""GA-4.9 fixture: keeps SQLite on disk, so doctor and deploy refuse it (STATE_SQLITE_EPHEMERAL)."""

import sqlite3

from fastapi import FastAPI

app = FastAPI()
db = sqlite3.connect("data.db", check_same_thread=False)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}

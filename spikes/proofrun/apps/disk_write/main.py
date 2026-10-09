"""GA-4.9 fixture: uses the home folder, so doctor warns (WRITES_HOME)."""

from pathlib import Path

from fastapi import FastAPI

app = FastAPI()
CACHE = Path.home() / ".cache" / "report"


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}

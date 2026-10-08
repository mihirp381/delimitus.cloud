"""The proof run's timer app (GA-4.1). Deployed with ``ssc deploy`` and ``ssc promote``.

- ``/`` and ``/health``: 200 at once, no work.
- ``/tick`` (GET and POST): the schedule's call. The identity note is verified with
  ``ssc_app.identity``; a verified call is recorded and logged as
  ``TICK role=<role> at=<iso> method=<method>``, a refused one as ``TICK refused=<code>`` with 401.
- ``/ticks``: the calls this instance recorded. They are lost when the instance goes to zero; the
  log line is the durable record.
"""

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ssc_app.identity import IdentityRefused, IdentityVerifier

app = FastAPI()
TICKS: list[dict[str, str]] = []
_verifier: IdentityVerifier | None = None


def verifier() -> IdentityVerifier | None:
    """The identity verifier, built on first use so a missing setting cannot stop ``/health``."""
    global _verifier  # noqa: PLW0603
    keys = os.environ.get("SSC_IDENTITY_KEYS_URL")
    origin = os.environ.get("SSC_APP_ORIGIN")
    if _verifier is None and keys and origin:
        _verifier = IdentityVerifier(audience=origin, keys=keys)
    return _verifier


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


def refuse(code: str) -> JSONResponse:
    print(f"TICK refused={code}", flush=True)
    return JSONResponse({"ok": False, "error": code}, status_code=401)


@app.get("/tick")
@app.post("/tick")
def tick(request: Request) -> Any:
    checker = verifier()
    if checker is None:
        return refuse("not_configured")
    try:
        note = checker.from_headers(request.headers)
    except IdentityRefused as exc:
        return refuse(exc.code)
    at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    TICKS.append({"at": at, "role": note.role, "subject": note.sub, "method": request.method})
    print(f"TICK role={note.role} at={at} method={request.method}", flush=True)
    return {"ok": True, "role": note.role, "count": len(TICKS)}


@app.get("/ticks")
def ticks() -> list[dict[str, str]]:
    return TICKS

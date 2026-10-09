"""The proof run's egress probe app (T6, GA-6.1). Deployed with ``ssc deploy``.

- ``/`` and ``/health``: 200.
- ``/egress?host=&credentials=``: open a tunnel to ``host:443`` through the cell's egress proxy
  (``HTTPS_PROXY``), with or without this app's credential, and say what the proxy and the host
  answered. Never returns the proxy's address or credential.
- ``/hold?host=&seconds=&run=``: open a tunnel to ``host:443`` with the app's credential and
  hold it, a keep-alive request a second, for up to ``seconds``. The answer streams one JSON
  line per event (``open``, ``alive``, ``end``) while the request is in flight, so the instance
  keeps its CPU. When the hold ends, one ``egress hold end`` line is logged with the run, the
  reason and the time it ended. Never returns or logs the proxy's address or credential.
"""

import json
import os
import sys
from collections.abc import Iterator

import tunnel
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/egress")
def egress(host: str, credentials: str = "yes") -> dict[str, object]:
    if not tunnel.valid_host(host) or credentials not in {"yes", "no"}:
        raise HTTPException(status_code=400, detail="host or credentials is not valid")
    return tunnel.probe(host, credentials == "yes", os.environ.get("HTTPS_PROXY"))


def log_end(run: str, host: str, end: dict[str, object]) -> None:
    """One structured line, which Cloud Run turns into ``jsonPayload``."""
    line = {
        "severity": "INFO",
        "message": "egress hold end",
        "hold": {"run": run, "host": host, **{k: v for k, v in end.items() if k != "event"}},
    }
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def hold_lines(run: str, host: str, seconds: int) -> Iterator[str]:
    ended = False
    try:
        for event in tunnel.hold(host, os.environ.get("HTTPS_PROXY"), seconds=seconds):
            if event["event"] == "end":
                ended = True
                log_end(run, host, event)
            yield json.dumps(event) + "\n"
    finally:
        if not ended:
            log_end(run, host, {"reason": "client_gone"})


@app.get("/hold")
def hold(
    host: str,
    seconds: int = Query(90, ge=5, le=150),
    run: str = Query(pattern="^[a-z0-9]{1,32}$"),
) -> StreamingResponse:
    if not tunnel.valid_host(host):
        raise HTTPException(status_code=400, detail="host is not valid")
    return StreamingResponse(hold_lines(run, host, seconds), media_type="application/x-ndjson")

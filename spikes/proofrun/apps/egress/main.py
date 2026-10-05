"""The proof run's egress probe app (T6). Deployed with ``ssc deploy``.

- ``/`` and ``/health``: 200.
- ``/egress?host=&credentials=``: open a tunnel to ``host:443`` through the cell's egress proxy
  (``HTTPS_PROXY``), with or without this app's credential, and say what the proxy and the host
  answered. Never returns the proxy's address or credential.
"""

import os

import tunnel
from fastapi import FastAPI, HTTPException

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

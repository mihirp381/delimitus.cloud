"""The proof run's secrets app (GA-4.6). Deployed with ``ssc deploy`` by the kit.

- ``/`` and ``/health``: 200 at once.
- ``/secret``: what the app was given in ``GA46_TOKEN`` (set with ``ssc secret set``), as a
  fingerprint only: the first 12 hex digits of the SHA-256 of its bytes and its length in bytes.
  The value itself is never returned, printed or logged, so the kit can tell which version a
  deployment carries without anything that could replay it.
"""

import hashlib
import os
from typing import Any

from fastapi import FastAPI

NAME = "GA46_TOKEN"
FINGERPRINT_HEX = 12
app = FastAPI()


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/secret")
def secret() -> dict[str, Any]:
    value = os.environ.get(NAME)
    if value is None:
        return {"name": NAME, "set": False}
    raw = value.encode()
    digest = hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX]
    return {"name": NAME, "set": True, "fingerprint": digest, "length": len(raw)}

"""The proof run's Python API app (T7, T8, T10, T11). Deployed with ``ssc deploy``.

- ``/`` and ``/health``: 200.
- ``/vpc``: how long after this process started its first connection through Direct VPC egress
  (to Google's private range, 199.36.153.8:443) went through, and after how many tries.
- ``/ws``: a WebSocket that sends a tick a second until the other side or the platform ends it.
- ``/deny?project=&secret=&bucket=``: with this app's own identity, ask Secret Manager for the
  latest version of a secret and Cloud Storage for one object name in a bucket. Returns each
  answer's status and Google's reason, never a secret or an object.
"""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

STARTED = time.monotonic()
VPC_TARGET = ("199.36.153.8", 443)
METADATA_TOKEN = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
VPC: dict[str, object] = {"delay_s": None, "attempts": 0, "target": "%s:%d" % VPC_TARGET}

app = FastAPI()


def _wait_for_vpc() -> None:
    """Try the private range every 50 ms for up to two minutes; record the first success."""
    while time.monotonic() - STARTED < 120:
        VPC["attempts"] = int(VPC["attempts"]) + 1
        try:
            with socket.create_connection(VPC_TARGET, timeout=1.0):
                VPC["delay_s"] = round(time.monotonic() - STARTED, 3)
                return
        except OSError:
            time.sleep(0.05)


threading.Thread(target=_wait_for_vpc, daemon=True).start()


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/vpc")
def vpc() -> dict[str, object]:
    return VPC


@app.websocket("/ws")
async def ticks(ws: WebSocket) -> None:
    await ws.accept()
    n = 0
    try:
        while True:
            n += 1
            await ws.send_text(json.dumps({"tick": n, "at": time.time()}))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


def _token() -> str:
    request = urllib.request.Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())["access_token"]


def _ask(url: str, token: str) -> dict[str, object]:
    """One call's status and Google's reason; the body of a successful call is dropped."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return {"status": response.status, "reason": "allowed"}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", {})
        except ValueError:
            error = {}
        reason = f"{error.get('status', '')} {str(error.get('message', ''))[:200]}".strip()
        return {"status": exc.code, "reason": reason}
    except (urllib.error.URLError, OSError) as exc:
        return {"status": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


@app.get("/deny")
def deny(project: str, secret: str, bucket: str) -> dict[str, object]:
    token = _token()
    name = urllib.parse.quote(f"projects/{project}/secrets/{secret}/versions/latest", safe="/")
    return {
        "secret": _ask(f"https://secretmanager.googleapis.com/v1/{name}:access", token),
        "bucket": _ask(
            f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket)}/o"
            "?maxResults=1&fields=items/name",
            token,
        ),
    }

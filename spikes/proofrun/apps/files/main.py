"""The proof run's file storage app (GA-4.2). Deployed with ``ssc deploy``; needs ``[files]``.

Every handler prints one ``FILES op=<op> name=<name> result=<ok|CODE>`` line, never a URL, a
token or file bytes.

- ``/`` and ``/health``: 200.
- ``POST /files/put?name=``: the body is stored, its ``Content-Type`` kept.
- ``GET /files/get?name=``: the stored bytes, or 404 with the code.
- ``GET /files/link?op=put|get&name=``: the broker's link as it is (the kit tests its expiry and
  edits its path).
- ``POST /files/delete?name=``: removes the file.
- ``GET /files/loop?seconds=&name=``: once a second a background thread asks for a ``put`` link and
  records ``LOOP at=<time> result=<ok|CODE>`` in the log. The response streams the same lines and
  may be cut by the platform; the thread is not, so the log keeps what the broker answered.
"""

import queue
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ssc_app import files

LOOP_MAX_SECONDS: Final = 120
END: Final = object()

app = FastAPI()


def say(line: str) -> None:
    print(line, flush=True)


def note(op: str, name: str, result: str) -> None:
    say(f"FILES op={op} name={name} result={result}")


def refusal(exc: files.FilesError) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "message": str(exc)}


@app.get("/")
@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/files/put")
async def put(request: Request, name: str) -> dict[str, Any]:
    data = await request.body()
    content_type = request.headers.get("content-type") or files.DEFAULT_CONTENT_TYPE
    try:
        files.put(name, data, content_type=content_type)
    except files.FilesError as exc:
        note("put", name, exc.code)
        return refusal(exc)
    note("put", name, "ok")
    return {"ok": True, "name": name, "size": len(data)}


@app.get("/files/get", response_model=None)
def get(name: str) -> Response:
    try:
        data = files.get(name)
    except files.FilesError as exc:
        note("get", name, exc.code)
        return JSONResponse(refusal(exc), status_code=404)
    note("get", name, "ok")
    return Response(data, media_type="application/octet-stream")


@app.get("/files/link")
def link(op: str, name: str) -> dict[str, Any]:
    if op not in ("put", "get"):
        return {"ok": False, "code": "VALIDATION_FAILED", "message": "op is put or get"}
    try:
        answer = files.link("put" if op == "put" else "get", name)
    except files.FilesError as exc:
        note(f"link-{op}", name, exc.code)
        return refusal(exc)
    note(f"link-{op}", name, "ok")
    return answer


@app.post("/files/delete")
def delete(name: str) -> dict[str, Any]:
    try:
        files.delete(name)
    except files.FilesError as exc:
        note("delete", name, exc.code)
        return refusal(exc)
    note("delete", name, "ok")
    return {"ok": True, "name": name}


def run_loop(seconds: int, name: str, lines: "queue.Queue[object]") -> None:
    """Ask for a ``put`` link once a second; log and queue one line each time."""
    started = time.monotonic()
    tick = 0
    try:
        while time.monotonic() - started < seconds:
            try:
                files.link("put", name)
                result = "ok"
            except files.FilesError as exc:
                result = exc.code
            line = f"LOOP at={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} result={result}"
            say(line)
            lines.put(line + "\n")
            tick += 1
            time.sleep(max(0.0, started + tick - time.monotonic()))
    finally:
        lines.put(END)


def drain(lines: "queue.Queue[object]") -> Iterator[str]:
    while True:
        item = lines.get()
        if item is END:
            return
        yield str(item)


@app.get("/files/loop")
def loop(name: str, seconds: int = 60) -> StreamingResponse:
    lines: queue.Queue[object] = queue.Queue()
    bounded = max(1, min(seconds, LOOP_MAX_SECONDS))
    threading.Thread(target=run_loop, args=(bounded, name, lines), daemon=True).start()
    return StreamingResponse(drain(lines), media_type="text/plain")

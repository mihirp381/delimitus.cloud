import asyncio
import json
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

import probes

app = FastAPI()


def _run(fn, *a):
    t0 = time.perf_counter()
    out = fn(*a)
    out.setdefault("elapsed_ms", round((time.perf_counter() - t0) * 1000, 1))
    return out


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/probe/egress")
async def egress():
    return await asyncio.to_thread(_run, probes.egress)


@app.get("/probe/dns")
async def dns(zone: str = ""):
    return await asyncio.to_thread(_run, probes.dns, zone)


@app.get("/probe/metadata")
async def metadata():
    return await asyncio.to_thread(_run, probes.metadata)


@app.get("/probe/peer")
async def peer(url: str = ""):
    return await asyncio.to_thread(_run, probes.peer, url)


@app.get("/probe/headers")
def headers(request: Request):
    return _run(probes.headers, dict(request.headers))


@app.get("/probe/sse")
async def sse(seconds: int = 600):
    async def gen():
        start = time.time()
        n = 0
        while time.time() - start < seconds:
            n += 1
            yield f"id: {n}\ndata: {json.dumps({'t': round(time.time() - start, 1)})}\n\n"
            await asyncio.sleep(15)
        yield "event: done\ndata: {}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.websocket("/probe/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    try:
        while True:
            msg = await sock.receive_text()
            await sock.send_text(msg)
    except WebSocketDisconnect:
        pass

"""A WebSocket kept past the 60-minute limit with ``ssc_app.reconnect`` (SSC-090)."""

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ssc_app.reconnect import close_before_deadline, seconds_left

app = FastAPI()


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    n = int(socket.query_params.get("after", "-1"))
    await socket.accept()
    print(f"OPEN after={n} left={seconds_left(socket.headers)}", flush=True)
    restart = asyncio.create_task(close_before_deadline(socket.close, socket.headers))
    try:
        while not restart.done():
            n += 1
            await socket.send_text(str(n))
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        restart.cancel()
        print(f"CLOSED at={n}", flush=True)

"""Streamlit WebSocket origin check through the load balancer.

Loads the page (warms the app), then opens /_stcore/stream with three Origin headers: the same host, a foreign
host, and none. Streamlit's check passes behind our proxy when same-origin opens and foreign is refused.
"""

import argparse
import asyncio
import json

import httpx2
import websockets


async def try_ws(url, origin):
    headers = {"Origin": origin} if origin else {}
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=30):
            return "open"
    except websockets.exceptions.InvalidStatus as e:
        return f"refused {e.response.status_code}"
    except Exception as e:
        return f"error {type(e).__name__}: {e}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="Streamlit base URL through the LB")
    a = p.parse_args()
    base = a.url.rstrip("/")
    page = httpx2.get(base + "/", timeout=120, follow_redirects=True).status_code
    ws = base.replace("https://", "wss://").replace("http://", "ws://") + "/_stcore/stream"
    u = httpx2.URL(base)
    host = f"{u.host}:{u.port}" if u.port else u.host
    scheme = u.scheme
    out = {"page": page}
    for label, origin in (("same", f"{scheme}://{host}"), ("foreign", "https://evil.example"), ("none", "")):
        out[label] = asyncio.run(try_ws(ws, origin))
    out["result"] = "pass" if out["same"] == "open" and out["foreign"] != "open" else "fail"
    print(json.dumps(out))


if __name__ == "__main__":
    main()

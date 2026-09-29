import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx2
import websockets


def get_json(client, endpoint, params=None):
    try:
        r = client.get(endpoint, params=params or {}, timeout=30)
        return r.json()
    except Exception as e:
        return {"result": "unknown", "detail": f"{type(e).__name__}: {e}"}


def first_byte_ms(url, n, gap, timeout=60):
    samples = []
    with httpx2.Client(timeout=timeout) as c:
        for i in range(n):
            t0 = time.perf_counter()
            try:
                with c.stream("GET", url) as r:
                    next(r.iter_bytes(), b"")
                    status = r.status_code
            except Exception as e:
                status = f"{type(e).__name__}"
            samples.append({"ms": round((time.perf_counter() - t0) * 1000, 1), "status": status})
            if i < n - 1:
                time.sleep(gap)
    ms = [s["ms"] for s in samples]
    return {"samples": samples, "p50_ms": statistics.median(ms), "max_ms": max(ms)}


def sse_hold(url, seconds):
    t0 = time.perf_counter()
    events = 0
    try:
        with httpx2.Client(timeout=httpx2.Timeout(60, read=seconds + 60)) as c:
            with c.stream("GET", url, params={"seconds": seconds}) as r:
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        events += 1
        ok = True
    except Exception as e:
        ok = False
        err = f"{type(e).__name__}: {e}"
    held = round(time.perf_counter() - t0, 1)
    out = {"held_s": held, "events": events, "target_s": seconds,
           "result": "pass" if ok and held >= seconds - 20 else "fail"}
    if not ok:
        out["error"] = err
    return out


async def ws_hold(url, seconds):
    t0 = time.perf_counter()
    pings = 0
    try:
        async with websockets.connect(url, ping_interval=None, open_timeout=30) as ws:
            while time.perf_counter() - t0 < seconds:
                await ws.send(f"ping {pings}")
                await asyncio.wait_for(ws.recv(), 20)
                pings += 1
                await asyncio.sleep(20)
        ok, err = True, None
    except Exception as e:
        ok, err = False, f"{type(e).__name__}: {e}"
    held = round(time.perf_counter() - t0, 1)
    out = {"held_s": held, "pings": pings, "target_s": seconds,
           "result": "pass" if ok and held >= seconds - 25 else "fail"}
    if err:
        out["error"] = err
    return out


def main():
    p = argparse.ArgumentParser(description="Run SSC-001 probes against one candidate's deployed apps.")
    p.add_argument("--candidate", required=True, choices=["gcp", "aws", "azure", "fly"])
    p.add_argument("--api-url", required=True, help="probe_api base URL as seen through the internal LB")
    p.add_argument("--static-url", default="")
    p.add_argument("--streamlit-url", default="")
    p.add_argument("--peer-url", default="", help="internal URL of a second app the API should NOT reach")
    p.add_argument("--public-api-url", default="", help="the runtime's default public address, expected 403")
    p.add_argument("--canary-zone", default="")
    p.add_argument("--cold-n", type=int, default=5)
    p.add_argument("--cold-gap", type=float, default=0, help="seconds between cold-start samples")
    p.add_argument("--quick", action="store_true", help="30 s holds instead of 600/1800 s")
    p.add_argument("--skip-holds", action="store_true")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    api = a.api_url.rstrip("/")
    out = {"candidate": a.candidate, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": {}}
    c = out["checks"]
    with httpx2.Client() as client:
        c["egress"] = get_json(client, f"{api}/probe/egress")
        c["dns"] = get_json(client, f"{api}/probe/dns", {"zone": a.canary_zone})
        c["metadata"] = get_json(client, f"{api}/probe/metadata")
        c["peer"] = get_json(client, f"{api}/probe/peer", {"url": a.peer_url})
        try:
            r = client.get(f"{api}/probe/headers", headers={"Authorization": "Bearer " + "x" * 64}, timeout=30)
            h = r.json()
            d = h.get("detail", {})
            h["result"] = "pass" if d.get("authorization_len") == 71 else "fail"
            u = httpx2.URL(api)
            h["host_matches_request"] = d.get("host") in (u.host, f"{u.host}:{u.port}")
            c["headers"] = h
        except Exception as e:
            c["headers"] = {"result": "unknown", "detail": str(e)}
        if a.public_api_url:
            try:
                r = client.get(a.public_api_url.rstrip("/") + "/healthz", timeout=30)
                c["public_ingress"] = {"status": r.status_code, "result": "pass" if r.status_code in (403, 404) else "fail"}
            except Exception as e:
                c["public_ingress"] = {"status": f"{type(e).__name__}", "result": "pass"}

    cold = {}
    for name, url in (("api", api + "/healthz"), ("static", a.static_url), ("streamlit", a.streamlit_url)):
        if url:
            cold[name] = first_byte_ms(url, a.cold_n, a.cold_gap)
    c["cold_start"] = cold

    if not a.skip_holds:
        sse_s, ws_s = (30, 30) if a.quick else (600, 1800)
        c["sse"] = sse_hold(f"{api}/probe/sse", sse_s)
        ws_url = api.replace("https://", "wss://").replace("http://", "ws://") + "/probe/ws"
        c["ws"] = asyncio.run(ws_hold(ws_url, ws_s))

    path = Path(a.out or f"results/{a.candidate}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v.get("result", v) if isinstance(v, dict) else v for k, v in c.items()}, indent=2, default=str))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

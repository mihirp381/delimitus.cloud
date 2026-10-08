"""The proof run's warm option app (GA-4.8). Deployed with ``ssc deploy`` and ``ssc promote``.

Nothing in ``ssc.toml`` but the runtime: no schedules, files, state or egress, so the control
plane creates no cell resource for it.

- ``/health``: when this process started (``started_at``, ISO UTC), its ``pid`` and
  ``uptime_s``. A kept instance answers the same ``started_at`` after an idle hold; a new one
  answers a later one.
- ``/``: a small page with the same two values in ``<meta>`` tags, for a browser page load
  through the gateway.
"""

import os
import time
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()
STARTED_AT = datetime.now(UTC).isoformat(timespec="microseconds")
_STARTED = time.monotonic()
TITLE = "SSC proof run: warm"


def facts() -> dict[str, object]:
    return {
        "started_at": STARTED_AT,
        "pid": os.getpid(),
        "uptime_s": round(time.monotonic() - _STARTED, 3),
    }


@app.get("/health")
def health() -> dict[str, object]:
    return facts()


def page_html() -> str:
    pid = os.getpid()
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        f"<meta name=ssc-started-at content={STARTED_AT}>"
        f"<meta name=ssc-pid content={pid}>"
        f"<title>{TITLE}</title><h1>{TITLE}</h1>"
        f"<p>This instance started at {STARTED_AT} (pid {pid}).</p></html>\n"
    )


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return page_html()

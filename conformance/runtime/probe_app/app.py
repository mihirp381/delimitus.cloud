"""The runtime probe app: what a platform container must allow and refuse, seen from inside.

Standard library only, so the image needs no package install. Listens on ``$PORT`` (no default:
an app that ignores ``PORT`` is the failure this probes). Routes:

- ``/`` and ``/healthz``: 200.
- ``/probe/uid``: the process's uid and gid.
- ``/probe/env``: the environment's variable names, never their values.
- ``/probe/write``: which of ``/``, ``/app`` and ``$HOME`` accept a new file.
"""

import json
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def writable(directory: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-"):
            return True
    except OSError:
        return False


def probe(path: str) -> object | None:
    home = os.environ.get("HOME", "")
    routes = {
        "/": lambda: "ok",
        "/healthz": lambda: "ok",
        "/probe/uid": lambda: {"uid": os.getuid(), "gid": os.getgid()},
        "/probe/env": lambda: {"names": sorted(os.environ)},
        "/probe/write": lambda: {
            "home": home,
            "writable": {d: writable(d) for d in ("/", "/app", home) if d},
        },
    }
    route = routes.get(path)
    return None if route is None else route()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = probe(self.path.split("?", 1)[0])
        status = 404 if body is None else 200
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()  # noqa: S104

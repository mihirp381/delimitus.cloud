"""Holds both of the app's allowed connections open and reports what the instance sees (SSC-040).

Deployed to the preview of pg01 to pg10, so ten apps hold their connections while the cell agent
makes its administration connection. Every 30 s it prints one ``HOLD_RESULT {...}`` line.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

HELD = 2


def report() -> None:
    conns: list[psycopg.Connection] = []
    error = None
    for _ in range(HELD):
        try:
            conns.append(psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10))
        except psycopg.Error as exc:
            error = (exc.sqlstate, str(exc).splitlines()[0][:160])
    while True:
        line: dict[str, object] = {"held": len(conns), "error": error}
        if conns:
            try:
                row = conns[0].execute(
                    "select count(*), current_setting('max_connections')::int,"
                    " current_setting('superuser_reserved_connections')::int"
                    " from pg_stat_activity where backend_type = 'client backend'"
                ).fetchone()
                line["client_backends"], line["max"], line["reserved"] = row or (None, None, None)
            except psycopg.Error as exc:
                line["query_error"] = str(exc).splitlines()[0][:160]
        print("HOLD_RESULT " + json.dumps(line), flush=True)
        time.sleep(30)


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:
        return


threading.Thread(target=report, daemon=True).start()
ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Health).serve_forever()

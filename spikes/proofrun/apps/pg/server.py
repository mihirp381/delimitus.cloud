"""The proof run's database app (T5). Deployed with ``ssc deploy``; ``[state] postgres = true``
gives it its own database on the cell's instance and ``DATABASE_URL``.

- ``/health``: 200.
- ``/db/own``: connect with ``DATABASE_URL`` and name the database.
- ``/db/cross?name=``: connect to another database with this app's own credentials.

Both answer ``{connected, database, sqlstate, error}``; the connection string is never returned.
"""

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def attempt(dbname: str | None) -> dict[str, object]:
    """Connect to ``dbname`` (or this app's own database) and report how it went."""
    url = os.environ["DATABASE_URL"]
    conninfo = make_conninfo(url, dbname=dbname) if dbname else url
    target = dbname or conninfo_to_dict(url).get("dbname")
    try:
        with psycopg.connect(conninfo, connect_timeout=10) as conn:
            row = conn.execute("select current_database()").fetchone()
            return {"connected": True, "database": row[0] if row else target, "sqlstate": None}
    except psycopg.Error as exc:
        message = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        return {"connected": False, "database": target, "sqlstate": exc.sqlstate, "error": message}


class Handler(BaseHTTPRequestHandler):
    """The three routes."""

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        if parts.path in {"/", "/health"}:
            self._send(200, {"ok": True})
        elif parts.path == "/db/own":
            self._send(200, attempt(None))
        elif parts.path == "/db/cross" and query.get("name"):
            self._send(200, attempt(query["name"][0]))
        else:
            self._send(404, {"error": "not found"})

    def _send(self, status: int, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()

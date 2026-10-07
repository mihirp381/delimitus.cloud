"""Django with the URL as given, through dj-database-url (SSC-040)."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dj_database_url
import django
from django.conf import settings

settings.configure(DATABASES={"default": dj_database_url.config(conn_max_age=0)})
django.setup()


def check() -> dict[str, object]:
    from django.db import connection

    try:
        with connection.cursor() as cur:
            cur.execute("select current_database(), (select ssl from pg_stat_ssl where pid = pg_backend_pid())")
            db, ssl = cur.fetchone()
        return {"driver": "django", "version": django.get_version(), "connected": True, "database": db, "ssl": ssl}
    except Exception as exc:  # noqa: BLE001  (the result is the report)
        return {"driver": "django", "version": django.get_version(), "connected": False, "error": str(exc)[:200]}
    finally:
        connection.close()


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, format: str, *args: object) -> None:
        return


print("DRIVER_RESULT " + json.dumps(check()), flush=True)
ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Health).serve_forever()

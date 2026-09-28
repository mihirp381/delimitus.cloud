from __future__ import annotations

import asyncio
import importlib.metadata as md
import json
import os
import pathlib
import sys
from urllib.parse import parse_qs, urlparse

URL = os.environ["DATABASE_URL"]
out: list[dict] = []


def rec(driver: str, version: str, ok: bool, needed: str, err: str = "") -> None:
    out.append({"driver": driver, "version": version, "accepted_as_given": ok, "needed": needed, "error": err})


def run_psycopg() -> None:
    import psycopg

    try:
        with psycopg.connect(URL) as c:
            assert c.execute("select 1").fetchone() == (1,)
        rec("psycopg", md.version("psycopg"), True, "nothing; sslrootcert query param honoured (libpq form)")
    except Exception as e:
        rec("psycopg", md.version("psycopg"), False, "", str(e))


def run_asyncpg() -> None:
    import asyncpg

    async def go(url: str):
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval("select 1")
        finally:
            await conn.close()

    try:
        assert asyncio.run(go(URL)) == 1
        rec("asyncpg", md.version("asyncpg"), True, "nothing; sslrootcert query param honoured")
    except Exception as e:
        err = str(e)
        try:
            import ssl

            ctx = ssl.create_default_context(cafile=parse_qs(urlparse(URL).query)["sslrootcert"][0])

            async def go2():
                conn = await asyncpg.connect(URL.split("?")[0], ssl=ctx)
                try:
                    return await conn.fetchval("select 1")
                finally:
                    await conn.close()

            assert asyncio.run(go2()) == 1
            rec("asyncpg", md.version("asyncpg"), False, "ssl=SSLContext(cafile) in code; query params not enough", err)
        except Exception as e2:
            rec("asyncpg", md.version("asyncpg"), False, "", f"{err} || {e2}")


def run_django() -> None:
    import django
    from django.conf import settings

    u = urlparse(URL)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    native_note = "Django has no DATABASE_URL parser; URL split by hand into ENGINE/NAME/USER/PASSWORD/HOST/PORT/OPTIONS"
    settings.configure(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": u.path.lstrip("/"),
                "USER": u.username,
                "PASSWORD": u.password,
                "HOST": u.hostname,
                "PORT": u.port,
                "OPTIONS": q,
            }
        }
    )
    django.setup()
    try:
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("select 1")
            assert cur.fetchone() == (1,)
        rec("django", django.get_version(), False, native_note + "; sslmode/sslrootcert passed through OPTIONS and honoured")
    except Exception as e:
        rec("django", django.get_version(), False, native_note, str(e))


for f in (run_psycopg, run_asyncpg, run_django):
    f()
pathlib.Path(sys.argv[1]).write_text(json.dumps(out, indent=2))
print(json.dumps(out, indent=2))

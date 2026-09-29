"""A local SSC control plane for development and tests.

    eval "$(uv run python tools/dev_stack.py up --dsn postgresql://postgres:pw@localhost/postgres)"
    uv run python tools/dev_stack.py token | uv run ssc --api http://127.0.0.1:8000 token set
    uv run python tools/dev_stack.py serve --port 8000

``up`` creates the roles, migrates, creates one org and a signing key. ``token`` mints an API
token with the same claims as the API tests. ``serve`` runs the API; ``--port 0`` picks a free port
and prints it. State lives in ``.ssc-dev/`` at the repository root, or ``--dir``.

``serve`` runs with ``SSC_ENV=dev``, a metrics key, and the filesystem blob store under
``<dir>/blobs`` with its own URL signing key, so metrics are recorded and bundle uploads work.
The keys are random, kept in the state file, and reused on every start; anything already set in
the environment wins.

Tokens name the issuer ``https://dev.invalid``, which a real API never trusts. The API itself is
unchanged: it only verifies tokens against the JWKS it is given.
"""

import argparse
import asyncio
import base64
import json
import os
import secrets
import shlex
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import uvicorn
from psycopg import sql

from ssc_control.api import Settings, create_app
from ssc_control.db import (
    APP_ROLE,
    MIGRATE_ROLE,
    NewOrg,
    create_org,
    ensure_roles,
    make_engine,
    upgrade,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / ".ssc-dev"
TESTKIT = ROOT / "packages" / "ssc_control" / "tests"
ISSUER = "https://dev.invalid"
KID = "dev-1"
BLOB_KID = "dev-blob-1"
ADMIN_SUBJECT = "dev-admin"


def _testkit() -> ModuleType:
    """The control-plane test helpers: one ``mint`` for the tests and the dev stack."""
    if str(TESTKIT) not in sys.path:
        sys.path.insert(0, str(TESTKIT))
    import ssc_testkit  # noqa: PLC0415

    return ssc_testkit


# ── state files ──────────────────────────────────────────────────────────────


def _state_path(d: Path) -> Path:
    return d / "state.json"


def load_state(d: Path) -> dict[str, Any]:
    try:
        return json.loads(_state_path(d).read_text())
    except FileNotFoundError:
        return {}


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _random_key() -> str:
    """Standard base64 of 32 random bytes: the shape of ``SSC_METRICS_KEY`` and of a blob key."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _dev_keys(d: Path) -> dict[str, str]:
    """The metrics key and the blob URL signing key, created and saved on first use."""
    state = load_state(d)
    missing = [k for k in ("metrics_key", "blob_signing_key") if k not in state]
    if missing:
        state.update({k: _random_key() for k in missing})
        _write_private(_state_path(d), json.dumps(state, indent=2, sort_keys=True).encode())
    return {k: state[k] for k in ("metrics_key", "blob_signing_key")}


def _signing_key(d: Path) -> Any:
    """The dev signing key as an ``ssc_testkit.SigningKey``, created on first use."""
    kit = _testkit()
    pem_path, jwks_path = d / "key.pem", d / "jwks.json"
    if pem_path.exists() and jwks_path.exists():
        return kit.SigningKey(pem_path.read_bytes(), json.loads(jwks_path.read_text())["keys"][0])
    fresh = kit.new_signing_key()
    key = kit.SigningKey(fresh.private_pem, {**fresh.jwk, "kid": KID})
    _write_private(pem_path, key.private_pem)
    jwks_path.write_text(json.dumps({"keys": [key.jwk]}, sort_keys=True))
    return key


# ── up ───────────────────────────────────────────────────────────────────────


def up(superuser_dsn: str, d: Path, org_name: str = "Dev org") -> dict[str, Any]:
    """Prepare the database, org and key. Safe to run again: it reuses what exists."""
    kit = _testkit()
    state = load_state(d)
    migrate_pw = state.get("migrate_password") or secrets.token_urlsafe(24)
    app_pw = state.get("app_password") or secrets.token_urlsafe(24)
    with psycopg.connect(superuser_dsn, autocommit=True) as conn:
        ensure_roles(conn)
        for role, pw in ((MIGRATE_ROLE, migrate_pw), (APP_ROLE, app_pw)):
            conn.execute(
                sql.SQL("alter role {} login password {}").format(
                    sql.Identifier(role), sql.Literal(pw)
                )
            )
        conn.execute(
            sql.SQL("grant create on database {} to {}").format(
                sql.Identifier(conn.info.dbname), sql.Identifier(MIGRATE_ROLE)
            )
        )
        upgrade(kit.with_role(superuser_dsn, MIGRATE_ROLE, migrate_pw))
        org_id = state.get("org_id")
        known = org_id and conn.execute("select 1 from ssc.org where id = %s", (org_id,)).fetchone()
    app_dsn = kit.with_role(superuser_dsn, APP_ROLE, app_pw)
    if not known:

        async def go() -> Any:
            engine = make_engine(app_dsn)
            try:
                spec = NewOrg(org_name, "Dev Admin", "dev@example.invalid", ISSUER, ADMIN_SUBJECT)
                return await create_org(engine, spec)
            finally:
                await engine.dispose()

        created = asyncio.run(go())
        state.update(org_id=created.org_id, admin_user_id=created.admin_user_id)
    key = _signing_key(d)
    state.update(migrate_password=migrate_pw, app_password=app_pw, database_dsn=app_dsn)
    _write_private(_state_path(d), json.dumps(state, indent=2, sort_keys=True).encode())
    _dev_keys(d)
    return {
        "SSC_DATABASE_DSN": app_dsn,
        "SSC_API_JWKS": json.dumps({"keys": [key.jwk]}, sort_keys=True),
        "SSC_API_ISSUER": ISSUER,
        "SSC_TOKEN": mint(d),
    }


# ── token ────────────────────────────────────────────────────────────────────


def mint(  # noqa: PLR0913  (each is a token claim)
    d: Path,
    *,
    sub: str | None = None,
    kind: str = "user",
    ttl: int = 12 * 3600,
    agent: bool = False,
    client_id: str | None = None,
    scope: str | None = None,
) -> str:
    """An API token signed with the dev key. ``sub`` defaults to the org's admin."""
    state = load_state(d)
    if "org_id" not in state:
        raise SystemExit(f"no dev stack in {d}; run `up` first")
    extra: dict[str, Any] = {}
    if agent:
        extra["agent"] = True
    if client_id:
        extra["client_id"] = client_id
    if scope:
        extra["scope"] = scope
    return _testkit().mint(
        _signing_key(d),
        org=state["org_id"],
        sub=sub or state["admin_user_id"],
        kind=kind,
        issuer=ISSUER,
        kid=KID,
        expires_in=ttl,
        **extra,
    )


# ── serve ────────────────────────────────────────────────────────────────────


def api_env(d: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The API's environment: what is set wins, the dev state fills the rest."""
    e = dict(os.environ if env is None else env)
    state = load_state(d)
    if "SSC_DATABASE_DSN" not in e and "database_dsn" in state:
        e["SSC_DATABASE_DSN"] = state["database_dsn"]
    if "SSC_API_JWKS" not in e and (d / "jwks.json").exists():
        e["SSC_API_JWKS"] = (d / "jwks.json").read_text()
    e.setdefault("SSC_API_ISSUER", ISSUER)
    keys = _dev_keys(d)
    e.setdefault("SSC_ENV", "dev")
    e.setdefault("SSC_METRICS_KEY", keys["metrics_key"])
    e.setdefault("SSC_BLOB_BACKEND", "fs")
    e.setdefault("SSC_BLOB_ROOT", str(d / "blobs"))
    e.setdefault("SSC_BLOB_SIGNING_KEYS", json.dumps({BLOB_KID: keys["blob_signing_key"]}))
    e.setdefault("SSC_BLOB_SIGNING_KID", BLOB_KID)
    return e


def serve(
    d: Path, host: str, port: int, rate_capacity: int | None, rate_refill: float | None
) -> None:
    env = api_env(d)
    if rate_capacity is not None:
        env["SSC_API_RATE_CAPACITY"] = str(rate_capacity)
    if rate_refill is not None:
        env["SSC_API_RATE_REFILL_PER_SECOND"] = str(rate_refill)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    bound = sock.getsockname()[1]
    shown = f"[{host}]" if family == socket.AF_INET6 else host
    url = f"http://{shown}:{bound}"
    env.setdefault("SSC_API_PUBLIC_URL", url)
    settings = Settings.from_env(env)
    print(f"SSC_API_URL={url}", flush=True)  # noqa: T201
    config = uvicorn.Config(create_app(settings), log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])


# ── command line ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="state folder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="roles, migrations, one org, a signing key")
    p_up.add_argument("--dsn", required=True, help="superuser DSN of the Postgres to use")
    p_up.add_argument("--org-name", default="Dev org")

    p_token = sub.add_parser("token", help="print an API token")
    p_token.add_argument("--sub", help="subject (default: the org admin)")
    p_token.add_argument("--kind", default="user")
    p_token.add_argument("--ttl", type=int, default=12 * 3600, help="seconds")
    p_token.add_argument("--agent", action="store_true")
    p_token.add_argument("--client-id")
    p_token.add_argument("--scope", help="preview: the credential never touches prod")

    p_serve = sub.add_parser("serve", help="run the API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000, help="0 picks a free port")
    p_serve.add_argument("--rate-capacity", type=int)
    p_serve.add_argument("--rate-refill", type=float)

    args = parser.parse_args(argv)
    d: Path = args.dir.resolve()
    if args.command == "up":
        for key, value in up(args.dsn, d, args.org_name).items():
            print(f"export {key}={shlex.quote(value)}")  # noqa: T201
    elif args.command == "token":
        print(  # noqa: T201
            mint(
                d,
                sub=args.sub,
                kind=args.kind,
                ttl=args.ttl,
                agent=args.agent,
                client_id=args.client_id,
                scope=args.scope,
            )
        )
    else:
        serve(d, args.host, args.port, args.rate_capacity, args.rate_refill)


if __name__ == "__main__":
    main(sys.argv[1:])

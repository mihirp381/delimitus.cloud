"""One org with one app and one connection, for the data gateway tests (SSC-050). Imported as
``datagw_world``. Google's keys are a local RSA key served over a mock transport; the cell's
identity key is a local EC key."""

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from ssc_contracts.identity import IDENTITY_ALG, IDENTITY_TYP
from ssc_contracts.snapshot import FORMAT_V1
from ssc_datagw.connectors import Column, Query, Table
from ssc_datagw.settings import Settings
from ssc_shared.blobstore_fs import FsBlobStore, UrlSigner
from ssc_shared.canonical import canonical_bytes
from ssc_shared.clock import SystemClock
from ssc_shared.snapshot_feed import latest_key, object_key

ORG = "org_" + "a" * 20
LEDGER, PAYROLL = "app_" + "l" * 20, "app_" + "p" * 20
PROD, PREVIEW, PAY = "env_" + "p" * 20, "env_" + "v" * 20, "env_" + "y" * 20
ADA, BEN = "usr_" + "a" * 20, "usr_" + "b" * 20
SALES, HR = "con_" + "s" * 20, "con_" + "h" * 20
LABEL = "bcdfghjklmnp"
DOMAIN = "apps.test"
PROJECT = f"ssc-c-{LABEL}"
AUDIENCE = "https://ssc-datagw-123456789012.us-central1.run.app"
ISSUER = f"https://keys.delimitus.com/{LABEL}"
CERTS = "https://certs.test/oauth2/v3/certs"
ORIGIN = f"https://ledger.{LABEL}.{DOMAIN}"
GOOGLE_KID, NOTE_KID = "google-1", "cell-1"
GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FORGED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NOTE_KEY = ec.generate_private_key(ec.SECP256R1())
SIGNING_KEY = ("datagw-" + "test-" + "signing-" + "key-").encode() * 2


def _jwk(public: Any, kid: str, alg: str) -> dict[str, Any]:
    algorithm = RSAAlgorithm if alg == "RS256" else ECAlgorithm
    jwk = json.loads(algorithm.to_jwk(public))
    jwk.update({"kid": kid, "alg": alg, "use": "sig"})
    return jwk


GOOGLE_JWKS = {"keys": [_jwk(GOOGLE_KEY.public_key(), GOOGLE_KID, "RS256")]}
NOTE_JWKS = {"keys": [_jwk(NOTE_KEY.public_key(), NOTE_KID, IDENTITY_ALG)]}

SETTINGS = Settings(
    org_id=ORG,
    cell_label=LABEL,
    project_id=PROJECT,
    bucket=f"{PROJECT}-cell",
    audience=AUDIENCE,
    jwks=NOTE_JWKS,
    issuer=ISSUER,
    apps_domain=DOMAIN,
)

ENV = {
    "SSC_ORG_ID": ORG,
    "SSC_CELL_LABEL": LABEL,
    "SSC_PROJECT_ID": PROJECT,
    "SSC_CELL_BUCKET": f"{PROJECT}-cell",
    "SSC_DATAGW_AUDIENCE": AUDIENCE,
    "SSC_IDENTITY_JWKS": json.dumps(NOTE_JWKS),
    "SSC_APPS_DOMAIN": DOMAIN,
}


AGENT = f"ssc-cell-agent@{PROJECT}.iam.gserviceaccount.com"
"""The cell agent's service account, which may describe a connection for an environment."""


def account(env_id: str, project: str = PROJECT) -> str:
    return f"ssc-a-{env_id.removeprefix('env_')}@{project}.iam.gserviceaccount.com"


def google_token(
    env_id: str = PROD, *, key: Any = GOOGLE_KEY, kid: str = GOOGLE_KID, **changes: Any
) -> str:
    """An ID token as Cloud Run's metadata server mints it for ``env_id``'s service. A change
    to None drops that claim."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "azp": "1234567890",
        "sub": "1234567890",
        "email": account(env_id),
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(changes)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def bearer(env_id: str = PROD, **changes: Any) -> dict[str, str]:
    return {"authorization": f"Bearer {google_token(env_id, **changes)}"}


def note(
    *, env: str = "prod", app: str = LEDGER, sub: str = ADA, aud: str = ORIGIN, **changes: Any
) -> str:
    """The identity note the gateway attached to the request the app is answering."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
        "org": ORG,
        "app": app,
        "env": env,
        "role": "schedule" if sub.startswith("sch_") else "user",
        "groups": [],
    }
    claims.update(changes)
    return jwt.encode(
        claims, NOTE_KEY, algorithm=IDENTITY_ALG, headers={"kid": NOTE_KID, "typ": IDENTITY_TYP}
    )


def certs_transport(
    fetches: list[str] | None = None, *, fail: bool = False
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if fetches is not None:
            fetches.append(str(request.url))
        if fail:
            return httpx2.Response(503)
        return httpx2.Response(200, json=GOOGLE_JWKS)

    return httpx2.MockTransport(handler)


def snapshot(
    version: int = 1,
    *,
    status: str = "active",
    sales: str = "active",
    sales_limits: dict[str, int] | None = None,
    grant_limits: dict[str, int] | None = None,
    connections: bool = True,
) -> dict[str, Any]:
    env = {"floor": "user"}
    doc: dict[str, Any] = {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": version,
        "compiled_at": "2026-10-03T12:00:00Z",
        "environments": {
            PROD: {"app_id": LEDGER, "name": "prod", "status": status, **env},
            PREVIEW: {"app_id": LEDGER, "name": "preview", "status": status, "floor": "builder"},
            PAY: {"app_id": PAYROLL, "name": "prod", "status": "active", **env},
        },
        "hosts": {"ledger": PROD, "ledger--preview": PREVIEW, "payroll": PAY},
        "grants": {},
        "groups_by_user": {},
        "users": {ADA: {"status": "active"}, BEN: {"status": "deactivated"}},
        "ceiling": None,
    }
    if connections:
        grant: dict[str, Any] = {} if grant_limits is None else {"limits": grant_limits}
        sales_doc: dict[str, Any] = {
            "connection_id": SALES,
            "status": sales,
            "grants": {PROD: grant, PREVIEW: {}},
        }
        if sales_limits is not None:
            sales_doc["limits"] = sales_limits
        doc["connections"] = {
            "sales": sales_doc,
            "hr": {"connection_id": HR, "status": "active", "grants": {PAY: {}}},
        }
    return doc


def store(root: Path) -> FsBlobStore:
    signer = UrlSigner({"k1": SIGNING_KEY}, active="k1", clock=SystemClock())
    return FsBlobStore(root, signer=signer, base_url="http://blobs.test")


async def publish(blobs: FsBlobStore, version: int = 1, **changes: Any) -> None:
    raw = canonical_bytes(snapshot(version, **changes))
    sha = hashlib.sha256(raw).hexdigest()
    key = object_key(ORG, version, sha)
    await blobs.put(key, raw)
    pointer = {"version": version, "key": key, "digest": f"sha256:{sha}"}
    await blobs.put(latest_key(ORG), canonical_bytes(pointer))


COLUMNS = (Column("id", "integer", "int4"), Column("amount", "decimal", "numeric"))
TABLES = (Table("public.sales", COLUMNS), Table("public.empty", ()))


@dataclass
class FakeConnector:
    """Yields ``rows`` and describes ``tables``; with ``hold`` set, waits on it before the first
    row or the description, as a slow query. ``described`` is each description's ``timeout_ms``."""

    rows: Sequence[Sequence[object]] = ()
    error: Exception | None = None
    hold: asyncio.Event | None = None
    queries: list[Query] = field(default_factory=list[Query])
    running: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: int = 0
    closed: int = 0
    tables: Sequence[Table] = TABLES
    described: list[int] = field(default_factory=list[int])

    async def _rows(self) -> AsyncIterator[Sequence[object]]:
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        for row in self.rows:
            yield row

    async def describe(self, *, schemas: Sequence[str] | None, timeout_ms: int) -> list[Table]:
        assert schemas is None, "the gateway describes every schema the credential reads"
        self.described.append(timeout_ms)
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if self.error is not None:
            raise self.error
        return list(self.tables)

    @asynccontextmanager
    async def open(self, query: Query) -> AsyncGenerator[FakeCursor]:
        self.queries.append(query)
        self.running += 1
        self.started.set()
        try:
            if self.error is not None:
                raise self.error
            yield FakeCursor(self)
        finally:
            self.running -= 1
            self.closed += 1


@dataclass
class FakeCursor:
    connector: FakeConnector
    columns: Sequence[Column] = COLUMNS

    def rows(self) -> AsyncIterator[Sequence[object]]:
        return self.connector._rows()  # noqa: SLF001

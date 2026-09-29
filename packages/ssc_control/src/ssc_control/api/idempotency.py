"""``Idempotency-Key`` on every POST, claimed in the request's own transaction.

Three states, as in Delimitus ``state/idempotency.ts``: **absent** (nobody has this key),
**claimed** (a request holds it now) and **settled** (it ran, here is the answer). Because the
claim shares the transaction with the change, a refusal rolls the claim back and leaves no trace,
and a process that dies mid-request leaves nothing either. A second request with the same key
blocks on the primary key until the first commits, then reads the settled answer and replays it.

A row in state ``claimed`` visible to another transaction can therefore only mean a bug; it is
refused as ``IDEMPOTENCY_IN_FLIGHT`` rather than re-run, because re-running is the one option
that can double an effect.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.errors import ErrorCode
from ssc_control.api.problems import Refusal
from ssc_control.api.uow import InternalUoW, Reply, UnitOfWork, UserUoW

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotency-Replayed"
MAX_KEY_LENGTH: Final = 200


def request_hash(method: str, path: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\n")
    h.update(path.encode())
    h.update(b"\n")
    h.update(body)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class Claimed:
    pass


@dataclass(frozen=True, slots=True)
class Settled:
    request_hash: str
    reply: Reply


@dataclass(frozen=True, slots=True)
class InFlight:
    pass


class Replay(Exception):  # noqa: N818  (control flow, not an error)
    """Raised by the dependency to short-circuit the endpoint with the stored answer."""

    def __init__(self, reply: Reply) -> None:
        super().__init__("replay")
        self.reply = reply


_CLAIM = text(
    "insert into ssc.idempotency_claim (org_id, credential_id, key, request_hash) "
    "values (:org, :cred, :key, :hash) on conflict do nothing returning state"
)
_READ = text(
    "select state, request_hash, status_code, response_body, response_headers "
    "from ssc.idempotency_claim where org_id = :org and credential_id = :cred and key = :key"
)
_SETTLE = text(
    "update ssc.idempotency_claim set state = 'settled', status_code = :status, "
    "response_body = cast(:body as jsonb), response_headers = cast(:headers as jsonb), "
    "settled_at = now() where org_id = :org and credential_id = :cred and key = :key"
)


async def claim(
    conn: AsyncConnection, *, org_id: str, credential_id: str, key: str, hash_: str
) -> Claimed | Settled | InFlight:
    params: dict[str, Any] = {"org": org_id, "cred": credential_id, "key": key, "hash": hash_}
    inserted = (await conn.execute(_CLAIM, params)).first()
    if inserted is not None:
        return Claimed()
    row = (await conn.execute(_READ, params)).first()
    if row is None:  # the holder rolled back between our insert and our read: claim again
        return await claim(conn, org_id=org_id, credential_id=credential_id, key=key, hash_=hash_)
    state, stored_hash, status, body, headers = row
    if state != "settled":
        return InFlight()
    stored_headers: dict[str, str] = {str(k): str(v) for k, v in dict(headers).items()}
    return Settled(str(stored_hash), Reply(status=int(status), body=body, headers=stored_headers))


async def settle(
    conn: AsyncConnection, *, org_id: str, credential_id: str, key: str, reply: Reply
) -> None:
    await conn.execute(
        _SETTLE,
        {
            "org": org_id,
            "cred": credential_id,
            "key": key,
            "status": reply.status,
            "body": json.dumps(reply.body),
            "headers": json.dumps(reply.headers),
        },
    )


async def _idempotent(
    request: Request,
    uow: UnitOfWork,
    key: str | None,
) -> AsyncIterator[None]:
    if not key:
        raise Refusal(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
    if len(key) > MAX_KEY_LENGTH:
        raise Refusal(ErrorCode.VALIDATION_FAILED, evidence={"idempotency_key_length": len(key)})
    body = await request.body()
    hash_ = request_hash(request.method, request.url.path, body)
    outcome = await claim(
        uow.conn,
        org_id=uow.org_id,
        credential_id=uow.principal.credential_id,
        key=key,
        hash_=hash_,
    )
    match outcome:
        case Settled(request_hash=stored, reply=reply):
            if stored != hash_:
                raise Refusal(ErrorCode.IDEMPOTENCY_KEY_REUSED)
            raise Replay(reply)
        case InFlight():
            raise Refusal(ErrorCode.IDEMPOTENCY_IN_FLIGHT)
        case Claimed():
            pass
    yield
    if uow.reply_sent is None:
        raise RuntimeError("a POST endpoint must answer through UnitOfWork.reply()")
    await settle(
        uow.conn,
        org_id=uow.org_id,
        credential_id=uow.principal.credential_id,
        key=key,
        reply=uow.reply_sent,
    )


KeyHeader = Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)]


async def user_idempotent(
    request: Request, uow: UserUoW, key: KeyHeader = None
) -> AsyncIterator[None]:
    async for _ in _idempotent(request, uow, key):
        yield


async def internal_idempotent(
    request: Request, uow: InternalUoW, key: KeyHeader = None
) -> AsyncIterator[None]:
    async for _ in _idempotent(request, uow, key):
        yield


UserIdempotent = Depends(user_idempotent, scope="function")
InternalIdempotent = Depends(internal_idempotent, scope="function")

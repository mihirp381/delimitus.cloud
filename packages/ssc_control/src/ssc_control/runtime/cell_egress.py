"""Egress proxy credentials through the cell agent (SSC-053).

An app environment that declares outbound hosts gets its own proxy credential. The cell agent
makes it: a random token, written into the environment's ``HTTPS_PROXY`` secret as a new version
of the proxy URL that carries it. The control plane gets back the credential's id, the digest of
its token, which the snapshot carries to the proxy, and the secret version; never the token.

``record_credential`` keeps what came back: one ``ssc.egress_credential`` row and the
``HTTPS_PROXY`` ``ssc.secret_ref``, audited ``secret.bound`` or ``secret.rotated`` like a
secret set by a person (SSC-026), so the deployment that follows pins the version. Credentials
beyond the newest ``MAX_CREDENTIALS`` are deleted, which the next snapshot drops from the proxy.

``info`` asks the cell where its proxy is and the fixed address its traffic leaves from, for the
console.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssc_contracts.app_env import HTTPS_PROXY
from ssc_contracts.audit import AuditAction
from ssc_contracts.egress import CREDENTIAL_ID, DIGEST, MAX_CREDENTIALS
from ssc_contracts.ids import new_id
from ssc_control.audit import Actor, NewEvent, append_event
from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.redaction import redact
from ssc_shared.runtime import ORG_HEADER, SECRET_VERSION, check_org

CALL_TIMEOUT_SECONDS: Final = 60.0


class CellEgressError(Exception):
    """The cell could not make a credential or say where its proxy is."""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuedCredential:
    """A new proxy credential: its id, its token's digest and the secret version holding it."""

    credential_id: str
    sha1: str
    version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EgressInfo:
    """The proxy's internal address and the cell's fixed outbound address, when it has them."""

    proxy_address: str | None
    outbound_ip: str | None


class CellEgress(Protocol):
    async def issue(self, environment_id: str) -> IssuedCredential:
        """Make a new credential for the environment and write it to its ``HTTPS_PROXY``."""
        ...

    async def info(self) -> EgressInfo:
        """Where the cell's proxy is and the address its traffic leaves from."""
        ...


class AgentCellEgress(CellEgress):
    """``CellEgress`` through one cell's agent, with an ID token for its URL on every call."""

    def __init__(
        self,
        agent_url: str,
        id_tokens: IdTokens,
        *,
        org_id: str,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._org = check_org(org_id)
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def issue(self, environment_id: str) -> IssuedCredential:
        return issued_of(await self._call("issue", {"environment_id": environment_id}))

    async def info(self) -> EgressInfo:
        body = await self._call("info", {})
        proxy, outbound = body.get("proxy_address"), body.get("outbound_ip")
        return EgressInfo(
            proxy_address=proxy if isinstance(proxy, str) and proxy else None,
            outbound_ip=outbound if isinstance(outbound, str) and outbound else None,
        )

    async def _call(self, method: str, payload: Mapping[str, object]) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/egress/{method}",
                json=dict(payload),
                headers={"Authorization": f"Bearer {token}", ORG_HEADER: self._org},
            )
        except httpx2.HTTPError as exc:
            raise CellEgressError(f"cell agent egress {method}: {type(exc).__name__}") from None
        try:
            parsed: object = response.json()
        except ValueError:
            parsed = None
        body = cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}
        if not response.is_success:
            message = f"{body.get('code', '')} {body.get('message', response.reason_phrase)}"
            raise CellEgressError(
                f"cell agent egress {method}: HTTP {response.status_code} {message}"
            )
        return body

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(slots=True)
class FakeCellEgress(CellEgress):
    """In memory, for tests and local development: credential ids and versions that count up and
    digests of nothing; it holds no token at all."""

    proxy_address: str | None = "10.20.4.10"
    outbound_ip: str | None = "192.0.2.10"
    calls: list[str] = field(default_factory=list[str])
    _versions: dict[str, int] = field(default_factory=dict[str, int])

    async def issue(self, environment_id: str) -> IssuedCredential:
        self.calls.append(environment_id)
        version = self._versions.get(environment_id, 0) + 1
        self._versions[environment_id] = version
        return IssuedCredential(
            credential_id=f"fake{len(self.calls):08d}", sha1="A" * 27 + "=", version=str(version)
        )

    async def info(self) -> EgressInfo:
        return EgressInfo(proxy_address=self.proxy_address, outbound_ip=self.outbound_ip)


def issued_of(body: Mapping[str, Any]) -> IssuedCredential:
    """The agent's answer to ``issue``, checked; ``CellEgressError`` for anything else."""
    credential_id, sha1, version = body.get("credential_id"), body.get("sha1"), body.get("version")
    if not isinstance(credential_id, str) or CREDENTIAL_ID.fullmatch(credential_id) is None:
        raise CellEgressError("cell agent egress issue: credential_id is not a credential id")
    if not isinstance(sha1, str) or DIGEST.fullmatch(sha1) is None:
        raise CellEgressError("cell agent egress issue: sha1 is not a digest")
    if not isinstance(version, str) or SECRET_VERSION.fullmatch(version) is None:
        raise CellEgressError("cell agent egress issue: version is not a secret version")
    return IssuedCredential(credential_id=credential_id, sha1=sha1, version=version)


_HAS = text(
    "select 1 from ssc.egress_credential where org_id = :org and environment_id = :env limit 1"
)
_INSERT = text(
    "insert into ssc.egress_credential (org_id, environment_id, credential_id, sha1, "
    "secret_version) values (:org, :env, :cid, :sha1, :version)"
)
_PRUNE = text(
    "delete from ssc.egress_credential where org_id = :org and environment_id = :env and "
    "credential_id not in (select credential_id from ssc.egress_credential where org_id = :org "
    "and environment_id = :env order by created_at desc, credential_id desc limit :keep)"
)
_SELECT_SECRET = text(
    "select id, secret_version from ssc.secret_ref "
    "where org_id = :org and environment_id = :env and name = :name"
)
_INSERT_SECRET = text(
    "insert into ssc.secret_ref (id, org_id, environment_id, name, secret_version) "
    "values (:id, :org, :env, :name, :version)"
)
_UPDATE_SECRET = text(
    "update ssc.secret_ref set secret_version = :version, updated_at = now() "
    "where org_id = :org and id = :id"
)


async def has_credential(conn: AsyncConnection, *, org_id: str, environment_id: str) -> bool:
    return (await conn.execute(_HAS, {"org": org_id, "env": environment_id})).first() is not None


async def record_credential(
    conn: AsyncConnection,
    *,
    org_id: str,
    environment_id: str,
    issued: IssuedCredential,
    actor: Actor,
) -> None:
    """Keep a new credential and point ``HTTPS_PROXY`` at its version, then prune and audit,
    after every row lock (decision 020). The caller asks for the snapshot that carries it."""
    params = {"org": org_id, "env": environment_id}
    await conn.execute(
        _INSERT,
        {**params, "cid": issued.credential_id, "sha1": issued.sha1, "version": issued.version},
    )
    await conn.execute(_PRUNE, {**params, "keep": MAX_CREDENTIALS})
    refs = {**params, "name": HTTPS_PROXY, "version": issued.version}
    current = (await conn.execute(_SELECT_SECRET, refs)).first()
    after = {"environment_id": environment_id, "name": HTTPS_PROXY, "version": issued.version}
    if current is None:
        ref_id = new_id("sec")
        await conn.execute(_INSERT_SECRET, {**refs, "id": ref_id})
        action, before = AuditAction.SECRET_BOUND, None
    else:
        ref_id = str(current.id)
        await conn.execute(_UPDATE_SECRET, {**refs, "id": ref_id})
        action = AuditAction.SECRET_ROTATED
        before = {**after, "version": str(current.secret_version)}
    await append_event(
        conn,
        NewEvent(
            org_id=org_id,
            action=action,
            actor=actor,
            target_kind="secret_ref",
            target_id=ref_id,
            before=before,
            after=after,
        ),
    )


__all__ = [
    "AgentCellEgress",
    "CellEgress",
    "CellEgressError",
    "EgressInfo",
    "FakeCellEgress",
    "IssuedCredential",
    "has_credential",
    "issued_of",
    "record_credential",
]

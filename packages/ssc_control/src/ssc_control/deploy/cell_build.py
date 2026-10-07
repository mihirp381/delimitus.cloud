"""``BuildDriver`` through a cell's agent: Cloud Build with Railpack in the cell (SSC-015).

The control plane holds no Cloud Build role in a cell, only invoker on its agent (decision 022),
so each call is one POST to the agent's ``/v1/build/{start,poll}`` with a Google ID token, as
``runtime.cell_agent`` does for the runtime. ``start`` signs a 10-minute GET URL for the bundle
in the control plane's blob store and hands it to the agent; the build's own identity holds no
storage role, so it reads that one object and can list no bucket.
"""

from datetime import timedelta
from typing import Any, Final, cast

import httpx2

from ssc_control.deploy.build_driver import (
    MAX_REF_CHARS,
    BuildDriver,
    BuildDriverError,
    BuildNotFoundError,
    BuildRequest,
    BuildStatus,
)
from ssc_control.runtime.cell_agent import IdTokens
from ssc_shared.blobstore import BlobStore
from ssc_shared.build import CellBuild, build_to_wire, status_from_wire
from ssc_shared.runtime import ORG_HEADER, check_org

URL_LIFETIME: Final = timedelta(minutes=10)
CALL_TIMEOUT_SECONDS: Final = 60.0
DIGEST_PREFIX: Final = "sha256:"


class CellAgentBuildDriver(BuildDriver):
    def __init__(
        self,
        agent_url: str,
        id_tokens: IdTokens,
        blob_store: BlobStore,
        *,
        org_id: str,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._url = agent_url.rstrip("/")
        self._id_tokens = id_tokens
        self._org = check_org(org_id)
        self._store = blob_store
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start(self, request: BuildRequest) -> str:
        signed = await self._store.signed_url(
            request.bundle_key, method="GET", expires_in=URL_LIFETIME
        )
        build = CellBuild(
            build_id=request.build_id,
            bundle_url=signed.url,
            bundle_sha256=request.source_digest.removeprefix(DIGEST_PREFIX),
            public_env=request.public_env,
            start=request.manifest.runtime.start,
            system_packages=request.system_packages,
        )
        body = await self._call("start", {"build": build_to_wire(build)})
        ref = body.get("ref")
        if not isinstance(ref, str) or not 0 < len(ref) <= MAX_REF_CHARS:
            raise BuildDriverError("cell agent: start returned no reference")
        return ref

    async def poll(self, ref: str) -> BuildStatus:
        body = await self._call("poll", {"ref": ref})
        try:
            return status_from_wire(cast("dict[str, Any]", body.get("status")))
        except (ValueError, TypeError) as exc:
            raise BuildDriverError(f"cell agent: {exc}") from None

    async def _call(self, method: str, body: dict[str, object]) -> dict[str, Any]:
        token = await self._id_tokens(self._url)
        try:
            response = await self._client.post(
                f"{self._url}/v1/build/{method}",
                json=body,
                headers={"Authorization": f"Bearer {token}", ORG_HEADER: self._org},
            )
        except httpx2.HTTPError as exc:
            raise BuildDriverError(f"cell agent build {method}: {type(exc).__name__}") from None
        try:
            payload: object = response.json()
        except ValueError:
            payload = None
        result = cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}
        if response.is_success:
            return result
        code = str(result.get("code") or "")
        message = str(result.get("message") or response.reason_phrase)
        error = BuildNotFoundError if code == "BUILD_NOT_FOUND" else BuildDriverError
        raise error(
            f"cell agent build {method}: HTTP {response.status_code} {code} {message}".strip()
        )

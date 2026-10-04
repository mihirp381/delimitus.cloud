"""An app environment's files in the cell bucket (SSC-046), as the cell agent sees them.

The data gateway's file broker keeps an environment's files under ``files/<env_id>/`` and the
agent never reads them. ``drop`` deletes the live ones once the environment's service is gone,
for the flow that deletes an environment after the database's grace period; the bucket keeps
each deleted or replaced object as a noncurrent version, which its lifecycle rule removes later
(``infra/README.md``, "File storage"). The agent lists with its bucket-wide read grant and may
delete under ``files/`` alone (``sscCellAgentFiles``).
"""

from collections.abc import Mapping
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2

from ssc_agent.cloud_run import AccessTokens
from ssc_shared.runtime import SERVICE_NAME, SERVICE_PREFIX

STORAGE_API: Final = "https://storage.googleapis.com/storage/v1"
FILES_PREFIX: Final = "files/"
CALL_TIMEOUT_SECONDS: Final = 30.0
PAGE_SIZE: Final = 1000
_HTTP_BAD_REQUEST: Final = 400
_HTTP_NOT_FOUND: Final = 404


class FilesError(Exception):
    """Cloud Storage refused or failed a call."""


def environment_files(service: str) -> str:
    """The prefix of a service's environment's files: ``files/env_<20>/``."""
    if SERVICE_NAME.fullmatch(service) is None:
        raise ValueError(f"not an SSC app service name: {service!r}")
    return f"{FILES_PREFIX}env_{service.removeprefix(SERVICE_PREFIX)}/"


class CellFiles:
    """The environments' files in ``bucket``, through the Cloud Storage JSON API."""

    def __init__(
        self, bucket: str, tokens: AccessTokens, *, client: httpx2.AsyncClient | None = None
    ) -> None:
        self._bucket = bucket
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def drop(self, service: str) -> int:
        """Delete every live file of ``service``'s environment; how many. Running it again, or
        on an environment that kept no files, is fine."""
        prefix = environment_files(service)
        names: list[str] = []
        token = ""
        while True:
            params = {
                "prefix": prefix,
                "maxResults": str(PAGE_SIZE),
                "fields": "items/name,nextPageToken",
            }
            if token:
                params["pageToken"] = token
            page = await self._call("GET", f"b/{self._bucket}/o", params=params)
            names += [str(item["name"]) for item in page.get("items", [])]
            token = str(page.get("nextPageToken", ""))
            if not token:
                break
        for name in names:
            if not name.startswith(prefix):
                raise FilesError(f"listed an object outside {prefix}")
            await self._call("DELETE", f"b/{self._bucket}/o/{quote(name, safe='')}")
        return len(names)

    async def _call(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.request(
                method, f"{STORAGE_API}/{path}", params=dict(params or {}), headers=headers
            )
        except httpx2.HTTPError as exc:
            raise FilesError(f"{method} objects: {type(exc).__name__}") from None
        if method == "DELETE" and response.status_code == _HTTP_NOT_FOUND:
            return {}
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise FilesError(f"{method} objects: HTTP {response.status_code}")
        if not response.content:
            return {}
        payload: object = response.json()
        return cast("dict[str, Any]", payload) if isinstance(payload, dict) else {}

"""Read-only calls the nightly makes to see how a cell is configured (SSC-056): IAM policies,
deny policies and organisation policies, over REST as ``ssc-nightly``.

``ssc-nightly`` reads policy metadata only: ``roles/iam.securityReviewer``,
``roles/iam.denyReviewer`` and ``roles/orgpolicy.policyViewer`` on the ``ssc-cells`` folder. A
403 means a grant is missing: ``NoReadAccessError``, which the checks report as ``skipped, no
read access`` and which fails the night.
"""

import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any, Final, cast

import httpx2

RESOURCE_MANAGER: Final = "https://cloudresourcemanager.googleapis.com/v3"
STORAGE: Final = "https://storage.googleapis.com/storage/v1"
IAM: Final = "https://iam.googleapis.com/v2/policies"
ORG_POLICY: Final = "https://orgpolicy.googleapis.com/v2"
POLICY_VERSION: Final = 3
FORBIDDEN: Final = 403

type Json = dict[str, Any]
type AccessToken = Callable[[], Awaitable[str]]


class CloudReadError(Exception):
    pass


class NoReadAccessError(CloudReadError):
    pass


class CloudReader:
    def __init__(self, access_token: AccessToken, *, client: httpx2.AsyncClient | None = None):
        self._access_token = access_token
        self._client = client or httpx2.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def project_policy(self, project: str) -> Json:
        return await self._call(
            "POST",
            f"{RESOURCE_MANAGER}/projects/{project}:getIamPolicy",
            {"options": {"requestedPolicyVersion": POLICY_VERSION}},
        )

    async def folder_policy(self, folder: str) -> Json:
        return await self._call(
            "POST",
            f"{RESOURCE_MANAGER}/folders/{folder}:getIamPolicy",
            {"options": {"requestedPolicyVersion": POLICY_VERSION}},
        )

    async def bucket_policy(self, bucket: str) -> Json:
        query = f"optionsRequestedPolicyVersion={POLICY_VERSION}"
        return await self._call("GET", f"{STORAGE}/b/{bucket}/iam?{query}")

    async def deny_policies(self, kind: str, resource: str) -> list[Json]:
        """The deny policies attached to ``projects/<id>`` or ``folders/<id>``; ``kind`` is
        ``projects`` or ``folders``. The attachment point is encoded twice in the path."""
        point = f"cloudresourcemanager.googleapis.com/{kind}/{resource}"
        once = urllib.parse.quote(point, safe="")
        body = await self._call("GET", f"{IAM}/{urllib.parse.quote(once, safe='')}/denypolicies")
        return objects(body.get("policies"))

    async def org_policies(self, kind: str, resource: str) -> list[Json]:
        """The policies set on ``projects/<id>`` or ``folders/<id>`` itself, not inherited."""
        found: list[Json] = []
        token = ""
        while True:
            query = f"?pageToken={urllib.parse.quote(token)}" if token else ""
            body = await self._call("GET", f"{ORG_POLICY}/{kind}/{resource}/policies{query}")
            found.extend(objects(body.get("policies")))
            token = str(body.get("nextPageToken") or "")
            if not token:
                return found

    async def _call(self, method: str, url: str, body: Json | None = None) -> Json:
        token = await self._access_token()
        try:
            response = await self._client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as exc:
            raise CloudReadError(f"{method} {url}: {type(exc).__name__}") from None
        if response.status_code == FORBIDDEN:
            raise NoReadAccessError(url)
        if not response.is_success:
            raise CloudReadError(f"{method} {url}: HTTP {response.status_code}")
        decoded = response.json()
        return decoded if isinstance(decoded, dict) else {}


def objects(value: object) -> list[Json]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [cast(Json, v) for v in items if isinstance(v, dict)]


def strings(value: object) -> list[str]:
    items = cast("list[object]", value) if isinstance(value, list) else []
    return [str(v) for v in items]

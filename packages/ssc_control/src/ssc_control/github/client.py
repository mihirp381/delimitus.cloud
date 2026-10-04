"""A thin async client for the GitHub REST calls SSC-047 makes (no SDK: ``httpx2``, as for
WorkOS).

The App authenticates as itself with a ten-minute RS256 JWT (``iss`` the App id) only to mint
installation tokens. Every token is minted for one repository and the permissions of one kind
of call: ``contents: read`` for the tarball, ``checks: write`` for the check run, ``checks:
read`` and ``actions: read`` for the promote gate, ``metadata: read`` to find a repository
when it is connected. GitHub makes them last one hour; one is reused for at most
:data:`TOKEN_REUSE`, kept in this process only, and never logged. Non-2xx answers raise
:class:`GitHubError` with the status and the path, never the body.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO, Any, Final, Literal, cast

import httpx2
import jwt

DEFAULT_BASE: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"
USER_AGENT: Final = "ssc-github-app"
TIMEOUT_SECONDS: Final = 20.0
DOWNLOAD_TIMEOUT_SECONDS: Final = 120.0
TOKEN_REUSE: Final = timedelta(minutes=50)
TOKEN_MARGIN: Final = timedelta(minutes=5)
JWT_LIFETIME_SECONDS: Final = 540
JWT_BACKDATE_SECONDS: Final = 60
PAGE: Final = 100
CHECK_NAME: Final = "SSC / preview"
CHUNK_BYTES: Final = 64 * 1024

CONTENTS_READ: Final = {"contents": "read"}
CHECKS_WRITE: Final = {"checks": "write"}
GATE_READ: Final = {"checks": "read", "actions": "read"}
METADATA_READ: Final = {"metadata": "read"}

type Json = dict[str, Any]
type _TokenKey = tuple[int, int | None, tuple[tuple[str, str], ...]]
CheckStatus = Literal["in_progress", "completed"]
Conclusion = Literal["success", "failure", "neutral"]


class GitHubError(RuntimeError):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"GitHub answered HTTP {status} for {path}")
        self.status = status
        self.path = path


class SourceTooLargeError(GitHubError):
    """The tarball is larger than the caller's cap; nothing past it was kept."""


@dataclass(frozen=True, slots=True)
class Repository:
    id: int
    full_name: str
    default_branch: str


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A connected repository: ``id`` scopes its tokens, ``name`` (``owner/name``) is the path."""

    installation_id: int
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    status: CheckStatus
    title: str
    summary: str
    conclusion: Conclusion | None = None
    details_url: str | None = None

    def body(self) -> Json:
        out: Json = {
            "status": self.status,
            "output": {"title": self.title, "summary": self.summary},
        }
        if self.conclusion is not None:
            out["conclusion"] = self.conclusion
        if self.details_url is not None:
            out["details_url"] = self.details_url
        return out


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GitHubApp:
    """``transport`` replaces the network (tests); ``clock`` dates the JWT and the token cache."""

    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        base: str = DEFAULT_BASE,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._clock = clock
        self._tokens: dict[_TokenKey, tuple[str, datetime]] = {}
        self._http = httpx2.AsyncClient(
            base_url=base.rstrip("/"),
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            follow_redirects=True,
            headers={
                "accept": "application/vnd.github+json",
                "x-github-api-version": API_VERSION,
                "user-agent": USER_AGENT,
            },
        )

    def __repr__(self) -> str:
        return f"GitHubApp(app_id={self._app_id!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def app_jwt(self) -> str:
        now = int(self._clock().timestamp())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._key, algorithm="RS256")

    async def installation_token(
        self, installation_id: int, permissions: Mapping[str, str], repository_id: int | None = None
    ) -> str:
        """A token for ``installation_id`` with exactly ``permissions``, limited to
        ``repository_id`` when given; reused while it has more than :data:`TOKEN_MARGIN` left
        and for at most :data:`TOKEN_REUSE`."""
        key = (installation_id, repository_id, tuple(sorted(permissions.items())))
        now = self._clock()
        cached = self._tokens.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        path = f"/app/installations/{installation_id}/access_tokens"
        body: Json = {"permissions": dict(permissions)}
        if repository_id is not None:
            body["repository_ids"] = [repository_id]
        r = await self._http.post(
            path, json=body, headers={"authorization": f"Bearer {self.app_jwt()}"}
        )
        if r.status_code != 201:
            raise GitHubError(r.status_code, path)
        raw = cast(Json, r.json())
        token = str(raw["token"])
        expires = datetime.fromisoformat(str(raw["expires_at"]).replace("Z", "+00:00"))
        self._tokens[key] = (token, min(now + TOKEN_REUSE, expires - TOKEN_MARGIN))
        return token

    async def _call(  # noqa: PLR0913  (keyword-only)
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: Json | None = None,
        params: Mapping[str, str | int] | None = None,
        ok: int = 200,
    ) -> Json | None:
        r = await self._http.request(
            method, path, json=json, params=params, headers={"authorization": f"token {token}"}
        )
        if r.status_code == 404:
            return None
        if r.status_code != ok:
            raise GitHubError(r.status_code, path)
        return cast(Json, r.json())

    async def repository(self, installation_id: int, full_name: str) -> Repository | None:
        """The repository when the installation can see it, else None."""
        token = await self.installation_token(installation_id, METADATA_READ)
        raw = await self._call("GET", f"/repos/{full_name}", token)
        if raw is None:
            return None
        return Repository(
            id=int(raw["id"]),
            full_name=str(raw["full_name"]),
            default_branch=str(raw["default_branch"]),
        )

    async def download_tarball(
        self, repo: RepoRef, sha: str, out: IO[bytes], max_bytes: int
    ) -> int:
        """Write the commit's tarball to ``out`` (GitHub redirects to its download host, which
        never sees the token); raises :class:`SourceTooLargeError` past ``max_bytes``."""
        token = await self.installation_token(repo.installation_id, CONTENTS_READ, repo.id)
        path = f"/repos/{repo.name}/tarball/{sha}"
        written = 0
        async with self._http.stream(
            "GET",
            path,
            headers={"authorization": f"token {token}"},
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        ) as r:
            if r.status_code != 200:
                raise GitHubError(r.status_code, path)
            async for chunk in r.aiter_bytes(CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise SourceTooLargeError(413, path)
                out.write(chunk)
        return written

    async def create_check_run(self, repo: RepoRef, sha: str, report: CheckReport) -> int:
        token = await self.installation_token(repo.installation_id, CHECKS_WRITE, repo.id)
        path = f"/repos/{repo.name}/check-runs"
        body = {"name": CHECK_NAME, "head_sha": sha, **report.body()}
        raw = await self._call("POST", path, token, json=body, ok=201)
        if raw is None:
            raise GitHubError(404, path)
        return int(raw["id"])

    async def update_check_run(self, repo: RepoRef, check_run_id: int, report: CheckReport) -> None:
        token = await self.installation_token(repo.installation_id, CHECKS_WRITE, repo.id)
        path = f"/repos/{repo.name}/check-runs/{check_run_id}"
        if await self._call("PATCH", path, token, json=report.body()) is None:
            raise GitHubError(404, path)

    async def check_runs(self, repo: RepoRef, sha: str) -> list[Json]:
        """The commit's check runs, up to :data:`PAGE`, every app's."""
        token = await self.installation_token(repo.installation_id, GATE_READ, repo.id)
        path = f"/repos/{repo.name}/commits/{sha}/check-runs"
        raw = await self._call("GET", path, token, params={"per_page": PAGE, "filter": "all"})
        if raw is None:
            raise GitHubError(404, path)
        return cast(list[Json], raw.get("check_runs") or [])

    async def workflow_runs(self, repo: RepoRef, sha: str) -> list[Json]:
        """The Actions workflow runs of the commit, up to :data:`PAGE`."""
        token = await self.installation_token(repo.installation_id, GATE_READ, repo.id)
        path = f"/repos/{repo.name}/actions/runs"
        raw = await self._call("GET", path, token, params={"per_page": PAGE, "head_sha": sha})
        if raw is None:
            raise GitHubError(404, path)
        return cast(list[Json], raw.get("workflow_runs") or [])

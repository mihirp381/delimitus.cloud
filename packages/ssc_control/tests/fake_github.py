"""An in-memory GitHub for the SSC-047 tests, served through ``httpx2.MockTransport``.

Only the endpoints ``ssc_control.github.client`` calls. Installation tokens are minted only for
a JWT signed by the App's key with ``iss`` the App id and a lifetime of at most ten minutes (its
dates are not checked against the wall clock, so a test may move the client's clock), and every
repository call checks that its token belongs to the repository's installation, was minted for
that repository when it names one and carries the permission the call needs. The tarball
answers with a redirect to another host, as GitHub's does, and that host refuses a request that
still carries the token.
"""

import gzip
import io
import json
import re
import tarfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ssc_control.github.client import GitHubApp

APP_ID = "424242"
BASE = "https://api.github.test"
CODELOAD = "https://codeload.github.test"
SSC_SUITE = 9_000_000

type Json = dict[str, Any]


def _key() -> tuple[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


_PEM, _PUBLIC = _key()


@dataclass
class Token:
    installation_id: int
    permissions: dict[str, str]
    repository_ids: list[int] | None


@dataclass
class FakeGitHub:
    repos: dict[str, Json] = field(default_factory=dict)
    installations: set[int] = field(default_factory=set)
    tarballs: dict[str, bytes] = field(default_factory=dict)
    check_runs: dict[str, list[Json]] = field(default_factory=dict)
    workflow_runs: dict[str, list[Json]] = field(default_factory=dict)
    tokens: dict[str, Token] = field(default_factory=dict)
    minted: list[Token] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    fail: int | None = None
    next_id: int = 1000

    def client(self, **kwargs: Any) -> GitHubApp:
        return GitHubApp(
            app_id=APP_ID,
            private_key=_PEM,
            base=BASE,
            transport=httpx2.MockTransport(self.handle),
            **kwargs,
        )

    def repo(self, full_name: str, installation_id: int, default_branch: str = "main") -> Json:
        self.installations.add(installation_id)
        self.next_id += 1
        raw = {
            "id": self.next_id,
            "full_name": full_name,
            "default_branch": default_branch,
            "installation": installation_id,
        }
        self.repos[full_name.lower()] = raw
        return raw

    def ci(  # noqa: PLR0913
        self,
        sha: str,
        name: str,
        workflow: str,
        branch: str,
        conclusion: str | None = "success",
        *,
        status: str = "completed",
    ) -> Json:
        """A workflow run of ``workflow`` on ``branch`` with one check run ``name``."""
        self.next_id += 1
        suite = self.next_id
        self.workflow_runs.setdefault(sha, []).append(
            {
                "id": suite + 1,
                "path": workflow,
                "head_branch": branch,
                "head_sha": sha,
                "check_suite_id": suite,
            }
        )
        return self._check_run(sha, name, status, conclusion, suite)

    def _check_run(
        self, sha: str, name: str, status: str, conclusion: str | None, suite: int
    ) -> Json:
        self.next_id += 1
        run = {
            "id": self.next_id,
            "name": name,
            "head_sha": sha,
            "status": status,
            "conclusion": conclusion,
            "details_url": None,
            "output": {},
            "check_suite": {"id": suite},
        }
        self.check_runs.setdefault(sha, []).append(run)
        return run

    def ssc_runs(self, sha: str) -> list[Json]:
        return [r for r in self.check_runs.get(sha, []) if r["check_suite"]["id"] == SSC_SUITE]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.calls.append(f"{request.method} {url.host}{url.path}")
        if url.host == "codeload.github.test":
            if "authorization" in request.headers:
                return httpx2.Response(400, json={"message": "token sent to codeload"})
            sha = url.path.rsplit("/", 1)[-1]
            body = self.tarballs.get(sha)
            return httpx2.Response(404) if body is None else httpx2.Response(200, content=body)
        if self.fail is not None and not url.path.endswith("/access_tokens"):
            return httpx2.Response(self.fail, json={"message": "boom"})
        m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", url.path)
        if m and request.method == "POST":
            return self._mint(request, int(m.group(1)))
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", url.path)
        if m is None:
            return httpx2.Response(404)
        repo = self.repos.get(m.group(1).lower())
        token = self.tokens.get(request.headers.get("authorization", "").removeprefix("token "))
        if repo is None or token is None or token.installation_id != repo["installation"]:
            return httpx2.Response(404, json={"message": "Not Found"})
        if token.repository_ids is not None and repo["id"] not in token.repository_ids:
            return httpx2.Response(404, json={"message": "Not Found"})
        return self._repo_call(request, repo, token, m.group(2) or "")

    def _mint(self, request: httpx2.Request, installation_id: int) -> httpx2.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            claims = jwt.decode(
                bearer,
                _PUBLIC,
                algorithms=["RS256"],
                options={
                    "require": ["iat", "exp", "iss"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return httpx2.Response(401, json={"message": "bad JWT"})
        if claims.get("iss") != APP_ID or claims["exp"] - claims["iat"] > 600:
            return httpx2.Response(401, json={"message": "bad JWT"})
        if installation_id not in self.installations:
            return httpx2.Response(404, json={"message": "Not Found"})
        body = json.loads(request.content)
        token = Token(installation_id, body["permissions"], body.get("repository_ids"))
        value = f"ghs_{uuid.uuid4().hex}"
        self.tokens[value] = token
        self.minted.append(token)
        expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return httpx2.Response(201, json={"token": value, "expires_at": expires})

    def _repo_call(
        self, request: httpx2.Request, repo: Json, token: Token, rest: str
    ) -> httpx2.Response:
        method, perms = request.method, token.permissions
        if method == "GET" and rest == "":
            if perms.get("metadata") != "read":
                return httpx2.Response(403)
            return httpx2.Response(200, json={k: v for k, v in repo.items() if k != "installation"})
        m = re.fullmatch(r"/tarball/([0-9a-f]{40})", rest)
        if m and method == "GET":
            if perms.get("contents") != "read":
                return httpx2.Response(403)
            return httpx2.Response(
                302, headers={"location": f"{CODELOAD}/{repo['full_name']}/tar.gz/{m.group(1)}"}
            )
        if rest == "/check-runs" and method == "POST":
            if perms.get("checks") != "write":
                return httpx2.Response(403)
            body = json.loads(request.content)
            run = self._check_run(body["head_sha"], body["name"], body["status"], None, SSC_SUITE)
            run.update({k: v for k, v in body.items() if k not in {"head_sha", "name"}})
            return httpx2.Response(201, json=run)
        m = re.fullmatch(r"/check-runs/(\d+)", rest)
        if m and method == "PATCH":
            if perms.get("checks") != "write":
                return httpx2.Response(403)
            for runs in self.check_runs.values():
                for run in runs:
                    if run["id"] == int(m.group(1)):
                        run.update(json.loads(request.content))
                        return httpx2.Response(200, json=run)
            return httpx2.Response(404)
        m = re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", rest)
        if m and method == "GET":
            if perms.get("checks") != "read":
                return httpx2.Response(403)
            runs = self.check_runs.get(m.group(1), [])
            return httpx2.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if rest == "/actions/runs" and method == "GET":
            if perms.get("actions") != "read":
                return httpx2.Response(403)
            sha = parse_qs(request.url.query.decode())["head_sha"][0]
            runs = self.workflow_runs.get(sha, [])
            return httpx2.Response(200, json={"total_count": len(runs), "workflow_runs": runs})
        return httpx2.Response(404)


def tarball_of(
    files: dict[str, bytes], top: str, *, extra: list[tarfile.TarInfo] | None = None
) -> bytes:
    """A tarball shaped like GitHub's: a pax global header naming the commit, one top folder,
    its directories and files; ``extra`` entries go in as they are."""
    raw = io.BytesIO()
    commit = {"comment": top.rsplit("-", 1)[-1]}
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT, pax_headers=commit) as tar:
        root = tarfile.TarInfo(top)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tar.addfile(root)
        dirs: set[str] = set()
        for name, data in sorted(files.items()):
            parts = name.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(f"{top}/{d}")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return gzip.compress(raw.getvalue(), mtime=0)


def files_of(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }

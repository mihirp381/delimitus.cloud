"""What every proof shares: the project fence, results, state files, the cookie jar, subprocess
calls and timed HTTP requests. Standard library only."""

import hashlib
import json
import os
import re
import shlex
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

FENCE_SHA256: Final = "71876f938e39b9536d36fe1e13612008e1e65ab0977323fbd1d2254993f7b61a"
FENCE_LENGTH: Final = 16
REGION: Final = "us-central1"
KIT: Final = Path(__file__).resolve().parents[1]
REPO: Final = Path(os.environ.get("PROOFRUN_REPO") or KIT.parents[1])
COOKIE_NAME: Final = "__Host-ssc-session"
COOKIES_ENV: Final = "PROOFRUN_COOKIES"
SSC_ENV: Final = "PROOFRUN_SSC"
APP_PREFIX: Final = "ssc-a-"
GATEWAY: Final = "ssc-gateway"
_ENV_ID: Final = re.compile(r"env_([a-z0-9]{20})")
_COOKIE_VALUE: Final = re.compile(r"[A-Za-z0-9._-]{16,4096}")
_FRACTION: Final = re.compile(r"(\.\d{1,6})\d*")
USER_AGENT: Final = "ssc-proofrun/0.0.1"


class FencedError(SystemExit):
    """An argument, setting or command names the project the kit must never touch."""


class CommandError(RuntimeError):
    """A subprocess the kit ran failed; the message holds its last error line, never its output."""


class StateMismatchError(RuntimeError):
    """A state file was written by a run with other settings, so resuming it would mix runs."""


class CookieError(RuntimeError):
    """The cookie jar is missing a host, malformed, or readable by others."""


def fenced(value: str) -> bool:
    """Whether ``value`` contains the fenced project id, compared by digest only."""
    low = value.lower()
    return any(
        hashlib.sha256(low[i : i + FENCE_LENGTH].encode()).hexdigest() == FENCE_SHA256
        for i in range(len(low) - FENCE_LENGTH + 1)
    )


def fence(*values: str) -> None:
    """Refuse to go on when any value names the fenced project. The id is never printed."""
    if any(fenced(v) for v in values):
        raise FencedError("refusing: an argument or setting names the fenced project")


def fence_environ(environ: Mapping[str, str]) -> None:
    """Fence every ``SSC_*``, ``PROOFRUN_*`` and ``CLOUDSDK_*`` setting the kit may pass on."""
    fence(*(v for k, v in environ.items() if k.startswith(("SSC_", "PROOFRUN_", "CLOUDSDK_"))))


@dataclass(frozen=True, slots=True)
class Done:
    """A finished subprocess."""

    returncode: int
    stdout: str
    stderr: str


class Run(Protocol):
    """Runs one command; the real one is :func:`run_command`, tests pass a fake."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Done: ...


def run_command(
    argv: Sequence[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> Done:
    """Run ``argv`` with output captured, after fencing every argument and setting."""
    fence(*argv)
    if env is not None:
        fence_environ(env)
    result = subprocess.run(  # noqa: S603
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return Done(result.returncode, result.stdout, result.stderr)


def last_line(text: str) -> str:
    """The last non-empty line of ``text``: what a failed command said, without its output."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def gcloud_json(run: Run, *args: str) -> Any:
    """``gcloud <args> --format=json`` decoded; read-only callers only."""
    done = run(["gcloud", *args, "--format=json", "--quiet"])
    if done.returncode != 0:
        raise CommandError(f"gcloud {' '.join(args[:3])}: {last_line(done.stderr)}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def access_token(run: Run) -> str:
    """The operator's access token from ``gcloud``, kept in memory and never printed."""
    done = run(["gcloud", "auth", "print-access-token", "--quiet"])
    if done.returncode != 0 or not done.stdout.strip():
        raise CommandError("gcloud auth print-access-token failed: log in to gcloud")
    return done.stdout.strip()


def ssc_prefix() -> list[str]:
    """How to start the ``ssc`` command: ``PROOFRUN_SSC``, else ``uv run ssc`` at the repo root."""
    return shlex.split(os.environ.get(SSC_ENV) or "uv run ssc")


def ssc_json(run: Run, *args: str) -> Any:
    """``ssc <args> --json`` at the repository root, decoded."""
    done = run([*ssc_prefix(), *args, "--json"], cwd=REPO)
    if done.returncode != 0:
        raise CommandError(f"ssc {' '.join(args[:2])}: {ssc_error(done)}")
    return json.loads(done.stdout)


def ssc_error(done: Done) -> str:
    """What a failed ``ssc --json`` said: its error object on stdout, else the last line, which
    may be a warning from ``uv`` rather than the error."""
    try:
        error = json.loads(done.stdout)["error"]
        return f"{error['title']} {error['detail']}"
    except ValueError, KeyError, TypeError:
        return last_line(done.stderr or done.stdout)


def app_environment(run: Run, slug: str, env_name: str) -> dict[str, Any]:
    """One environment of an app from ``ssc status``: its ``id``, ``url`` and the rest."""
    status = ssc_json(run, "status", slug)
    for env in status.get("environments", []):
        if env.get("name") == env_name:
            if not env.get("url"):
                raise CommandError(f"{slug} {env_name} has no URL yet: deploy it first")
            return env
    raise CommandError(f"{slug} has no {env_name} environment")


def app_service(env_id: str) -> str:
    """The Cloud Run service of an environment, as ``ssc_shared.runtime.service_name`` names it."""
    m = _ENV_ID.fullmatch(env_id)
    if m is None:
        raise ValueError(f"not an environment id: {env_id!r}")
    return APP_PREFIX + m.group(1)


def database_name(env_id: str) -> str:
    """The environment's database, as ``ssc_agent.app_database`` names it."""
    return "app_" + app_service(env_id).removeprefix(APP_PREFIX)


def run_url(service: str, project_number: str, region: str = REGION) -> str:
    """A Cloud Run service's deterministic URL."""
    if not project_number.isdigit():
        raise ValueError("a project number is digits only")
    return f"https://{service}-{project_number}.{region}.run.app"


def host_of(url: str) -> str:
    """The host name of a URL, lower-cased."""
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError(f"no host in {url!r}")
    return host.lower()


def www_host(app_host: str) -> str:
    """The cell's reserved ``www`` host, which the gateway answers itself, beside an app host."""
    _, _, suffix = app_host.partition(".")
    if "." not in suffix:
        raise ValueError(f"{app_host!r} is not <slug>.<cell label>.<apps domain>")
    return f"www.{suffix}"


def parse_time(value: str) -> datetime:
    """An RFC 3339 time from a Google API, nanoseconds and ``Z`` included."""
    text = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def median(values: Iterable[float]) -> float | None:
    """The median, or None for no values."""
    items = list(values)
    return statistics.median(items) if items else None


def seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} s"


@dataclass
class Outcome:
    """What one proof found: ``passed`` is None when it could not decide."""

    proof: str
    number: str
    passed: bool | None
    lines: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return {True: "PASS", False: "FAIL", None: "INCOMPLETE"}[self.passed]

    def final_line(self) -> str:
        return f"{self.proof} {self.number} {self.verdict}"


def results_dir() -> Path:
    """Where results go: ``PROOFRUN_RESULTS``, read when a result is written, else ``results/``."""
    return Path(os.environ.get("PROOFRUN_RESULTS") or KIT / "results")


def emit(outcome: Outcome, results: Path | None = None) -> int:
    """Print the findings and the final line, append them to ``results/<proof>.json`` and return
    the exit code: 0 pass, 1 fail, 2 incomplete."""
    for line in outcome.lines:
        print(line)
    print(outcome.final_line())
    results = results or results_dir()
    results.mkdir(parents=True, exist_ok=True)
    path = results / f"{outcome.proof.lower().replace(' ', '-')}.json"
    history: list[Any] = json.loads(path.read_text()) if path.exists() else []
    history.append(
        {
            "at": now_iso(),
            "number": outcome.number,
            "verdict": outcome.verdict,
            "lines": outcome.lines,
            "data": outcome.data,
        }
    )
    path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n")
    return {True: 0, False: 1, None: 2}[outcome.passed]


class StateFile:
    """A JSON file written atomically, so a long run can stop and resume."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text())

    def save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)

    def resume(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """The saved state when it was started with ``config``, else a new one. Refuses a state
        file started with other settings."""
        saved = self.load()
        if saved is None:
            fresh: dict[str, Any] = {"config": dict(config), "started_at": now_iso()}
            self.save(fresh)
            return fresh
        if saved.get("config") != dict(config):
            raise StateMismatchError(
                f"{self.path} was started with other settings; use another --state file"
            )
        return saved


@dataclass(frozen=True, slots=True)
class Cookie:
    """A session cookie for one host. ``source`` is ``browser`` or ``sealed`` (the fallback)."""

    host: str
    value: str
    source: str
    saved_at: str


def default_cookie_path() -> Path:
    raw = os.environ.get(COOKIES_ENV)
    return Path(raw) if raw else Path.home() / ".ssc-proofrun" / "cookies.json"


class CookieJar:
    """Session cookies by host, in a file only its owner may read. Values are never printed."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cookie_path()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        if self.path.stat().st_mode & 0o077:
            raise CookieError(f"{self.path} is readable by others: chmod 600 it")
        return json.loads(self.path.read_text())

    def get(self, host: str) -> Cookie:
        entry = self._load().get(host.lower())
        if entry is None:
            raise CookieError(
                f"no session cookie for {host}: log in there in a browser and run "
                f"`python -m proofrun cookie set {host}`, or seal one (README, T2)"
            )
        return Cookie(host.lower(), entry["value"], entry["source"], entry["saved_at"])

    def put(self, host: str, value: str, source: str) -> None:
        if not _COOKIE_VALUE.fullmatch(value):
            raise CookieError("that is not a session cookie value")
        if source not in {"browser", "sealed"}:
            raise CookieError("source is browser or sealed")
        entries = self._load()
        entries[host.lower()] = {"value": value, "source": source, "saved_at": now_iso()}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2, sort_keys=True)
        os.replace(self.path.with_suffix(".tmp"), self.path)

    def listing(self) -> list[tuple[str, str, str]]:
        """Each host with its cookie's source and when it was saved, never the value."""
        return [(h, e["source"], e["saved_at"]) for h, e in sorted(self._load().items())]


def session_headers(cookie: Cookie) -> dict[str, str]:
    """What a signed-in client that is not a browser page load sends: the cookie, and no
    ``Sec-Fetch-*`` headers, so the gateway answers without its waking page."""
    return {"Cookie": f"{COOKIE_NAME}={cookie.value}", "User-Agent": USER_AGENT}


@dataclass(frozen=True, slots=True)
class Fetched:
    """One HTTP answer: ``seconds`` runs from the request to the status line and headers."""

    status: int | None
    seconds: float
    error: str | None = None
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


class Http(Protocol):
    """Sends one GET; the real one is :func:`fetch`, tests pass a fake."""

    def __call__(
        self, url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0
    ) -> Fetched: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)
BODY_LIMIT: Final = 65536


def fetch(url: str, headers: Mapping[str, str] | None = None, timeout: float = 90.0) -> Fetched:
    """GET ``url`` without following redirects, timing the first byte of the answer."""
    fence(url)
    request = urllib.request.Request(url, headers=dict(headers or {"User-Agent": USER_AGENT}))  # noqa: S310
    started = time.monotonic()
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            first = time.monotonic() - started
            body = response.read(BODY_LIMIT)
            return Fetched(response.status, first, None, body, dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        first = time.monotonic() - started
        body = exc.read(BODY_LIMIT) if exc.fp else b""
        return Fetched(exc.code, first, None, body, dict(exc.headers.items()))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Fetched(None, time.monotonic() - started, f"{type(reason).__name__}: {reason}")

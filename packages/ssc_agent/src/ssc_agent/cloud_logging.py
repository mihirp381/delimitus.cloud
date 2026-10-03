"""App logs and app health from the cell's Cloud Logging (SSC-024).

Reads go through the cell's log views, ``SSC_LOG_VIEW``, the only logs the agent's IAM can read.
The filter is built here from the service name and Cloud Build ids, never from caller text.

Cloud Logging allows 60 ``entries.list`` calls a minute per project, so the agent keeps under
it by construction, whatever the number of callers:

- follows share one read: every followed query is ORed into a single ``entries.list`` at most
  once per ``FOLLOW_INTERVAL`` seconds, with no background task (30 a minute at most);
- history reads and health share a token bucket of ``READS_PER_MINUTE`` with a burst of
  ``READ_BURST``, and repeat reads hit a short cache;

so any 60 seconds hold at most 30 + 20 + 5 = 55 calls. Each caller also gets a fair share of
``CALLER_READS_PER_MINUTE`` history reads and ``CALLER_FOLLOWS`` follows at once; past it, or
past the cell's own limits, a call raises ``LogsRateLimitedError``. The bound is per agent
instance, so the cell runs one.

Health reads the service from the Cloud Run Admin API and its recent request and system logs.
Nothing here sends a request to the app, so a health check never wakes it or bills it.
"""

import asyncio
import json
import math
import re
import secrets
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast
from urllib.parse import urlsplit

import httpx2

from ssc_shared.logs import (
    MAX_LINES,
    MAX_SINCE_SECONDS,
    MAX_WAIT_SECONDS,
    CellLogs,
    Health,
    LogLine,
    LogPage,
    LogQuery,
    LogsError,
    LogsNotConfiguredError,
    LogsRateLimitedError,
    check_caller,
    make_cursor,
    parse_cursor,
)
from ssc_shared.redaction import redact
from ssc_shared.runtime import SERVICE_NAME, RuntimeDriver

type AccessTokens = Callable[[], Awaitable[str]]
type Json = dict[str, Any]

LOGGING_API: Final = "https://logging.googleapis.com/v2"
LOG_VIEW: Final = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/buckets/[A-Za-z0-9_-]+"
    r"/views/[A-Za-z0-9_-]+"
)
CALL_TIMEOUT_SECONDS: Final = 30.0
FOLLOW_INTERVAL: Final = 2.0
FOLLOW_PAGE: Final = 1000
LAG: Final = timedelta(seconds=10)
"""Cloud Logging may take a few seconds to make an entry readable; each follow read reaches
this far back and drops what it has already seen."""
MAX_FOLLOW_BACK: Final = timedelta(hours=1)
READS_PER_MINUTE: Final = 20
READ_BURST: Final = 5
READ_CACHE_SECONDS: Final = 5.0
CALLER_READS_PER_MINUTE: Final = 6
CALLER_READ_BURST: Final = 3
CALLER_FOLLOWS: Final = 3
MAX_FOLLOWS: Final = 40
MAX_FOLLOWED: Final = 20
FOLLOW_IDLE_SECONDS: Final = 60.0
BUFFER_LINES: Final = 2000
BACKOFF_SECONDS: Final = 30
HEALTH_CACHE_SECONDS: Final = 20.0
HEALTH_PAGE: Final = 20
FAILING_REQUESTS: Final = 3
AWAKE: Final = timedelta(minutes=15)
"""Cloud Run keeps an idle instance for up to 15 minutes, so a request within that is
``running`` and none is ``asleep``."""
MAX_LINE_CHARS: Final = 8192
_MAX_CALLERS: Final = 1000
_HTTP_TOO_MANY: Final = 429
_HTTP_BAD_REQUEST: Final = 400
_HTTP_SERVER_ERROR: Final = 500
_REQUESTS_LOG: Final = "run.googleapis.com/requests"
_SYSTEM_LOG: Final = "run.googleapis.com/varlog/system"


class LogEntries(Protocol):
    """``entries.list`` over the cell's log views: one page, oldest or newest first."""

    async def list(self, filter_: str, *, newest_first: bool, page_size: int) -> list[Json]: ...


class CloudLoggingEntries(LogEntries):
    """The Logging API v2, reading only ``views``."""

    def __init__(
        self,
        views: tuple[str, ...],
        tokens: AccessTokens,
        *,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not views:
            raise ValueError("no log view")
        for view in views:
            if LOG_VIEW.fullmatch(view) is None:
                raise ValueError(f"not a log view: {view!r}")
        self._views = views
        self._tokens = tokens
        self._client = client or httpx2.AsyncClient(timeout=CALL_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list(self, filter_: str, *, newest_first: bool, page_size: int) -> list[Json]:
        body = {
            "resourceNames": list(self._views),
            "filter": filter_,
            "orderBy": "timestamp desc" if newest_first else "timestamp asc",
            "pageSize": page_size,
        }
        headers = {"Authorization": f"Bearer {await self._tokens()}"}
        try:
            response = await self._client.post(
                f"{LOGGING_API}/entries:list", json=body, headers=headers
            )
        except httpx2.HTTPError as exc:
            raise LogsError(f"entries.list: {type(exc).__name__}") from None
        if response.status_code == _HTTP_TOO_MANY:
            raise LogsRateLimitedError("Cloud Logging's read quota is spent", BACKOFF_SECONDS)
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise LogsError(f"entries.list: HTTP {response.status_code} {_reason(response)}")
        entries = _obj(response.json()).get("entries")
        return [_obj(e) for e in cast("list[object]", entries)] if isinstance(entries, list) else []


def query_filter(query: LogQuery) -> str:
    """The filter for one query; every value in it was checked against a strict pattern."""
    if query.source == "app":
        return (
            f'resource.type="cloud_run_revision" AND resource.labels.service_name="{query.service}"'
        )
    refs = " OR ".join(f'"{ref}"' for ref in query.builds)
    return f'resource.type="build" AND resource.labels.build_id=({refs})'


def health_filter(service: str, since: datetime) -> str:
    return (
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" AND '
        f'(LOG_ID("{_REQUESTS_LOG}") OR (LOG_ID("{_SYSTEM_LOG}") AND severity>=ERROR)) AND '
        f'timestamp>="{_rfc3339(since)}"'
    )


def entry_line(entry: Json, source: str) -> LogLine | None:
    """One entry as a redacted line, or None without a timestamp."""
    timestamp = _timestamp(entry)
    if timestamp is None:
        return None
    severity = entry.get("severity")
    return LogLine(
        timestamp=timestamp,
        severity=severity if isinstance(severity, str) else "DEFAULT",
        source=source,
        text=redact(_text(entry))[:MAX_LINE_CHARS],
    )


class _Bucket:
    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float]) -> None:
        self._rate = per_minute / 60
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._at = clock()

    def take(self) -> float:
        """Zero when a token was taken, else the seconds until one is due."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._at) * self._rate)
        self._at = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self._rate

    def full(self) -> bool:
        return self._tokens + (self._clock() - self._at) * self._rate >= self._burst


@dataclass(eq=False)
class _Followed:
    query: LogQuery
    lower: datetime
    last_poll: float
    waiters: int = 0
    lines: deque[tuple[int, LogLine]] = field(
        default_factory=lambda: deque[tuple[int, LogLine]](maxlen=BUFFER_LINES)
    )
    seen: dict[str, None] = field(default_factory=dict[str, None])


@dataclass(frozen=True, slots=True)
class _Outcome:
    last_request: datetime | None
    failure: str | None


class CellLogHub(CellLogs):
    """``CellLogs`` for one cell. Without ``entries`` (no log view) reads raise
    ``LogsNotConfiguredError`` and health falls back to the service alone."""

    def __init__(  # noqa: PLR0913  (keyword-only test seams)
        self,
        entries: LogEntries | None,
        driver: RuntimeDriver,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        follow_interval: float = FOLLOW_INTERVAL,
    ) -> None:
        self._entries = entries
        self._driver = driver
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self._interval = follow_interval
        self._epoch = secrets.randbelow(10**9) + 1
        self._seq = 0
        self._followed: dict[LogQuery, _Followed] = {}
        self._following: Counter[str] = Counter()
        self._lock = asyncio.Lock()
        self._generation: int = 0
        self._next_fetch: float = 0.0
        self._backoff_until = 0.0
        self._reads = _Bucket(READS_PER_MINUTE, READ_BURST, clock)
        self._callers: dict[str, _Bucket] = {}
        self._cache: dict[tuple[LogQuery, int, int], tuple[float, LogPage]] = {}
        self._outcomes: dict[str, tuple[float, _Outcome]] = {}
        self.upstream_calls = 0

    async def read(
        self, query: LogQuery, *, since_seconds: int, limit: int, caller: str
    ) -> LogPage:
        entries = self._configured()
        check_caller(caller)
        if not 1 <= since_seconds <= MAX_SINCE_SECONDS:
            raise ValueError(f"need 1 <= since_seconds <= {MAX_SINCE_SECONDS}")
        if not 1 <= limit <= MAX_LINES:
            raise ValueError(f"need 1 <= limit <= {MAX_LINES}")
        key = (query, since_seconds, limit)
        hit = self._cache.get(key)
        if hit is not None and self._clock() - hit[0] < READ_CACHE_SECONDS:
            return hit[1]
        now = self._now()
        since = now - timedelta(seconds=since_seconds)
        lines: list[LogLine] = []
        if query.source == "app" or query.builds:
            self._charge(caller)
            self._take_read()
            raw = await self._list(
                entries,
                f'{query_filter(query)} AND timestamp>="{_rfc3339(since)}"',
                newest_first=True,
                page_size=limit,
            )
            lines = sorted(
                (line for e in raw if (line := entry_line(e, query.source)) is not None),
                key=lambda line: line.timestamp,
            )
        page = LogPage(
            lines=tuple(lines), cursor=make_cursor(0, 0, lines[-1].timestamp if lines else now)
        )
        self._cache = {
            k: v for k, v in self._cache.items() if self._clock() - v[0] < READ_CACHE_SECONDS
        }
        self._cache[key] = (self._clock(), page)
        return page

    async def follow(
        self, query: LogQuery, *, cursor: str | None, wait_seconds: float, caller: str
    ) -> LogPage:
        self._configured()
        check_caller(caller)
        if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise ValueError(f"need 0 <= wait_seconds <= {MAX_WAIT_SECONDS}")
        epoch, after_seq, after = (0, 0, self._now()) if cursor is None else parse_cursor(cursor)
        if self._following[caller] >= CALLER_FOLLOWS:
            raise LogsRateLimitedError(f"at most {CALLER_FOLLOWS} follows at once", 5)
        if self._following.total() >= MAX_FOLLOWS:
            raise LogsRateLimitedError("the cell serves as many follows as it can", 5)
        followed, created = self._follow(query, after)
        by_seq = epoch == self._epoch and not created
        self._following[caller] += 1
        followed.waiters += 1
        try:
            deadline = self._clock() + wait_seconds
            while True:
                found = [
                    (seq, line)
                    for seq, line in followed.lines
                    if (seq > after_seq if by_seq else line.timestamp > after)
                ][:MAX_LINES]
                if found or self._clock() >= deadline:
                    break
                await self._refresh(deadline)
        finally:
            followed.waiters -= 1
            followed.last_poll = self._clock()
            self._following[caller] -= 1
            if not self._following[caller]:
                del self._following[caller]
        if found:
            lines = tuple(sorted((line for _, line in found), key=lambda line: line.timestamp))
            moment = max(after, lines[-1].timestamp)
            return LogPage(lines=lines, cursor=make_cursor(self._epoch, found[-1][0], moment))
        return LogPage(
            lines=(), cursor=make_cursor(self._epoch, after_seq if by_seq else self._seq, after)
        )

    async def health(self, service: str, *, caller: str) -> Health:  # noqa: PLR0911  (one per state)
        if SERVICE_NAME.fullmatch(service) is None:
            raise ValueError(f"not an SSC app service name: {service!r}")
        check_caller(caller)
        checked = self._now()
        seen = await self._driver.observe(service)
        if seen is None:
            return Health(
                state=None, reason="not_deployed", last_request_at=None, checked_at=checked
            )
        if seen.stopped:
            return Health(state=None, reason="stopped", last_request_at=None, checked_at=checked)
        if any(r.failed for r in seen.revisions if r.traffic_percent > 0):
            return Health(
                state="failing", reason="revision_failed", last_request_at=None, checked_at=checked
            )
        try:
            outcome = await self._outcome(service)
        except LogsError:
            outcome = None
        last = None if outcome is None else outcome.last_request
        if outcome is not None and outcome.failure is not None:
            return Health(
                state="failing", reason=outcome.failure, last_request_at=last, checked_at=checked
            )
        if seen.min_instances >= 1:
            return Health(
                state="running", reason="always_on", last_request_at=last, checked_at=checked
            )
        if outcome is None:
            return Health(
                state=None, reason="logs_unavailable", last_request_at=None, checked_at=checked
            )
        if last is not None and checked - last < AWAKE:
            return Health(
                state="running", reason="serving", last_request_at=last, checked_at=checked
            )
        return Health(state="asleep", reason="idle", last_request_at=last, checked_at=checked)

    def _follow(self, query: LogQuery, after: datetime) -> tuple[_Followed, bool]:
        followed = self._followed.get(query)
        if followed is not None:
            return followed, False
        if len(self._followed) >= MAX_FOLLOWED:
            self._drop_idle()
        if len(self._followed) >= MAX_FOLLOWED:
            raise LogsRateLimitedError(
                "the cell follows as many apps as it can", math.ceil(FOLLOW_IDLE_SECONDS)
            )
        lower = max(after, self._now() - MAX_FOLLOW_BACK) - LAG
        followed = _Followed(query=query, lower=lower, last_poll=self._clock())
        self._followed[query] = followed
        return followed, True

    def _drop_idle(self) -> None:
        now = self._clock()
        for query, followed in list(self._followed.items()):
            if not followed.waiters and now - followed.last_poll > FOLLOW_IDLE_SECONDS:
                del self._followed[query]

    async def _refresh(self, deadline: float) -> None:
        """One shared read for every follower, at most once per interval."""
        delay = self._next_fetch - self._clock()
        if delay > 0:
            await self._sleep(min(delay, max(0.0, deadline - self._clock())))
            return
        seen = self._generation
        async with self._lock:
            if self._generation != seen or self._next_fetch > self._clock():
                return
            try:
                await self._fetch()
            finally:
                self._generation += 1
                self._next_fetch = max(self._next_fetch, self._clock() + self._interval)

    async def _fetch(self) -> None:
        self._drop_idle()
        live = [f for f in self._followed.values() if f.query.source == "app" or f.query.builds]
        entries = self._entries
        if not live or entries is None:
            return
        started = self._now()
        clauses = [f'({query_filter(f.query)} AND timestamp>="{_rfc3339(f.lower)}")' for f in live]
        raw = await self._list(
            entries, " OR ".join(clauses), newest_first=False, page_size=FOLLOW_PAGE
        )
        for entry in raw:
            for followed in live:
                if _matches(followed.query, entry):
                    self._add(followed, entry)
        newest = _timestamp(raw[-1]) if raw else None
        for followed in live:
            bound = newest if len(raw) >= FOLLOW_PAGE and newest is not None else started - LAG
            followed.lower = max(followed.lower, bound)

    def _add(self, followed: _Followed, entry: Json) -> None:
        line = entry_line(entry, followed.query.source)
        if line is None:
            return
        insert = entry.get("insertId")
        key = insert if isinstance(insert, str) else f"{line.timestamp.isoformat()}|{line.text}"
        if key in followed.seen:
            return
        followed.seen[key] = None
        if len(followed.seen) > 2 * BUFFER_LINES:
            del followed.seen[next(iter(followed.seen))]
        self._seq += 1
        followed.lines.append((self._seq, line))

    def _configured(self) -> LogEntries:
        if self._entries is None:
            raise LogsNotConfiguredError("this agent has no log view")
        return self._entries

    def _charge(self, caller: str) -> None:
        bucket = self._callers.get(caller)
        if bucket is None:
            if len(self._callers) >= _MAX_CALLERS:
                self._callers = {k: v for k, v in self._callers.items() if not v.full()}
            bucket = self._callers[caller] = _Bucket(
                CALLER_READS_PER_MINUTE, CALLER_READ_BURST, self._clock
            )
        wait = bucket.take()
        if wait > 0:
            raise LogsRateLimitedError("too many log reads from you", math.ceil(wait))

    def _take_read(self) -> None:
        backoff = self._backoff_until - self._clock()
        if backoff > 0:
            raise LogsRateLimitedError("Cloud Logging asked the cell to wait", math.ceil(backoff))
        wait = self._reads.take()
        if wait > 0:
            raise LogsRateLimitedError("the cell's log reads are spent for now", math.ceil(wait))

    async def _list(
        self, entries: LogEntries, filter_: str, *, newest_first: bool, page_size: int
    ) -> list[Json]:
        self.upstream_calls += 1
        try:
            return await entries.list(filter_, newest_first=newest_first, page_size=page_size)
        except LogsRateLimitedError as exc:
            self._backoff_until = self._clock() + exc.retry_after
            self._next_fetch = max(self._next_fetch, self._backoff_until)
            raise

    async def _outcome(self, service: str) -> _Outcome:
        hit = self._outcomes.get(service)
        if hit is not None and self._clock() - hit[0] < HEALTH_CACHE_SECONDS:
            return hit[1]
        entries = self._configured()
        self._take_read()
        raw = await self._list(
            entries,
            health_filter(service, self._now() - AWAKE),
            newest_first=True,
            page_size=HEALTH_PAGE,
        )
        outcome = _outcome_of(raw)
        self._outcomes = {
            k: v for k, v in self._outcomes.items() if self._clock() - v[0] < HEALTH_CACHE_SECONDS
        }
        self._outcomes[service] = (self._clock(), outcome)
        return outcome


def _outcome_of(entries: list[Json]) -> _Outcome:
    """From request and system logs, newest first: the latest request, and a failure when the
    newest entry is a crash, or the newest ``FAILING_REQUESTS`` requests (or all there are) are
    server errors."""
    requests = [e for e in entries if isinstance(e.get("httpRequest"), dict)]
    last = _timestamp(requests[0]) if requests else None
    if not entries:
        return _Outcome(last_request=None, failure=None)
    if not isinstance(entries[0].get("httpRequest"), dict):
        return _Outcome(last_request=last, failure="crashed")
    statuses = [_status(cast(Json, e["httpRequest"])) for e in requests[:FAILING_REQUESTS]]
    failing = all(s is not None and s >= _HTTP_SERVER_ERROR for s in statuses)
    return _Outcome(last_request=last, failure="server_error" if failing else None)


def _matches(query: LogQuery, entry: Json) -> bool:
    resource = _obj(entry.get("resource"))
    labels = _obj(resource.get("labels"))
    if query.source == "app":
        return (
            resource.get("type") == "cloud_run_revision"
            and labels.get("service_name") == query.service
        )
    return resource.get("type") == "build" and labels.get("build_id") in query.builds


def _text(entry: Json) -> str:
    request = entry.get("httpRequest")
    if isinstance(request, dict):
        request = cast(Json, request)
        path = urlsplit(str(request.get("requestUrl") or "")).path or "/"
        status = _status(request)
        parts = [str(request.get("requestMethod") or "?"), path, str(status or "-")]
        if isinstance(latency := request.get("latency"), str):
            parts.append(latency)
        return " ".join(parts)
    text = entry.get("textPayload")
    if isinstance(text, str):
        return text
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        message = cast(Json, payload).get("message")
        if isinstance(message, str):
            return message
        return json.dumps(payload, sort_keys=True, default=str)
    return ""


def _status(request: Json) -> int | None:
    status = request.get("status")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _timestamp(entry: Json) -> datetime | None:
    for name in ("timestamp", "receiveTimestamp"):
        value = entry.get(name)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _obj(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _reason(response: httpx2.Response) -> str:
    try:
        error = _obj(_obj(response.json()).get("error"))
    except ValueError:
        return response.reason_phrase
    return f"{error.get('status', '')} {error.get('message', '')}".strip()


__all__ = ["CellLogHub", "CloudLoggingEntries", "LogEntries", "entry_line", "query_filter"]

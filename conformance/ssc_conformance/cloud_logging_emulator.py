"""An in-memory Cloud Logging API v2 ``entries:list``, for ``httpx2.MockTransport``.

It reads one log view and understands the part of the Logging query language the cell agent
writes: ``field="value"``, ``field=("a" OR "b")``, ``timestamp>=``, ``severity>=``,
``LOG_ID("...")``, ``AND``, ``OR`` and parentheses, with ``OR`` binding tighter than ``AND`` as
Cloud Logging's does. It counts every call and keeps every filter, so a test can check the
agent's quota and that no filter reaches another app.
"""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final, cast
from urllib.parse import quote

import httpx2

from ssc_conformance.cloud_run_emulator import PROJECT, REGION

type Json = dict[str, Any]
type Predicate = Callable[[Json], bool]

VIEW: Final = f"projects/{PROJECT}/locations/{REGION}/buckets/_Default/views/ssc-app-logs"
SEVERITIES: Final = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}
_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|(>=|=)|([A-Za-z_][A-Za-z0-9_.]*))')


class FilterError(ValueError):
    pass


class CloudLoggingEmulator:
    def __init__(
        self, *, views: tuple[str, ...] = (VIEW,), now: Callable[[], datetime] | None = None
    ) -> None:
        self.views = views
        self.entries: list[Json] = []
        self.calls: list[Json] = []
        self.refuse: int | None = None
        self._now = now or (lambda: datetime.now(UTC))
        self._ids = 0

    # ── test controls ────────────────────────────────────────────────────────

    def app_line(
        self, service: str, text: str, *, severity: str = "DEFAULT", at: datetime | None = None
    ) -> Json:
        return self._write(
            {"type": "cloud_run_revision", "labels": {"service_name": service}},
            "run.googleapis.com/stdout",
            at,
            severity=severity,
            textPayload=text,
        )

    def request(
        self, service: str, status: int, *, path: str = "/", at: datetime | None = None
    ) -> Json:
        return self._write(
            {"type": "cloud_run_revision", "labels": {"service_name": service}},
            "run.googleapis.com/requests",
            at,
            severity="ERROR" if status >= 500 else "INFO",  # noqa: PLR2004
            httpRequest={
                "requestMethod": "GET",
                "requestUrl": f"https://{service}.internal{path}",
                "status": status,
                "latency": "0.012s",
            },
        )

    def system_error(self, service: str, text: str, *, at: datetime | None = None) -> Json:
        return self._write(
            {"type": "cloud_run_revision", "labels": {"service_name": service}},
            "run.googleapis.com/varlog/system",
            at,
            severity="ERROR",
            textPayload=text,
        )

    def build_line(self, build_id: str, text: str, *, at: datetime | None = None) -> Json:
        return self._write(
            {"type": "build", "labels": {"build_id": build_id}}, "cloudbuild", at, textPayload=text
        )

    # ── transport ────────────────────────────────────────────────────────────

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host != "logging.googleapis.com":
            return _error(404, "NOT_FOUND", f"no host {request.url.host}")
        if request.method != "POST" or request.url.path != "/v2/entries:list":
            return _error(404, "NOT_FOUND", f"no route {request.method} {request.url.path}")
        body = cast(Json, json.loads(request.content))
        self.calls.append(body)
        if self.refuse is not None:
            return _error(self.refuse, "RESOURCE_EXHAUSTED", "refused by the test")
        if body.get("resourceNames") != list(self.views):
            return _error(403, "PERMISSION_DENIED", "only the cell's log views are readable")
        try:
            matches = parse_filter(str(body.get("filter") or ""))
        except FilterError as exc:
            return _error(400, "INVALID_ARGUMENT", str(exc))
        newest_first = body.get("orderBy") == "timestamp desc"
        found = sorted(
            (e for e in self.entries if matches(e)),
            key=lambda e: _moment(e["timestamp"]),
            reverse=newest_first,
        )
        size = int(body.get("pageSize") or 50)
        return httpx2.Response(200, json={"entries": found[:size]} if found else {})

    def _write(self, resource: Json, log_id: str, at: datetime | None, **fields: object) -> Json:
        self._ids += 1
        moment = at or self._now()
        entry: Json = {
            "insertId": f"e{self._ids:08d}",
            "logName": f"projects/{PROJECT}/logs/{quote(log_id, safe='')}",
            "resource": resource,
            "timestamp": moment.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "severity": "DEFAULT",
            **fields,
        }
        self.entries.append(entry)
        return entry


def parse_filter(text: str) -> Predicate:
    """``FilterError`` for anything outside the subset the agent writes."""
    tokens = _tokens(text)
    predicate, rest = _and(tokens)
    if rest:
        raise FilterError(f"unexpected {rest[0][1]!r}")
    return predicate


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text.rstrip()):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise FilterError(f"cannot read the filter at {text[pos : pos + 20]!r}")
        pos = m.end()
        lparen, rparen, string, op, word = m.groups()
        if lparen:
            out.append(("(", "("))
        elif rparen:
            out.append((")", ")"))
        elif string is not None:
            out.append(("str", string.replace('\\"', '"')))
        elif op:
            out.append(("op", op))
        else:
            out.append(("word", word))
    return out


def _and(tokens: list[tuple[str, str]]) -> tuple[Predicate, list[tuple[str, str]]]:
    parts = []
    first, tokens = _or(tokens)
    parts.append(first)
    while tokens and tokens[0] == ("word", "AND"):
        nxt, tokens = _or(tokens[1:])
        parts.append(nxt)
    return (lambda e: all(p(e) for p in parts)), tokens


def _or(tokens: list[tuple[str, str]]) -> tuple[Predicate, list[tuple[str, str]]]:
    parts = []
    first, tokens = _factor(tokens)
    parts.append(first)
    while tokens and tokens[0] == ("word", "OR"):
        nxt, tokens = _factor(tokens[1:])
        parts.append(nxt)
    return (lambda e: any(p(e) for p in parts)), tokens


def _factor(tokens: list[tuple[str, str]]) -> tuple[Predicate, list[tuple[str, str]]]:
    if not tokens:
        raise FilterError("the filter ends early")
    kind, value = tokens[0]
    if kind == "(":
        inner, rest = _and(tokens[1:])
        return inner, _expect(rest, ")")
    if kind != "word" or value in ("AND", "OR"):
        raise FilterError(f"unexpected {value!r}")
    if value == "LOG_ID":
        rest = _expect(tokens[1:], "(")
        if not rest or rest[0][0] != "str":
            raise FilterError("LOG_ID takes a string")
        suffix = "/logs/" + quote(rest[0][1], safe="")
        return (lambda e: str(e.get("logName", "")).endswith(suffix)), _expect(rest[1:], ")")
    if len(tokens) < 3 or tokens[1][0] != "op":  # noqa: PLR2004
        raise FilterError(f"{value} needs a comparison")
    field, op, rest = value, tokens[1][1], tokens[2:]
    if rest[0][0] == "(":
        values: list[str] = []
        rest = rest[1:]
        while True:
            if not rest or rest[0][0] != "str":
                raise FilterError("a value list holds strings")
            values.append(rest[0][1])
            rest = rest[1:]
            if rest and rest[0] == ("word", "OR"):
                rest = rest[1:]
                continue
            rest = _expect(rest, ")")
            break
        if op != "=":
            raise FilterError("a value list compares with =")
        return (lambda e: _field(e, field) in values), rest
    target = rest[0][1]
    return _compare(field, op, target), rest[1:]


def _compare(field: str, op: str, target: str) -> Predicate:
    if field == "severity":
        if target not in SEVERITIES:
            raise FilterError(f"no severity {target}")
        level = SEVERITIES[target]
        if op == ">=":
            return lambda e: SEVERITIES.get(str(e.get("severity", "DEFAULT")), 0) >= level
        return lambda e: e.get("severity", "DEFAULT") == target
    if field == "timestamp":
        moment = _moment(target)
        if op == ">=":
            return lambda e: _moment(e["timestamp"]) >= moment
        return lambda e: _moment(e["timestamp"]) == moment
    if op != "=":
        raise FilterError(f"{field} compares with =")
    return lambda e: _field(e, field) == target


def _expect(tokens: list[tuple[str, str]], kind: str) -> list[tuple[str, str]]:
    if not tokens or tokens[0][0] != kind:
        raise FilterError(f"expected {kind!r}")
    return tokens[1:]


def _field(entry: Json, path: str) -> object:
    value: object = entry
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = cast(Json, value).get(part)
    return value


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _error(status: int, reason: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"code": status, "status": reason, "message": message}}
    )

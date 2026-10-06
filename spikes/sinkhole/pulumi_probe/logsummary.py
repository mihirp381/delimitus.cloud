"""Reads the P=8 run's log (``pulumi up --logflow -v=9 --logtostderr`` with ``TF_LOG=DEBUG``) and
says where a rule's time goes: when each create started and ended (as the engine saw the
provider's ``Create``), the HTTP calls the provider made with their times, and any 429, retry or
sleep line.

The formats are Pulumi's glog lines (``I1006 05:55:55.123456 ...``) and Terraform's
(``2026-10-06T05:55:55.123Z [DEBUG] ...``). A line without a timestamp, such as the lines of an
HTTP dump, takes the last one seen. This is a reading aid: ``logs/p8.log`` is the data.
"""

import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

GLOG: Final = re.compile(r"(?<![\w])[IWEF]\d{4} (\d{2}):(\d{2}):(\d{2})\.(\d{3,6})")
TERRAFORM: Final = re.compile(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})\.(\d{1,9})")
CREATE: Final = re.compile(r"\.Create\((urn:pulumi:\S*?::([^:\s)]+))\)\s*(.*)")
REQUEST: Final = re.compile(r"(?:^|\s)(GET|POST|PUT|PATCH|DELETE) (/\S+) HTTP/[\d.]+")
RESPONSE: Final = re.compile(r"(?:^|\s)HTTP/[\d.]+ (\d{3})\b")
TROUBLE: Final = re.compile(
    r"\b429\b|too many requests|retry|retrying|sleep|backoff|rate.?limit|quota", re.IGNORECASE
)
RULE_TYPE: Final = "ResponsePolicyRule"
SHOWN_RULES: Final = 5
SHOWN_CALLS: Final = 12
SHOWN_TROUBLE: Final = 10
MAX_TROUBLE_LINE: Final = 200
HOUR: Final = 3600.0
DAY: Final = 86400.0


def seconds_of_day(line: str) -> float | None:
    for pattern in (GLOG, TERRAFORM):
        found = pattern.search(line)
        if found:
            h, m, s, frac = found.groups()
            return int(h) * 3600 + int(m) * 60 + int(s) + float("0." + frac)
    return None


@dataclass(slots=True)
class Create:
    name: str
    start: float
    end: float | None = None
    outcome: str = ""

    @property
    def seconds(self) -> float | None:
        return None if self.end is None else self.end - self.start


@dataclass(frozen=True, slots=True)
class Call:
    at: float
    method: str
    path: str
    status: str


@dataclass(slots=True)
class Summary:
    creates: list[Create] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    trouble: list[str] = field(default_factory=list)
    lines: int = 0
    origin: float = 0.0


def summarise(lines: Iterable[str]) -> Summary:
    """Times are seconds since the first rule create started. A clock that goes past midnight
    keeps counting."""
    summary = Summary()
    open_creates: dict[str, Create] = {}
    now = 0.0
    previous: float | None = None
    day = 0.0
    pending: tuple[float, str, str] | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        summary.lines += 1
        seen = seconds_of_day(line)
        if seen is not None:
            if previous is not None and seen < previous - HOUR:  # past midnight
                day += DAY
            previous = seen
            now = seen + day
        made = CREATE.search(line)
        if made and RULE_TYPE in made.group(1):
            name, rest = made.group(2), made.group(3)
            if "executing" in rest or "started" in rest:
                create = Create(name, now)
                open_creates[name] = create
                summary.creates.append(create)
            elif name in open_creates and ("success" in rest or "failed" in rest):
                create = open_creates.pop(name)
                create.end, create.outcome = now, "failed" if "failed" in rest else "success"
        request = REQUEST.search(line)
        if request:
            pending = (now, request.group(1), request.group(2))
        elif pending is not None:
            response = RESPONSE.search(line)
            if response:
                at, method, path = pending
                summary.calls.append(Call(at, method, path, response.group(1)))
                pending = None
        if TROUBLE.search(line) and not made:
            summary.trouble.append(line.strip()[:MAX_TROUBLE_LINE])
    if summary.creates:
        summary.origin = min(c.start for c in summary.creates)
    return summary


def kind(path: str) -> str:
    """The call without the rule's name or the query: ``POST .../rules``, ``GET .../rules/{rule}``."""
    base = path.split("?", 1)[0]
    return re.sub(r"(/rules/)[^/]+", r"\1{rule}", base)


def in_flight(creates: list[Create]) -> int:
    edges = sorted(
        [(c.start, 1) for c in creates] + [(c.end, -1) for c in creates if c.end is not None],
        key=lambda e: (e[0], e[1]),
    )
    peak = level = 0
    for _, step in edges:
        level += step
        peak = max(peak, level)
    return peak


def report(summary: Summary) -> list[str]:
    out = [f"log: {summary.lines} lines"]
    done = [c for c in summary.creates if c.seconds is not None]
    if not summary.creates:
        out.append("no ResponsePolicyRule Create lines found: check the log was taken with -v=9")
    else:
        durations = [c.seconds for c in done if c.seconds is not None]
        ends = [c.end for c in done if c.end is not None]
        span = (max(ends) - summary.origin) if ends else 0.0
        out.append(
            f"rule creates: {len(summary.creates)} started, {len(done)} ended, "
            f"{sum(c.outcome == 'failed' for c in done)} failed, up to {in_flight(summary.creates)} "
            f"at once, first start to last end {span:.1f}s"
        )
        if durations:
            out.append(
                f"  one create (engine to provider and back): median "
                f"{statistics.median(durations):.2f}s, max {max(durations):.2f}s"
            )
        if span:
            out.append(f"  {len(done) / span * 60:.1f} creates a minute over that span")
        out.append("  rule                      start    end      seconds")
        ordered = sorted(summary.creates, key=lambda c: c.start)
        shown = ordered
        if len(ordered) > 2 * SHOWN_RULES:
            shown = ordered[:SHOWN_RULES] + ordered[-SHOWN_RULES:]
        for c in shown:
            end = "-" if c.end is None else f"{c.end - summary.origin:7.2f}"
            took = "-" if c.seconds is None else f"{c.seconds:.2f}"
            out.append(f"  {c.name:<24} {c.start - summary.origin:7.2f}  {end:>7}  {took:>7}")
    counts: dict[tuple[str, str, str], int] = {}
    for call in summary.calls:
        key = (call.method, kind(call.path), call.status)
        counts[key] = counts.get(key, 0) + 1
    out.append(f"HTTP calls seen: {len(summary.calls)}")
    for (method, path, status), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"  {n:5d}  {method} {path} -> {status}")
    posts = [c.at for c in summary.calls if c.method == "POST" and kind(c.path).endswith("/rules")]
    if len(posts) > 1:
        gaps = [b - a for a, b in zip(posts, posts[1:], strict=False)]
        out.append(
            f"  rule POSTs: {len(posts)}, between neighbours median {statistics.median(gaps):.2f}s, "
            f"max {max(gaps):.2f}s"
        )
    out.append("  first calls (seconds since the first create started):")
    for call in summary.calls[:SHOWN_CALLS]:
        out.append(
            f"    {call.at - summary.origin:7.2f}  {call.method} {kind(call.path)} {call.status}"
        )
    out.append(f"429, retry, sleep, backoff or quota lines: {len(summary.trouble)}")
    out += [f"  {line}" for line in summary.trouble[:SHOWN_TROUBLE]]
    return out

"""``ssc.toml`` version ``ssc/v1``: what an app asks the platform for.

A manifest is a request, never enforcement: the platform compares it with what the environment
grants (``ssc_contracts.capabilities``) and decides. Frozen contract in
``docs/contracts/manifest.md`` (decision 013); a change that would alter the digest of a valid
manifest or refuse one that was accepted needs ``ssc/v2`` beside this module.

Refusals name the place in the file, ``ssc.toml:LINE:COL: field: message``. ``tomllib`` gives
positions only for syntax errors, so validation errors are placed through a key-path to position
map built from the text (``_Positions``). The digest (``ssc_shared.canonical.manifest_digest``) is
taken over the normalised model: defaults applied, sets sorted.
"""

import difflib
import json
import re
import tomllib
import zoneinfo
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, NoReturn, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from ssc_contracts.identity import EnvironmentName

SCHEMA_V1: Final = "ssc/v1"
MANIFEST_FILE: Final = "ssc.toml"
MAX_MANIFEST_BYTES: Final = 256 * 1024
MAX_PUBLIC_VALUES: Final = 50
MAX_CONNECTIONS: Final = 20
MAX_EGRESS_HOSTS: Final = 50
MAX_SCHEDULES: Final = 20
MAX_TIMEOUT_SECONDS: Final = 900
AUTO_PUBLIC_PREFIXES: Final = ("VITE_", "NEXT_PUBLIC_")
KV_FIX_IT: Final = (
    "a key-value store is not offered (STATE_KV_UNSUPPORTED); set postgres = true and keep the "
    "data in a table (for a cache, an UNLOGGED table with an expires_at column)"
)
SQLITE_FIX_IT: Final = (
    "SQLite on disk does not last (STATE_SQLITE_EPHEMERAL): the file system is memory and is lost "
    "when the instance stops; set postgres = true and keep the data in Postgres"
)

ResourceClassName = Literal["small", "medium", "large"]
_Loc = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class ResourceClass:
    """What one class name buys. Sizes are platform policy (decision 014), not manifest data."""

    vcpu: int
    memory_mib: int
    max_instances: int


RESOURCE_CLASSES: Final[Mapping[ResourceClassName, ResourceClass]] = MappingProxyType(
    {
        "small": ResourceClass(vcpu=1, memory_mib=512, max_instances=2),
        "medium": ResourceClass(vcpu=1, memory_mib=2048, max_instances=4),
        "large": ResourceClass(vcpu=2, memory_mib=4096, max_instances=8),
    }
)
SESSION_MAX_INSTANCES: Final = 1
SESSION_FRAMEWORKS: Final = frozenset({"streamlit", "gradio", "dash", "shiny"})
_SESSION_COMMANDS: Final = frozenset({"streamlit", "gradio", "shiny"})
_SHINY_R: Final = re.compile(r"\bshiny::runApp\b")


class NestedFieldError(ValueError):
    """A refusal that points below the field that raised it (``at`` is appended to its loc)."""

    def __init__(self, message: str, at: tuple[str | int, ...]) -> None:
        super().__init__(message)
        self.at = at


# --- value rules -------------------------------------------------------------------------

_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_PATH_CHARS = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_ZONE = re.compile(r"UTC|[A-Z][A-Za-z]+(/[A-Za-z0-9_+-]+){1,2}")
_RESERVED_ENV = frozenset({"PORT", "HOME", "PATH", "DATABASE_URL"})
_SECRET_WORDS = ("SECRET", "PASSWORD", "PASSWD", "SERVICE_ROLE", "PRIVATE_KEY")
_KV_WORDS = frozenset({"kv", "redis", "valkey", "memcached", "keyvalue", "key_value", "cache"})
_SQLITE_WORDS = frozenset({"sqlite", "sqlite3"})
_TABLE_HINT: Final = "write [state] with postgres = true"
_STATE_FIX_ITS: Final[Mapping[str, str]] = MappingProxyType(
    {**dict.fromkeys(_KV_WORDS, KV_FIX_IT), **dict.fromkeys(_SQLITE_WORDS, SQLITE_FIX_IT)}
)


def _name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(
            "must start with a lowercase letter and use only a-z, 0-9 and -, at most 63 characters"
        )
    return value


def _http_path(*, query: bool, max_length: int) -> Callable[[str], str]:
    def check(value: str) -> str:
        path, mark, rest = value.partition("?")
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("must start with a single /")
        if len(value) > max_length:
            raise ValueError(f"at most {max_length} characters")
        if mark and not query:
            raise ValueError("a path only, without ?query")
        if not _PATH_CHARS.fullmatch(path) or not _PATH_CHARS.fullmatch(rest.replace("?", "")):
            raise ValueError("contains a character that is not allowed in a URL path")
        return value

    return check


def _unicode(value: str) -> None:
    try:
        value.encode()
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None


def _start_command(value: str) -> str:
    _unicode(value)
    if not value.strip():
        raise ValueError("must not be empty")
    if len(value) > 1024:
        raise ValueError("at most 1024 characters")
    if any(c in value for c in "\x00\r\n"):
        raise ValueError("must be one line")
    return value


def _public_name(value: str) -> str:
    if not _ENV_NAME.fullmatch(value):
        raise ValueError("must be an upper-case name of A-Z, 0-9 and _, at most 128 characters")
    if value in _RESERVED_ENV or value.startswith(("SSC_", "RAILPACK_")):
        raise ValueError("is set by the platform and cannot be a public build value")
    if any(word in value for word in _SECRET_WORDS):
        raise ValueError(
            "looks like a secret; public build values end up in the browser bundle, "
            "so keep this out of ssc.toml"
        )
    return value


def _auto_public(name: str) -> bool:
    return any(name.startswith(p) and len(name) > len(p) for p in AUTO_PUBLIC_PREFIXES)


def _public_value(value: str) -> str:
    _unicode(value)
    if len(value) > 4096:
        raise ValueError("at most 4096 characters")
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


def _hostname(value: str) -> str:
    if "://" in value or "/" in value:
        raise ValueError("write the host name only, without a scheme or path")
    if "*" in value:
        raise ValueError("wildcards are not supported in ssc/v1; list each host")
    if value.startswith("[") or value.count(":") > 1:
        raise ValueError("IP addresses are not allowed; name the host")
    if ":" in value:
        raise ValueError("write the host name only, without a port")
    if value != value.lower():
        raise ValueError("write host names in lower case")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise ValueError("must be a DNS host name such as api.example.com")
    if labels[-1].isdigit():
        raise ValueError("IP addresses are not allowed; name the host")
    return value


def _timezone(value: str) -> str:
    if value == "UTC":  # needs no time zone database (Windows without tzdata)
        return value
    if not _ZONE.fullmatch(value):
        raise ValueError("must be UTC or an IANA zone such as Europe/London")
    try:
        zoneinfo.ZoneInfo(value)
    except ValueError, OSError, KeyError:
        raise ValueError("is not a known IANA time zone") from None
    return value


_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_CRON_FIELDS: tuple[tuple[str, int, int, tuple[str, ...]], ...] = (
    ("minute", 0, 59, ()),
    ("hour", 0, 23, ()),
    ("day-of-month", 1, 31, ()),
    ("month", 1, 12, _MONTHS),
    ("day-of-week", 0, 7, _DAYS),
)
_CRON_CHARS = re.compile(r"[0-9A-Za-z*/,\- \t]+")


def _cron_value(text: str, low: int, high: int, names: tuple[str, ...]) -> int:
    if text in names:
        return names.index(text) + low
    if not text.isdigit() or len(text) > 2 or not low <= int(text) <= high:
        raise ValueError(f"{text!r} is outside {low}-{high}")
    return int(text)


def _cron_field(text: str, low: int, high: int, names: tuple[str, ...]) -> set[int]:
    values: set[int] = set()
    for term in text.split(","):
        base, slash, step_text = term.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or not 1 <= int(step_text) <= high:
                raise ValueError(f"step {step_text!r} must be 1-{high}")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _cron_value(first, low, high, names)
            end = _cron_value(last, low, high, names)
            if end < start:
                raise ValueError(f"range {base!r} runs backwards")
        else:
            start = _cron_value(base, low, high, names)
            end = high if slash else start
        values.update(range(start, end + 1, step))
    return values


def _cron(value: str) -> str:
    """Five fields, numbers, names, ``*``, ranges, lists and steps: the subset cronsim reads."""
    if not _CRON_CHARS.fullmatch(value):
        raise ValueError("must be five cron fields using 0-9, names, * , - and /")
    fields = value.upper().split()
    if len(fields) != 5:
        raise ValueError("must have exactly five fields: minute hour day-of-month month weekday")
    parsed: list[set[int]] = []
    for text, (label, low, high, names) in zip(fields, _CRON_FIELDS, strict=True):
        try:
            parsed.append(_cron_field(text, low, high, names))
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
    days, months = parsed[2], parsed[3]
    if min(days) > max(_DAYS_IN_MONTH[m - 1] for m in months):
        raise ValueError("day-of-month never occurs in the chosen months")
    return " ".join(fields)


def _unique_sorted[T](values: tuple[T, ...], key: Callable[[T], str], what: str) -> tuple[T, ...]:
    seen: set[str] = set()
    for index, value in enumerate(values):
        k = key(value)
        if k in seen:
            raise NestedFieldError(f"{what} {k!r} is listed twice", at=(index,))
        seen.add(k)
    return tuple(sorted(values, key=key))


Name = Annotated[StrictStr, AfterValidator(_name)]
HealthPath = Annotated[StrictStr, AfterValidator(_http_path(query=False, max_length=256))]
SchedulePath = Annotated[StrictStr, AfterValidator(_http_path(query=True, max_length=512))]
StartCommand = Annotated[StrictStr, AfterValidator(_start_command)]
PublicName = Annotated[StrictStr, AfterValidator(_public_name)]
PublicValue = Annotated[StrictStr, AfterValidator(_public_value)]
Hostname = Annotated[StrictStr, AfterValidator(_hostname)]
TimeZoneName = Annotated[StrictStr, AfterValidator(_timezone)]
CronExpression = Annotated[StrictStr, AfterValidator(_cron)]


# --- models ------------------------------------------------------------------------------


class _Table(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", serialize_by_alias=True)


class Runtime(_Table):
    """How the app runs. ``sessions = true`` caps it at one instance."""

    class_: ResourceClassName = Field(default="small", alias="class")
    port: StrictInt = Field(default=8080, ge=1, le=65535)
    health_path: HealthPath = "/"
    start: StartCommand | None = None
    sessions: StrictBool = False


class Build(_Table):
    """Public build-time values per environment. Names are VITE_*, NEXT_PUBLIC_* or listed."""

    public_names: tuple[PublicName, ...] = Field(default=(), max_length=MAX_PUBLIC_VALUES)
    public_env: dict[
        EnvironmentName,
        Annotated[dict[PublicName, PublicValue], Field(max_length=MAX_PUBLIC_VALUES)],
    ] = Field(default_factory=dict[EnvironmentName, dict[str, str]])

    @field_validator("public_names")
    @classmethod
    def _names_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, str, "name")

    @model_validator(mode="after")
    def _names_public(self) -> Self:
        listed = set(self.public_names)
        for env, values in self.public_env.items():
            for name in values:
                if name not in listed and not _auto_public(name):
                    raise NestedFieldError(
                        "is not public: use a VITE_ or NEXT_PUBLIC_ name, "
                        "or add it to build.public_names",
                        at=("public_env", env, name),
                    )
        return self


class State(_Table):
    """Durable state. Postgres only; a key-value or SQLite request gets its fix-it."""

    postgres: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def _only_postgres(cls, data: Any) -> Any:
        if isinstance(data, str):
            raise ValueError(f"must be a table; {_STATE_FIX_ITS.get(data.lower(), _TABLE_HINT)}")
        key = _state_key(data)
        if key is not None:
            raise NestedFieldError(_STATE_FIX_ITS[key.lower()], at=(key,))
        return data


def _state_key(data: object) -> str | None:
    keys: Iterable[object] = cast("dict[object, object]", data) if isinstance(data, dict) else ()
    return next((k for k in keys if isinstance(k, str) and k.lower() in _STATE_FIX_ITS), None)


class Connections(_Table):
    """Company data connections the app asks for, by name."""

    names: tuple[Name, ...] = Field(default=(), max_length=MAX_CONNECTIONS)

    @field_validator("names")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, str, "connection")


class Egress(_Table):
    """Outbound hosts the app asks to call. Exact host names; no wildcards in v1."""

    hosts: tuple[Hostname, ...] = Field(default=(), max_length=MAX_EGRESS_HOSTS)

    @field_validator("hosts")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(value, str, "host")


class Schedule(_Table):
    """One timer: a cron expression in a time zone that calls a path on the app."""

    name: Name
    cron: CronExpression
    path: SchedulePath
    timezone: TimeZoneName = "UTC"
    method: Literal["GET", "POST"] = "POST"
    timeout_seconds: StrictInt = Field(default=60, ge=1, le=MAX_TIMEOUT_SECONDS)


class Manifest(_Table):
    """``ssc.toml`` ``ssc/v1``. Build it with ``load_manifest`` or ``model_validate`` (aliases)."""

    schema_: Literal["ssc/v1"] = Field(alias="schema")
    runtime: Runtime = Field(default_factory=Runtime)
    build: Build = Field(default_factory=Build)
    state: State = Field(default_factory=State)
    connections: Connections = Field(default_factory=Connections)
    egress: Egress = Field(default_factory=Egress)
    schedules: tuple[Schedule, ...] = Field(default=(), max_length=MAX_SCHEDULES)

    @field_validator("schedules")
    @classmethod
    def _unique(cls, value: tuple[Schedule, ...]) -> tuple[Schedule, ...]:
        try:
            return _unique_sorted(value, lambda s: s.name, "schedule")
        except NestedFieldError as exc:
            raise NestedFieldError(str(exc), at=(*exc.at, "name")) from None


def default_manifest() -> Manifest:
    """What an app without ``ssc.toml`` gets."""
    return Manifest.model_validate({"schema": SCHEMA_V1})


def session_framework(start: str | None) -> str | None:
    """The session framework a start command runs (``uv run streamlit run app.py``), or None."""
    if start is None:
        return None
    for token in start.split():
        name = token.strip("\"'`").rsplit("/", 1)[-1].lower()
        if name in _SESSION_COMMANDS:
            return name
    return "shiny" if _SHINY_R.search(start) else None


def is_session_app(runtime: Runtime, framework: str | None = None) -> bool:
    """Whether the app runs as a session app: ``sessions = true``, a session framework the build
    detected (``framework``), or a session framework's start command. Derived, never written into
    the manifest, so it does not change the digest."""
    if runtime.sessions or session_framework(runtime.start) is not None:
        return True
    return framework is not None and framework.lower() in SESSION_FRAMEWORKS


def max_instances(runtime: Runtime, framework: str | None = None) -> int:
    """The instance ceiling: one for a session app (``is_session_app``), else the class limit."""
    if is_session_app(runtime, framework):
        return SESSION_MAX_INSTANCES
    return RESOURCE_CLASSES[runtime.class_].max_instances


# --- refusals ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class ManifestProblem:
    """One refusal, placed in the file."""

    line: int
    column: int
    field: str
    message: str

    def __str__(self) -> str:
        return f"{MANIFEST_FILE}:{self.line}:{self.column}: {self.field}: {self.message}"


class ManifestError(ValueError):
    """All problems in file order; ``line``, ``column``, ``field``, ``message`` are the first's."""

    def __init__(self, problems: tuple[ManifestProblem, ...]) -> None:
        self.problems = tuple(sorted(problems))
        super().__init__("\n".join(str(p) for p in self.problems))

    @property
    def line(self) -> int:
        return self.problems[0].line

    @property
    def column(self) -> int:
        return self.problems[0].column

    @property
    def field(self) -> str:
        return self.problems[0].field

    @property
    def message(self) -> str:
        return self.problems[0].message


def load_manifest(source: str | bytes) -> Manifest:
    """Parse and validate ``ssc.toml`` text; raises ``ManifestError`` listing every problem."""
    text = _decode(source)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError((ManifestProblem(exc.lineno, exc.colno, "syntax", exc.msg),)) from None
    at = _Positions(text).at
    schema = data.get("schema")
    if schema is not None and schema != SCHEMA_V1:
        line, column = _where(at, ("schema",))
        shown = json.dumps(schema, default=str)
        message = f'{shown} is not a schema this version reads; it reads "{SCHEMA_V1}"'
        raise ManifestError((ManifestProblem(line, column, "schema", message),))
    try:
        return Manifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(tuple(_problem(at, e) for e in exc.errors())) from None


def _decode(source: str | bytes) -> str:
    if isinstance(source, str):
        try:
            source = source.encode()
        except UnicodeEncodeError as exc:
            _refuse_encoding(source[: exc.start])
    if len(source) > MAX_MANIFEST_BYTES:
        raise ManifestError(
            (ManifestProblem(1, 1, "(file)", f"larger than {MAX_MANIFEST_BYTES // 1024} KiB"),)
        )
    try:
        return source.decode().removeprefix("\ufeff")
    except UnicodeDecodeError as exc:
        _refuse_encoding(source[: exc.start].decode())


def _refuse_encoding(before: str) -> NoReturn:
    line = before.count("\n") + 1
    column = len(before) - before.rfind("\n")
    raise ManifestError((ManifestProblem(line, column, "(file)", "not UTF-8 text"),))


def _problem(at: dict[_Loc, tuple[int, int]], error: Mapping[str, Any]) -> ManifestProblem:
    loc: _Loc = tuple(p for p in error["loc"] if p != "[key]")
    cause = error.get("ctx", {}).get("error")
    if isinstance(cause, NestedFieldError):
        loc = (*loc, *cause.at)
    line, column = _where(at, loc)
    return ManifestProblem(line, column, _field(loc), _message(loc, error))


def _where(at: dict[_Loc, tuple[int, int]], loc: _Loc) -> tuple[int, int]:
    path = loc
    while path:
        if path in at:
            return at[path]
        path = path[:-1]
    return (1, 1)


def _field(loc: _Loc) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            name = part if _BARE_KEY.fullmatch(part) else json.dumps(part)
            out += f".{name}" if out else name
    return out or "(file)"


_TABLES: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "": Manifest,
        "runtime": Runtime,
        "build": Build,
        "state": State,
        "connections": Connections,
        "egress": Egress,
        "schedules": Schedule,
    }
)
_PLAIN: Final[Mapping[str, str]] = MappingProxyType(
    {
        "model_type": "must be a table",
        "dict_type": "must be a table",
        "tuple_type": "must be an array",
        "list_type": "must be an array",
        "int_type": "must be a whole number",
        "string_type": "must be a quoted string",
        "bool_type": "must be true or false",
        "too_long": "at most {max_length} entries",
        "string_too_long": "at most {max_length} characters",
    }
)


def _message(loc: _Loc, error: Mapping[str, Any]) -> str:
    kind: str = error["type"]
    ctx: dict[str, Any] = error.get("ctx", {})
    cause = ctx.get("error")
    if kind == "value_error" and isinstance(cause, ValueError):
        return str(cause)
    if kind == "missing":
        return (
            f'required; start ssc.toml with schema = "{SCHEMA_V1}"'
            if loc == ("schema",)
            else "required"
        )
    if kind == "extra_forbidden":
        return "unknown key" + _suggest(loc)
    if kind in _PLAIN:
        return _PLAIN[kind].format_map(ctx)
    msg: str = error["msg"]
    head = "Input should be "
    return (
        "must be " + msg.removeprefix(head) if msg.startswith(head) else msg[:1].lower() + msg[1:]
    )


def _suggest(loc: _Loc) -> str:
    parent = [p for p in loc[:-1] if isinstance(p, str)]
    model = _TABLES.get(parent[0] if parent else "")
    if model is None or not loc or not isinstance(loc[-1], str):
        return ""
    keys = [f.alias or n for n, f in model.model_fields.items()]
    close = difflib.get_close_matches(loc[-1], keys, n=1, cutoff=0.75)
    return f"; did you mean {close[0]!r}?" if close else ""


# --- key path -> position ----------------------------------------------------------------

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_SCALAR = re.compile(r"[^,\]}\r\n#]*")
_ESCAPES: Final[Mapping[str, str]] = MappingProxyType(
    {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "e": "\x1b", '"': '"', "\\": "\\"}
)
_HEX_ESCAPES: Final[Mapping[str, int]] = MappingProxyType({"x": 2, "u": 4, "U": 8})


class _ScanError(Exception):
    pass


class _Positions:
    """Where each key, table and array element starts, for text ``tomllib`` already accepted.

    Only positions are recovered; values come from ``tomllib``. A scan failure leaves the map
    partial, and lookups fall back to the nearest known parent, then to line 1.
    """

    def __init__(self, text: str) -> None:
        self._s = text
        self._i = 0
        self._starts = [0, *(m.end() for m in re.finditer("\n", text))]
        self._arrays: dict[_Loc, int] = {}
        self.at: dict[_Loc, tuple[int, int]] = {}
        try:
            self._document()
        except _ScanError, IndexError, ValueError:
            pass

    def _pos(self, index: int) -> tuple[int, int]:
        line = bisect_right(self._starts, index)
        return line, index - self._starts[line - 1] + 1

    def _mark(self, base: int, path: _Loc, index: int) -> None:
        position = self._pos(index)
        for n in range(base + 1, len(path)):
            self.at.setdefault(path[:n], position)
        self.at[path] = position

    def _document(self) -> None:
        table: _Loc = ()
        while True:
            self._blank()
            if self._i >= len(self._s):
                return
            start = self._i
            if self._s.startswith("[[", start):
                self._i += 2
                table = self._header(array=True)
                self._expect("]]")
                self._mark(0, table, start)
            elif self._s[start] == "[":
                self._i += 1
                table = self._header(array=False)
                self._expect("]")
                self._mark(0, table, start)
            else:
                self._pair(table)

    def _header(self, *, array: bool) -> _Loc:
        keys = self._keys()
        out: list[str | int] = []
        for n, key in enumerate(keys):
            out.append(key)
            here = tuple(out)
            if array and n == len(keys) - 1:
                index = self._arrays.get(here, 0)
                self._arrays[here] = index + 1
                out.append(index)
            elif here in self._arrays:
                out.append(self._arrays[here] - 1)
        return tuple(out)

    def _pair(self, table: _Loc) -> None:
        start = self._i
        path = (*table, *self._keys())
        self._mark(len(table), path, start)
        self._spaces()
        self._expect("=")
        self._spaces()
        self._value(path)

    def _keys(self) -> list[str]:
        keys: list[str] = []
        while True:
            self._spaces()
            keys.append(self._key())
            self._spaces()
            if self._s[self._i] != ".":
                return keys
            self._i += 1

    def _key(self) -> str:
        char = self._s[self._i]
        if char == '"':
            return self._basic()
        if char == "'":
            return self._literal()
        match = _BARE_KEY.match(self._s, self._i)
        if match is None:
            raise _ScanError
        self._i = match.end()
        return match.group()

    def _value(self, path: _Loc) -> None:
        s, i = self._s, self._i
        if s.startswith('"""', i) or s.startswith("'''", i):
            self._multiline(s[i])
        elif s[i] == '"':
            self._basic()
        elif s[i] == "'":
            self._literal()
        elif s[i] == "[":
            self._array(path)
        elif s[i] == "{":
            self._inline(path)
        else:
            match = _SCALAR.match(s, i)
            if match is None or match.end() == i:
                raise _ScanError
            self._i = match.end()

    def _array(self, path: _Loc) -> None:
        self._i += 1
        index = 0
        while True:
            self._blank()
            if self._s[self._i] == "]":
                self._i += 1
                return
            here = (*path, index)
            self.at[here] = self._pos(self._i)
            self._value(here)
            index += 1
            self._blank()
            if self._s[self._i] == ",":
                self._i += 1

    def _inline(self, path: _Loc) -> None:
        self._i += 1
        while True:
            self._blank()
            char = self._s[self._i]
            if char == "}":
                self._i += 1
                return
            if char == ",":
                self._i += 1
                continue
            self._pair(path)

    def _basic(self) -> str:
        s = self._s
        j = self._i + 1
        out: list[str] = []
        while s[j] != '"':
            if s[j] == "\\":
                code = s[j + 1]
                if code in _HEX_ESCAPES:
                    width = _HEX_ESCAPES[code]
                    out.append(chr(int(s[j + 2 : j + 2 + width], 16)))
                    j += 2 + width
                    continue
                out.append(_ESCAPES.get(code, code))
                j += 2
                continue
            out.append(s[j])
            j += 1
        self._i = j + 1
        return "".join(out)

    def _literal(self) -> str:
        end = self._s.index("'", self._i + 1)
        value = self._s[self._i + 1 : end]
        self._i = end + 1
        return value

    def _multiline(self, quote: str) -> None:
        s = self._s
        delimiter = quote * 3
        j = self._i + 3
        while True:
            if quote == '"' and s[j] == "\\":
                j += 2
                continue
            if s.startswith(delimiter, j):
                end = j + 3
                while end < len(s) and s[end] == quote and end < j + 5:
                    end += 1
                self._i = end
                return
            j += 1

    def _expect(self, token: str) -> None:
        if not self._s.startswith(token, self._i):
            raise _ScanError
        self._i += len(token)

    def _spaces(self) -> None:
        s = self._s
        while self._i < len(s) and s[self._i] in " \t":
            self._i += 1

    def _blank(self) -> None:
        s = self._s
        while self._i < len(s):
            char = s[self._i]
            if char in " \t\r\n":
                self._i += 1
            elif char == "#":
                end = s.find("\n", self._i)
                self._i = len(s) if end < 0 else end
            else:
                return


# --- writing -----------------------------------------------------------------------------


def dump_manifest(manifest: Manifest) -> str:
    """Render ``ssc.toml`` text that ``load_manifest`` reads back to an equal model."""
    rt = manifest.runtime
    lines = [
        f"schema = {_toml(manifest.schema_)}",
        "",
        "[runtime]",
        f"class = {_toml(rt.class_)}",
        f"port = {rt.port}",
        f"health_path = {_toml(rt.health_path)}",
        *([f"start = {_toml(rt.start)}"] if rt.start is not None else []),
        f"sessions = {_toml(rt.sessions)}",
        "",
        "[build]",
        f"public_names = {_toml(manifest.build.public_names)}",
    ]
    for env, values in sorted(manifest.build.public_env.items()):
        lines += ["", f"[build.public_env.{env}]"]
        lines += [f"{name} = {_toml(value)}" for name, value in sorted(values.items())]
    lines += [
        "",
        "[state]",
        f"postgres = {_toml(manifest.state.postgres)}",
        "",
        "[connections]",
        f"names = {_toml(manifest.connections.names)}",
        "",
        "[egress]",
        f"hosts = {_toml(manifest.egress.hosts)}",
    ]
    for schedule in manifest.schedules:
        lines += ["", "[[schedules]]"]
        lines += [f"{key} = {_toml(value)}" for key, value in schedule.model_dump().items()]
    return "\n".join(lines) + "\n"


def _toml(value: str | int | tuple[str, ...]) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, tuple):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    out: list[str] = []
    for char in value:
        if char in '"\\':
            out.append("\\" + char)
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'

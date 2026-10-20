"""Redaction of secret-shaped text in logs and error messages (SSC-026).

Patterns only in MVP: well-known credential formats, bearer, authorization and cookie headers,
signed-URL signatures, credentials in URLs and ``name=value`` pairs whose name says secret. Each
pattern runs in linear time on one long line. Fingerprints of the stored values come later.
``install`` makes every log record in the process pass through ``redact`` when it is created, so
no handler or formatter configured afterwards can skip it.
"""

import logging
import re
import traceback
from collections.abc import Callable, Mapping
from typing import Any, Final

MASK: Final = "[redacted]"

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[rs]k_(live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-(ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"https://hooks\.slack\.com/services/\S+"),
)
_COOKIE: Final = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)\S[^\r\n]*")
_AUTHORIZATION: Final = re.compile(
    r"(?i)\b(authorization\s*:\s*[A-Za-z][\w.-]*\s+)(?!\[redacted\])\S+"
)
_BEARER: Final = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SIGNATURE_PARAM: Final = re.compile(
    r"(?i)([?&](?:x-goog-signature|x-amz-signature|signature|sig)=)[^&\s]+"
)
_SIGNATURE_HEADER: Final = re.compile(r"(?i)\b((?:x-goog-|x-amz-)?signature\s*:\s*)\S{8,}")
_URL_CREDENTIALS: Final = re.compile(
    r"(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://[^/\s:@]++:)[^\s/]+@", re.I
)
_NAME_CHARS: Final = r"[A-Z0-9_.-]"
_UNLESS_MASKED: Final = r"(?!bearer\s|basic\s|\[redacted\]|[\w.-]+\s+\[redacted\])"
_ASSIGNMENT: Final = re.compile(
    rf"(?i)(?<!{_NAME_CHARS})(?={_NAME_CHARS}*?(?:secret|passw(?:or)?d|passwd|token|api[_-]?key|"
    rf"private[_-]?key|credential|auth))(?P<name>{_NAME_CHARS}++)(?P<sep>\s*+[:=]\s*+|\"\s*+:\s*+)"
    rf"(?:(?P<q>[\"']){_UNLESS_MASKED}(?:(?!(?P=q))[^\n]){{4,}}(?P=q)|"
    rf"(?P<lead>[\"']?){_UNLESS_MASKED}[^\s\"',;&}}]{{4,}})"
)


def _mask_assignment(m: re.Match[str]) -> str:
    quote = m.group("q") or ""
    return f"{m.group('name')}{m.group('sep')}{quote or m.group('lead')}{MASK}{quote}"


def redact(text: str) -> str:
    """``text`` with every secret-shaped part replaced by ``[redacted]``."""
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    text = _COOKIE.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _AUTHORIZATION.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _SIGNATURE_PARAM.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _SIGNATURE_HEADER.sub(lambda m: f"{m.group(1)}{MASK}", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}@", text)
    return _ASSIGNMENT.sub(_mask_assignment, text)


_Args = tuple[object, ...] | Mapping[str, object] | None


def _redact_args(args: _Args) -> _Args:
    if isinstance(args, tuple):
        return tuple(redact(a) if isinstance(a, str) else a for a in args)
    if args is None:
        return None
    return {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact a record in place: its message and each string argument, its exception text, its
    stack and every string attribute added through ``extra``. Arguments stay a tuple or mapping
    of the same shape, which some formatters (uvicorn's access log) unpack. They are merged into
    the message when it cannot be formatted or a secret survives formatting (split between
    message and argument, or inside a non-string argument)."""
    try:
        message = record.getMessage()
    except TypeError, ValueError:
        message = None
    if message is not None and isinstance(record.msg, str) and redact(message) != message:
        record.msg, record.args = redact(record.msg), _redact_args(record.args)
        try:
            message = record.getMessage()
        except TypeError, ValueError:
            message = None
    if message is None or not isinstance(record.msg, str) or redact(message) != message:
        record.msg = redact(f"{record.msg} {record.args}" if message is None else message)
        record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    for name, value in list(vars(record).items()):
        if name not in _STANDARD and isinstance(value, str):
            setattr(record, name, redact(value))
    return record


_STANDARD: Final = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    """Redacts each record a handler is given, including the ``extra`` a record factory never
    sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


class _RedactingFactory:
    def __init__(self, inner: Callable[..., logging.LogRecord]) -> None:
        self.inner = inner

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        return redact_record(self.inner(*args, **kwargs))


def installed() -> bool:
    """Whether :func:`install` has run in this process (the record factory redacts)."""
    return isinstance(logging.getLogRecordFactory(), _RedactingFactory)


def install() -> None:
    """Redact every log record this process creates from now on, and every record the root
    logger's handlers emit. Calling it twice is harmless."""
    factory = logging.getLogRecordFactory()
    if not isinstance(factory, _RedactingFactory):
        logging.setLogRecordFactory(_RedactingFactory(factory))
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())

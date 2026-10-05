"""Redaction of secret-shaped text in logs and error messages (SSC-026).

Patterns only in MVP: well-known credential formats, bearer headers, credentials in URLs and
``name=value`` pairs whose name says secret. Fingerprints of the stored values come later.
``install`` makes every log record in the process pass through ``redact`` when it is created, so
no handler or formatter configured afterwards can skip it.
"""

import logging
import re
import traceback
from collections.abc import Callable
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
)
_BEARER: Final = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_CREDENTIALS: Final = re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.I)
_ASSIGNMENT: Final = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(secret|passw(or)?d|passwd|token|api[_-]?key|private[_-]?key|"
    r"credential|auth)[A-Z0-9_.-]*)(\s*[:=]\s*|\"\s*:\s*\")([\"']?)"
    r"(?!bearer\s|basic\s|\[redacted\])[^\s\"',;&}]{4,}"
)


def redact(text: str) -> str:
    """``text`` with every secret-shaped part replaced by ``[redacted]``."""
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group(1)}{MASK}@", text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(4)}{m.group(5)}{MASK}", text)


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact a record in place: its message with arguments merged in, its exception text, its
    stack and every string attribute added through ``extra``. A message with nothing to redact
    keeps its arguments, which some formatters (uvicorn's access log) read."""
    try:
        message, merged = record.getMessage(), False
    except TypeError, ValueError:
        message, merged = f"{record.msg} {record.args}", True
    redacted = redact(message)
    if merged or redacted != message or not isinstance(record.msg, str):
        record.msg = redacted
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


def install() -> None:
    """Redact every log record this process creates from now on, and every record the root
    logger's handlers emit. Calling it twice is harmless."""
    factory = logging.getLogRecordFactory()
    if not isinstance(factory, _RedactingFactory):
        logging.setLogRecordFactory(_RedactingFactory(factory))
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())

"""SSC-026: the redaction filter for logs and error text, by pattern. Every credential here is
assembled at run time and fake."""

import logging
from collections.abc import Iterator

import pytest

from ssc_shared import redaction
from ssc_shared.redaction import MASK, RedactingFilter, redact, redact_record

FAKE = "Q7" * 12
SHAPED = (
    "AKIA" + "Z7Q3K9XWP2LMN4RT",
    "ghp_" + "a1" * 18,
    "github_pat_" + "b2" * 12,
    "xoxb-" + "1234567890-abcdef",
    "sk_" + "live_" + FAKE,
    "AIza" + "c" * 35,
    "ya29." + FAKE,
    "eyJ" + "hbGciOiJIUzI1" + "." + "eyJzdWIiOiIx" + "." + "c2lnbmF0dXJl",
    "sk-" + "ant-" + FAKE,
    "-----BEGIN " + "PRIVATE KEY-----\nMIIEv" + FAKE + "\n-----END " + "PRIVATE KEY-----",
)


@pytest.mark.parametrize("secret", SHAPED)
def test_a_known_credential_format_is_masked(secret: str) -> None:
    out = redact(f"before {secret} after")
    assert secret not in out
    assert out == f"before {MASK} after"


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        (f"Authorization: Bearer {FAKE}", f"Authorization: Bearer {MASK}"),
        (f"postgres://ssc:{FAKE}@db/ssc", f"postgres://ssc:{MASK}@db/ssc"),
        (f"STRIPE_SECRET={FAKE} next", f"STRIPE_SECRET={MASK} next"),
        (f"password: {FAKE}", f"password: {MASK}"),
        (f'{{"api_key": "{FAKE}"}}', f'{{"api_key": "{MASK}"}}'),
        (f"db_passwd='{FAKE}'", f"db_passwd='{MASK}'"),
    ],
)
def test_a_secret_in_context_is_masked(text: str, masked: str) -> None:
    assert redact(text) == masked


def test_ordinary_text_is_left_alone() -> None:
    for text in (
        "deployment dep_abc went live on revision ssc-a-x-00002",
        "secret STRIPE_KEY is version 3",
        "token expired",
        "https://ssc.example.test/v1/apps",
    ):
        assert redact(text) == text


def record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    rec = logging.makeLogRecord({"msg": msg, "args": args or None, **extra})
    rec.levelno, rec.levelname = logging.WARNING, "WARNING"
    return rec


def test_a_record_is_redacted_in_its_message_args_extra_and_exception() -> None:
    try:
        raise ValueError(f"bad password={FAKE}")
    except ValueError:
        import sys  # noqa: PLC0415

        info = sys.exc_info()
    rec = record("call failed: %s", f"Bearer {FAKE}", error=f"token={FAKE}")
    rec.exc_info = info
    redact_record(rec)
    formatted = logging.Formatter("%(message)s %(error)s").format(rec)
    assert FAKE not in formatted
    assert rec.getMessage() == f"call failed: Bearer {MASK}"
    assert MASK in str(rec.__dict__["error"])


def test_a_record_with_nothing_to_redact_keeps_its_arguments() -> None:
    args = ("10.0.0.1:5000", "POST", "/v1/runtime/observe", "1.1", 200)
    rec = record('%s - "%s %s HTTP/%s" %d', *args)
    redact_record(rec)
    assert rec.args == args
    assert rec.getMessage() == '10.0.0.1:5000 - "POST /v1/runtime/observe HTTP/1.1" 200'


def test_a_record_with_bad_arguments_is_still_redacted() -> None:
    rec = record("%d items", f"password={FAKE}")
    redact_record(rec)
    assert FAKE not in rec.getMessage()


@pytest.fixture
def restored() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    factory, handlers = logging.getLogRecordFactory(), list(root.handlers)
    filters = {h: list(h.filters) for h in handlers}
    yield root
    logging.setLogRecordFactory(factory)
    root.handlers[:] = handlers
    for h, f in filters.items():
        h.filters[:] = f


def test_install_redacts_every_record_and_is_idempotent(restored: logging.Logger) -> None:
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(self.format(record))

    handler = Collect()
    handler.setFormatter(logging.Formatter("%(message)s %(secret)s"))
    restored.addHandler(handler)
    redaction.install()
    redaction.install()
    assert [type(f) for f in handler.filters] == [RedactingFilter]
    logging.getLogger("ssc.test").warning(
        "upload %s", f"Bearer {FAKE}", extra={"secret": f"sk_live_{FAKE}"}
    )
    assert seen == [f"upload Bearer {MASK} {MASK}"]

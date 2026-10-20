"""SSC-026: the redaction filter for logs and error text, by pattern. Every credential here is
assembled at run time and fake."""

import logging
import time
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
    "sb_" + "secret_" + FAKE,
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
        (f"Cookie: session={FAKE}; theme=dark", f"Cookie: {MASK}"),
        (f"set-cookie: sid={FAKE}; HttpOnly", f"set-cookie: {MASK}"),
        (f"Authorization: Token {FAKE}", f"Authorization: Token {MASK}"),
        (f"authorization: AWS4-HMAC-SHA256 {FAKE}", f"authorization: AWS4-HMAC-SHA256 {MASK}"),
        (f"GET /o?X-Goog-Signature={FAKE}&a=1", f"GET /o?X-Goog-Signature={MASK}&a=1"),
        (f"GET /o?a=1&X-Amz-Signature={FAKE}", f"GET /o?a=1&X-Amz-Signature={MASK}"),
        (f"GET /o?Signature={FAKE} 200", f"GET /o?Signature={MASK} 200"),
        (f"GET /o?x=1&sig={FAKE}", f"GET /o?x=1&sig={MASK}"),
        (f"x-goog-signature: {FAKE}", f"x-goog-signature: {MASK}"),
        (f"X-Amz-Signature: {FAKE}", f"X-Amz-Signature: {MASK}"),
        (f"Signature: {FAKE}", f"Signature: {MASK}"),
        (f"glpat-{FAKE}", MASK),
        ("npm_" + "a1B2" * 9, MASK),
        (f"posting to https://hooks.slack.com/services/T0/B0/{FAKE}", f"posting to {MASK}"),
        (f"postgres://ssc:p@ss:{FAKE}@db/ssc", f"postgres://ssc:{MASK}@db/ssc"),
        (f'password: "two words {FAKE}"', f'password: "{MASK}"'),
        (f"secret='with space {FAKE}'", f"secret='{MASK}'"),
        (f'{{"token": "a b c {FAKE}"}}', f'{{"token": "{MASK}"}}'),
    ],
)
def test_a_secret_in_context_is_masked(text: str, masked: str) -> None:
    assert redact(text) == masked


@pytest.mark.parametrize(
    "text",
    [
        "the cookie banner was dismissed",
        "cookies: 3 set",
        "authorization failed for usr_abc",
        "Authorization header missing",
        "GET /search?signed=1&q=sigma&design=2",
        "signature: ok",
        "glpat-short",
        "npm_install finished",
        "see https://hooks.slack.com/ for webhooks",
        "https://example.com:8080/a@b",
        'password: "abc"',
        "mailto:ana@example.com",
    ],
)
def test_a_lookalike_is_left_alone(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "line",
    ["token" * 20000, "a" * 100000, "Cookie: " + "a=b; " * 20000],
    ids=["token", "a", "cookie"],
)
def test_a_long_line_is_redacted_quickly(line: str) -> None:
    started = time.perf_counter()
    redact(line)
    assert time.perf_counter() - started < 0.05


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


def test_an_access_log_record_keeps_its_arguments_when_one_is_redacted() -> None:
    from uvicorn.logging import AccessFormatter  # noqa: PLC0415

    rec = record('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/x?token=abcdefgh", "1.1", 200)
    redact_record(rec)
    assert rec.args == ("1.2.3.4", "GET", f"/x?token={MASK}", "1.1", 200)
    line = AccessFormatter().format(rec)
    assert MASK in line
    assert "abcdefgh" not in line


def test_a_record_with_named_arguments_keeps_them() -> None:
    named = {"who": "ana", "what": f"Bearer {FAKE}"}
    rec = logging.LogRecord(
        "ssc", logging.INFO, __file__, 1, "%(who)s sent %(what)s", (named,), None
    )
    redact_record(rec)
    assert rec.args == {"who": "ana", "what": f"Bearer {MASK}"}
    assert rec.getMessage() == f"ana sent Bearer {MASK}"


def test_a_secret_split_between_message_and_argument_is_still_redacted() -> None:
    rec = record("password=%s", FAKE)
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


def test_installed_says_whether_install_ran(restored: logging.Logger) -> None:
    logging.setLogRecordFactory(logging.LogRecord)
    assert not redaction.installed()
    redaction.install()
    assert redaction.installed()


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

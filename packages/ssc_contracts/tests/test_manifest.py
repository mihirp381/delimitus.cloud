"""``ssc.toml`` ``ssc/v1``: refusals carry a line, defaults, rules, round trips and the digest."""

import random
import tomllib
import zoneinfo
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cronsim import CronSim, CronSimError
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from ssc_contracts.manifest import (
    KV_FIX_IT,
    MAX_MANIFEST_BYTES,
    RESOURCE_CLASSES,
    SCHEMA_V1,
    SESSION_FRAMEWORKS,
    SQLITE_FIX_IT,
    Manifest,
    ManifestError,
    ManifestProblem,
    Runtime,
    Schedule,
    _Positions,
    default_manifest,
    dump_manifest,
    is_session_app,
    load_manifest,
    max_instances,
    session_framework,
    uses_files,
)
from ssc_shared.canonical import manifest_digest

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
PROPERTY = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def refusal(text: str | bytes) -> ManifestError:
    with pytest.raises(ManifestError) as err:
        load_manifest(text)
    return err.value


# --- an invalid manifest is refused with a line number -----------------------------------

LINE_CASES = {
    "toml syntax error": (
        'schema = "ssc/v1"\n\n[runtime]\nport = 8080\nhealth_path = "/healthz\n',
        5,
        "syntax",
    ),
    "unknown key": (
        'schema = "ssc/v1"\n\n[runtime]\nport = 8080\nhelth_path = "/healthz"\n',
        5,
        "runtime.helth_path",
    ),
    "wrong type on runtime.port": (
        'schema = "ssc/v1"\n# app runtime\n\n[runtime]\nclass = "small"\n'
        'health_path = "/"\nport = "8080"\n',
        7,
        "runtime.port",
    ),
    "missing schema": ("[runtime]\nport = 8080\n", 1, "schema"),
    "bad cron in the second schedule": (
        'schema = "ssc/v1"\n\n[[schedules]]\nname = "nightly"\ncron = "0 3 * * *"\n'
        'path = "/tasks/nightly"\n\n[[schedules]]\nname = "hourly"\ncron = "0 * * *"\n'
        'path = "/tasks/hourly"\n',
        10,
        "schedules[1].cron",
    ),
    "duplicate connection name": (
        'schema = "ssc/v1"\n\n[connections]\nnames = [\n  "warehouse",\n  "crm",\n'
        '  "warehouse",\n]\n',
        7,
        "connections.names[2]",
    ),
    "public_env name without a public prefix": (
        'schema = "ssc/v1"\n\n[build.public_env.prod]\nVITE_API_URL = "https://api.example.com"\n'
        'API_URL = "https://api.example.com"\n',
        5,
        "build.public_env.prod.API_URL",
    ),
}


@pytest.mark.parametrize(("text", "line", "field"), LINE_CASES.values(), ids=LINE_CASES.keys())
def test_invalid_manifest_is_refused_with_a_line_number(text: str, line: int, field: str) -> None:
    err = refusal(text)
    assert (err.line, err.field) == (line, field)
    assert str(err).startswith(f"ssc.toml:{line}:{err.column}: {field}: ")


def test_refusal_messages_are_plain() -> None:
    assert refusal(LINE_CASES["unknown key"][0]).message == (
        "unknown key; did you mean 'health_path'?"
    )
    assert refusal(LINE_CASES["wrong type on runtime.port"][0]).message == "must be a whole number"
    assert refusal(LINE_CASES["missing schema"][0]).message == (
        'required; start ssc.toml with schema = "ssc/v1"'
    )
    assert "listed twice" in refusal(LINE_CASES["duplicate connection name"][0]).message
    assert (
        "build.public_names"
        in refusal(LINE_CASES["public_env name without a public prefix"][0]).message
    )


def test_every_problem_is_listed_in_file_order() -> None:
    err = refusal(
        'schema = "ssc/v1"\n[egress]\nhosts = ["ok.example.com", "10.0.0.1"]\n'
        '[runtime]\nport = 0\nclass = "huge"\n'
    )
    assert [(p.line, p.field) for p in err.problems] == [
        (3, "egress.hosts[1]"),
        (5, "runtime.port"),
        (6, "runtime.class"),
    ]
    assert str(err).splitlines()[0] == str(err.problems[0])
    assert str(ManifestProblem(2, 3, "a.b", "c")) == "ssc.toml:2:3: a.b: c"


def test_positions_survive_strings_comments_quoting_and_inline_tables() -> None:
    err = refusal(
        'schema = "ssc/v1"   # [connections] in a comment\n'
        "\n"
        "[runtime]\n"
        "start = '''\n"
        "[connections]\n"
        "names = 1\n"
        "'''\n"
        '"port" = "x"  # quoted key\n'
        "[egress]\n"
        'hosts = ["a.example.com", """b.example.com""",\n'
        "  'Bad.example.com']\n"
    )
    assert [(p.line, p.column, p.field) for p in err.problems] == [
        (4, 1, "runtime.start"),
        (8, 1, "runtime.port"),
        (11, 3, "egress.hosts[2]"),
    ]
    err = refusal('schema = "ssc/v1"\nruntime = { class = "small", port = 0 }\n')
    assert (err.line, err.column, err.field) == (2, 30, "runtime.port")
    err = refusal(
        'schema = "ssc/v1"\nschedules = [\n  { name = "a", cron = "x", path = "/" },\n]\n'
    )
    assert (err.line, err.column, err.field) == (3, 17, "schedules[0].cron")


def test_crlf_bom_and_encoding() -> None:
    err = refusal(b'\xef\xbb\xbfschema = "ssc/v1"\r\n[runtime]\r\nport = "x"\r\n')
    assert (err.line, err.column) == (3, 1)
    assert load_manifest(b'\xef\xbb\xbfschema = "ssc/v1"\r\n') == default_manifest()
    err = refusal(b'schema = "ssc/v1"\n# caf\xe9\n')
    assert (err.line, err.column, err.field, err.message) == (2, 6, "(file)", "not UTF-8 text")
    err = refusal('schema = "ssc/v1"\n# ' + chr(0xD800) + "\n")
    assert (err.line, err.field) == (2, "(file)")
    err = refusal('schema = "ssc/v1"\n#' + "x" * MAX_MANIFEST_BYTES)
    assert err.message == "larger than 256 KiB"


def test_other_schema_versions_are_named_not_guessed() -> None:
    err = refusal('# future\nschema = "ssc/v2"\n[runtime]\nfuture_key = 1\n')
    assert [(p.line, p.field) for p in err.problems] == [(2, "schema")]
    assert err.message == '"ssc/v2" is not a schema this version reads; it reads "ssc/v1"'
    assert refusal("schema = 1\n").field == "schema"


def test_a_key_value_store_gets_the_fix_it() -> None:
    for text in (
        'schema = "ssc/v1"\n[state]\npostgres = true\nkv = true\n',
        'schema = "ssc/v1"\n[state]\nRedis = { url = "x" }\n',
    ):
        err = refusal(text)
        assert (err.line, err.message) == (4 if "kv" in text else 3, KV_FIX_IT)
        assert "STATE_KV_UNSUPPORTED" in err.message
    err = refusal('schema = "ssc/v1"\nstate = "redis"\n')
    assert (err.line, err.field) == (2, "state")
    assert KV_FIX_IT in err.message


def test_sqlite_on_disk_gets_the_fix_it() -> None:
    for text, line, field in (
        ('schema = "ssc/v1"\n[state]\npostgres = false\nsqlite = true\n', 4, "state.sqlite"),
        ('schema = "ssc/v1"\n[state]\nSQLite3 = { path = "app.db" }\n', 3, "state.SQLite3"),
    ):
        err = refusal(text)
        assert (err.line, err.field, err.message) == (line, field, SQLITE_FIX_IT)
        assert "STATE_SQLITE_EPHEMERAL" in err.message and "postgres = true" in err.message
    err = refusal('schema = "ssc/v1"\nstate = "sqlite"\n')
    assert (err.line, err.field) == (2, "state")
    assert SQLITE_FIX_IT in err.message


@pytest.mark.parametrize(
    "text",
    [
        'schema = "ssc/v1"\n[runtime]\nbilling = "instance"\n',
        'schema = "ssc/v1"\n[runtime]\nbilling = "request"\n',
        'schema = "ssc/v1"\n[runtime]\nbilling = "gpu"\n',
        'schema = "ssc/v1"\n[runtime]\nclass = "small"\nbilling = true\n',
    ],
)
def test_billing_is_not_a_manifest_key(text: str) -> None:
    err = refusal(text)
    assert [(p.field, p.message) for p in err.problems] == [("runtime.billing", "unknown key")]
    assert err.line == text.count("\n")
    top = refusal('schema = "ssc/v1"\nbilling = "instance"\n')
    assert (top.line, top.field, top.message) == (2, "billing", "unknown key")


# --- defaults and platform policy --------------------------------------------------------


def test_defaults() -> None:
    m = load_manifest('schema = "ssc/v1"\n')
    assert m == default_manifest()
    assert (m.runtime.class_, m.runtime.port, m.runtime.health_path) == ("small", 8080, "/")
    assert (m.runtime.start, m.runtime.sessions, m.state.postgres) == (None, False, False)
    assert m.build.public_names == () and m.build.public_env == {}
    assert m.connections.names == () and m.egress.hosts == () and m.schedules == ()
    s = load_manifest(
        'schema = "ssc/v1"\n[[schedules]]\nname = "a"\ncron = "0 3 * * *"\npath = "/a"\n'
    ).schedules[0]
    assert (s.timezone, s.method, s.timeout_seconds) == ("UTC", "POST", 60)


def test_resource_classes_and_the_session_cap() -> None:
    assert {n: (c.vcpu, c.memory_mib, c.max_instances) for n, c in RESOURCE_CLASSES.items()} == {
        "small": (1, 512, 2),
        "medium": (1, 2048, 4),
        "large": (2, 4096, 8),
    }
    assert max_instances(Runtime.model_validate({"class": "large"})) == 8
    assert max_instances(Runtime.model_validate({"class": "large", "sessions": True})) == 1
    assert max_instances(Runtime.model_validate({"class": "large", "start": "gradio app.py"})) == 1
    assert max_instances(Runtime.model_validate({"class": "medium"}), framework="Dash") == 1
    assert max_instances(Runtime.model_validate({"class": "medium"}), framework="flask") == 4


# --- session apps ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "framework"),
    [
        (None, None),
        ("streamlit run app.py --server.port $PORT --server.address 0.0.0.0", "streamlit"),
        ("uv run /opt/venv/bin/streamlit run app.py", "streamlit"),
        ("python -m streamlit run app.py", "streamlit"),
        ("gradio app.py", "gradio"),
        ("python -m gradio app.py", "gradio"),
        ("shiny run app.py --host 0.0.0.0 --port $PORT", "shiny"),
        ("uv run shiny run --port 8080 app.py", "shiny"),
        ("Rscript -e \"shiny::runApp('.', host='0.0.0.0', port=8080)\"", "shiny"),
        ("sh -c 'streamlit run app.py'", "streamlit"),
        ("python app.py", None),
        ("gunicorn app:server", None),
        ("dash -c 'node server.js'", None),
        ("python streamlit_app.py", None),
        ("python -m uvicorn app:app", None),
        ("npm start", None),
    ],
)
def test_session_framework_is_detected_from_the_start_command(
    start: str | None, framework: str | None
) -> None:
    assert session_framework(start) == framework
    runtime = Runtime.model_validate({} if start is None else {"start": start})
    assert is_session_app(runtime) == (framework is not None)


def test_the_session_framework_list() -> None:
    assert SESSION_FRAMEWORKS == {"streamlit", "gradio", "dash", "shiny"}


def test_a_session_app_is_sessions_true_a_detected_framework_or_a_start_command() -> None:
    plain = Runtime.model_validate({"start": "python app.py"})
    assert not is_session_app(plain)
    assert is_session_app(Runtime.model_validate({"sessions": True}))
    for name in ("streamlit", "Gradio", "dash", "SHINY"):
        assert is_session_app(plain, framework=name)
    assert not is_session_app(plain, framework="fastapi")


def test_detection_never_changes_the_manifest_or_its_digest() -> None:
    text = 'schema = "ssc/v1"\n[runtime]\nstart = "streamlit run app.py --server.port $PORT"\n'
    m = load_manifest(text)
    assert is_session_app(m.runtime) and m.runtime.sessions is False
    assert m.model_dump(mode="json", by_alias=True)["runtime"]["sessions"] is False
    assert "sessions = false" in dump_manifest(m)
    assert manifest_digest(m) == PINNED_DIGESTS["streamlit start"][1]
    declared = load_manifest(text.replace("[runtime]\n", "[runtime]\nsessions = true\n"))
    assert manifest_digest(declared) != manifest_digest(m)


def test_the_model_is_frozen_and_strict() -> None:
    m = default_manifest()
    with pytest.raises(ValueError):
        m.runtime.port = 1  # type: ignore[misc]
    assert refusal('schema = "ssc/v1"\n[runtime]\nport = 80.0\n').field == "runtime.port"
    assert refusal('schema = "ssc/v1"\n[runtime]\nsessions = "yes"\n').message == (
        "must be true or false"
    )
    assert refusal('schema = "ssc/v1"\nruntime = 1\n').message == "must be a table"
    assert refusal('schema = "ssc/v1"\n[connections]\nnames = "a"\n').message == "must be an array"


# --- field rules -------------------------------------------------------------------------


def field_error(section: str, body: str) -> str | None:
    try:
        load_manifest(f'schema = "ssc/v1"\n[{section}]\n{body}\n')
    except ManifestError as err:
        return err.message
    return None


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('health_path = "/healthz"', None),
        ('health_path = "/api/v1/health;x=1"', None),
        ('health_path = "healthz"', "single /"),
        ('health_path = "//evil.example.com"', "single /"),
        ('health_path = "/h?x=1"', "without ?query"),
        ('health_path = "/h#top"', "not allowed"),
        ('health_path = "/h x"', "not allowed"),
        ('health_path = "/' + "a" * 256 + '"', "at most 256"),
        ('start = "streamlit run app.py --server.port $PORT"', None),
        ('start = "a\\nb"', "one line"),
        ('start = "   "', "not be empty"),
        ("port = 65536", "less than or equal to 65535"),
        ('class = "xl"', "'small', 'medium' or 'large'"),
    ],
)
def test_runtime_rules(body: str, fragment: str | None) -> None:
    message = field_error("runtime", body)
    assert message is None if fragment is None else fragment in (message or "")


@pytest.mark.parametrize(
    ("host", "fragment"),
    [
        ("api.example.com", None),
        ("a-b.c.example.co", None),
        ("https://api.example.com", "without a scheme"),
        ("api.example.com/v1", "without a scheme"),
        ("api.example.com:443", "without a port"),
        ("*.example.com", "wildcards"),
        ("10.0.0.1", "IP addresses"),
        ("[::1]", "IP addresses"),
        ("2001:db8::1", "IP addresses"),
        ("API.example.com", "lower case"),
        ("localhost", "DNS host name"),
        ("-a.example.com", "DNS host name"),
        ("a_b.example.com", "DNS host name"),
        ("example.com.", "DNS host name"),
        ("a" * 64 + ".com", "DNS host name"),
    ],
)
def test_egress_host_rules(host: str, fragment: str | None) -> None:
    message = field_error("egress", f'hosts = ["{host}"]')
    assert message is None if fragment is None else fragment in (message or "")


def test_sets_are_unique_bounded_and_sorted() -> None:
    m = load_manifest(
        'schema = "ssc/v1"\n[connections]\nnames = ["warehouse", "crm"]\n'
        '[egress]\nhosts = ["b.example.com", "a.example.com"]\n'
    )
    assert m.connections.names == ("crm", "warehouse")
    assert m.egress.hosts == ("a.example.com", "b.example.com")
    many = ", ".join(f'"h{i}.example.com"' for i in range(51))
    assert field_error("egress", f"hosts = [{many}]") == "at most 50 entries"
    assert "listed twice" in (field_error("egress", 'hosts = ["a.io", "a.io"]') or "")
    assert "lowercase letter" in (field_error("connections", 'names = ["Warehouse"]') or "")


@pytest.mark.parametrize(
    ("section", "body", "fragment"),
    [
        ("build.public_env.prod", 'VITE_API = "x"', None),
        ("build.public_env.preview", 'NEXT_PUBLIC_API = "x"', None),
        ("build.public_env.prod", 'VITE_ = "x"', "is not public"),
        ("build.public_env.prod", 'REACT_APP_API = "x"', "is not public"),
        ("build.public_env.prod", 'VITE_STRIPE_SECRET = "x"', "looks like a secret"),
        ("build.public_env.prod", 'NEXT_PUBLIC_SUPABASE_SERVICE_ROLE = "x"', "looks like a secret"),
        ("build.public_env.prod", 'vite_api = "x"', "upper-case name"),
        ("build.public_env.prod", 'VITE_A = "' + "x" * 4097 + '"', "at most 4096"),
        ("build.public_env.staging", 'VITE_A = "x"', "'prod' or 'preview'"),
        ("build", 'public_names = ["PORT"]', "set by the platform"),
        ("build", 'public_names = ["SSC_ORG"]', "set by the platform"),
        ("build", 'public_names = ["RAILPACK_DEPLOY_APT_PACKAGES"]', "set by the platform"),
        ("build", 'public_names = ["DATABASE_URL"]', "set by the platform"),
        ("build", 'public_names = ["DB_PASSWORD"]', "looks like a secret"),
    ],
)
def test_public_build_value_rules(section: str, body: str, fragment: str | None) -> None:
    message = field_error(section, body)
    assert message is None if fragment is None else fragment in (message or "")


def test_listed_public_names() -> None:
    m = load_manifest(
        'schema = "ssc/v1"\n[build]\npublic_names = ["REACT_APP_API", "API_URL"]\n'
        '[build.public_env.prod]\nREACT_APP_API = "https://api.example.com"\nVITE_X = "1"\n'
        '[build.public_env.preview]\nAPI_URL = "https://preview.example.com"\n'
    )
    assert m.build.public_names == ("API_URL", "REACT_APP_API")
    assert m.build.public_env["prod"] == {"REACT_APP_API": "https://api.example.com", "VITE_X": "1"}


def schedule_error(**fields: object) -> str | None:
    data: dict[str, object] = {"name": "job", "cron": "0 3 * * *", "path": "/tasks/job"} | fields
    try:
        Schedule.model_validate(data)
    except ValueError as err:
        return str(err)
    return None


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"path": "/tasks/job?full=1&x=%20"}, None),
        ({"path": "tasks"}, "single /"),
        ({"path": "/" + "a" * 512}, "at most 512"),
        ({"path": "/a#b"}, "not allowed"),
        ({"timezone": "Europe/London"}, None),
        ({"timezone": "America/Argentina/Buenos_Aires"}, None),
        ({"timezone": "Mars/Olympus"}, "not a known IANA time zone"),
        ({"timezone": "../etc/passwd"}, "IANA zone"),
        ({"timezone": "utc"}, "IANA zone"),
        ({"method": "GET"}, None),
        ({"method": "DELETE"}, "'GET' or 'POST'"),
        ({"timeout_seconds": 900}, None),
        ({"timeout_seconds": 901}, "less than or equal to 900"),
        ({"timeout_seconds": 0}, "greater than or equal to 1"),
        ({"name": "Job"}, "lowercase letter"),
    ],
)
def test_schedule_rules(fields: dict[str, object], fragment: str | None) -> None:
    message = schedule_error(**fields)
    assert message is None if fragment is None else fragment in (message or "")


def test_utc_needs_no_time_zone_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(key: str) -> None:
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
    assert schedule_error(timezone="UTC") is None
    assert "not a known IANA time zone" in (schedule_error(timezone="Europe/London") or "")


def test_schedule_names_are_unique_and_sorted() -> None:
    body = 'schema = "ssc/v1"\n' + "".join(
        f'[[schedules]]\nname = "{n}"\ncron = "0 3 * * *"\npath = "/"\n' for n in ("b", "a")
    )
    assert [s.name for s in load_manifest(body).schedules] == ["a", "b"]
    err = refusal(body + '[[schedules]]\nname = "b"\ncron = "0 3 * * *"\npath = "/"\n')
    assert (err.line, err.field) == (11, "schedules[2].name")


# --- cron --------------------------------------------------------------------------------

CRON_OK = [
    ("0 3 * * *", "0 3 * * *"),
    ("*/5 * * * *", "*/5 * * * *"),
    ("0 9-17 * * mon-fri", "0 9-17 * * MON-FRI"),
    ("0\t0  1,15 jan,jul *", "0 0 1,15 JAN,JUL *"),
    ("30 2 29 2 *", "30 2 29 2 *"),
    ("0 0 * * 7", "0 0 * * 7"),
    ("5/15 0 1-31/2 */3 SUN", "5/15 0 1-31/2 */3 SUN"),
]
CRON_BAD = [
    ("0 * * *", "exactly five"),
    ("0 0 * * * *", "exactly five"),
    ("60 * * * *", "minute"),
    ("* 24 * * *", "hour"),
    ("* * 0 * *", "day-of-month"),
    ("* * * 13 *", "month"),
    ("* * * * 8", "day-of-week"),
    ("*/0 * * * *", "step"),
    ("5-1 * * * *", "backwards"),
    ("0 0 30 2 *", "never occurs"),
    ("0 0 31 4,6,9,11 *", "never occurs"),
    ("0 0 L * *", "outside"),
    ("0 0 * * 5#2", "five cron fields"),
    ("@daily", "five cron fields"),
    ("0 0 * * MON-SUN", "backwards"),
    ("0 0 * JAN-DEC/0 *", "step"),
]


@pytest.mark.parametrize(("cron", "normalised"), CRON_OK)
def test_cron_accepted_and_normalised(cron: str, normalised: str) -> None:
    assert Schedule(name="a", cron=cron, path="/").cron == normalised
    CronSim(cron, T0)


@pytest.mark.parametrize(("cron", "fragment"), CRON_BAD)
def test_cron_refused(cron: str, fragment: str) -> None:
    assert fragment in (schedule_error(cron=cron) or "")


FIELD_RANGES = [
    (0, 59, ()),
    (0, 23, ()),
    (1, 31, ()),
    (1, 12, ("JAN", "jun", "DEC")),
    (0, 7, ("SUN", "mon", "SAT")),
]


def cron_field(low: int, high: int, names: tuple[str, ...]) -> st.SearchStrategy[str]:
    value = st.one_of(st.integers(low, high).map(str), *([st.sampled_from(names)] if names else []))
    term = st.one_of(
        st.just("*"),
        value,
        st.tuples(value, value).map("-".join),
    )
    stepped = st.tuples(term, st.one_of(st.none(), st.integers(1, high).map(str))).map(
        lambda t: t[0] if t[1] is None else f"{t[0]}/{t[1]}"
    )
    return st.lists(stepped, min_size=1, max_size=3).map(",".join)


VALID_ISH_CRON = st.tuples(*(cron_field(*r) for r in FIELD_RANGES)).map(" ".join)


@PROPERTY
@given(VALID_ISH_CRON)
def test_every_cron_we_accept_cronsim_accepts_with_the_same_times(cron: str) -> None:
    try:
        normalised = Schedule(name="a", cron=cron, path="/").cron
    except ValueError:
        assume(False)
        return
    ours, theirs = CronSim(normalised, T0), CronSim(cron, T0)
    assert [next(ours) for _ in range(3)] == [next(theirs) for _ in range(3)]


@PROPERTY
@given(st.text(alphabet="0123456789*/,- JANFEBSUNMOLW", max_size=24))
def test_arbitrary_cron_text_never_passes_us_and_fails_cronsim(cron: str) -> None:
    try:
        Schedule(name="a", cron=cron, path="/")
    except ValueError:
        return
    try:
        next(CronSim(cron, T0))
    except CronSimError:
        pytest.fail(f"accepted {cron!r} that cronsim refuses")


# --- round trip and digest ---------------------------------------------------------------

NAME = st.from_regex(r"[a-z][a-z0-9-]{0,8}", fullmatch=True)
LABEL = st.from_regex(r"[a-z]([a-z0-9-]{0,6}[a-z0-9])?", fullmatch=True)
HOST = st.lists(LABEL, min_size=2, max_size=3).map(".".join).filter(lambda h: len(h) <= 253)
SECRETISH = ("SECRET", "PASSWORD", "PASSWD", "SERVICE_ROLE", "PRIVATE_KEY")
PUBLIC_SUFFIX = st.from_regex(r"[A-Z0-9_]{1,8}", fullmatch=True)
PREFIXED = st.tuples(st.sampled_from(["VITE_", "NEXT_PUBLIC_"]), PUBLIC_SUFFIX).map("".join)
LISTED = st.from_regex(r"[A-Z][A-Z0-9_]{0,8}", fullmatch=True).filter(
    lambda n: (
        n not in {"PORT", "HOME", "PATH", "DATABASE_URL"}
        and not n.startswith(("SSC_", "RAILPACK_"))
    )
)
TEXT = st.text(st.characters(exclude_categories=["Cs"], exclude_characters="\x00"), max_size=12)
LINE = TEXT.map(lambda t: "run " + "".join(c for c in t if c not in "\r\n"))
PATH = st.from_regex(r"/[a-z0-9_./-]{0,12}", fullmatch=True).filter(
    lambda p: not p.startswith("//")
)
CRON = st.tuples(
    st.sampled_from(["*", "0", "*/5", "0,30", "10-20"]),
    st.sampled_from(["*", "3", "9-17", "*/2"]),
    st.sampled_from(["*", "1", "1-15", "*/10"]),
    st.sampled_from(["*", "1", "jan-jun", "*/3", "DEC"]),
    st.sampled_from(["*", "mon-fri", "0", "sun,sat", "1-5"]),
).map(" ".join)
ZONE = st.sampled_from(["UTC", "Europe/London", "Asia/Kolkata", "America/Argentina/Buenos_Aires"])


def no_secret(name: str) -> bool:
    return not any(word in name for word in SECRETISH)


@st.composite
def manifest_data(draw: st.DrawFn) -> dict[str, Any]:
    """A valid ``ssc.toml`` as TOML data, with optional keys sometimes left to defaults."""
    listed = draw(st.lists(LISTED.filter(no_secret), max_size=3, unique=True))
    names = st.one_of(PREFIXED, st.sampled_from(listed) if listed else PREFIXED).filter(no_secret)
    env_values = st.dictionaries(names, TEXT, max_size=3)
    data: dict[str, Any] = {"schema": SCHEMA_V1}
    optional: dict[str, Any] = {
        "runtime": st.fixed_dictionaries(
            {},
            optional={
                "class": st.sampled_from(["small", "medium", "large"]),
                "port": st.integers(1, 65535),
                "health_path": PATH,
                "start": LINE,
                "sessions": st.booleans(),
            },
        ),
        "build": st.fixed_dictionaries(
            {},
            optional={
                "public_names": st.just(listed),
                "public_env": st.fixed_dictionaries(
                    {}, optional={"prod": env_values, "preview": env_values}
                ),
            },
        ),
        "state": st.fixed_dictionaries({}, optional={"postgres": st.booleans()}),
        "connections": st.fixed_dictionaries(
            {}, optional={"names": st.lists(NAME, max_size=4, unique=True)}
        ),
        "egress": st.fixed_dictionaries(
            {}, optional={"hosts": st.lists(HOST, max_size=4, unique=True)}
        ),
        "files": st.fixed_dictionaries({"enabled": st.booleans()}),
        "schedules": st.lists(
            st.fixed_dictionaries(
                {"name": NAME, "cron": CRON, "path": PATH},
                optional={
                    "timezone": ZONE,
                    "method": st.sampled_from(["GET", "POST"]),
                    "timeout_seconds": st.integers(1, 900),
                },
            ),
            max_size=3,
            unique_by=lambda s: s["name"],
        ),
    }
    for key, strategy in optional.items():
        if draw(st.booleans()):
            data[key] = draw(strategy)
    if data.get("build", {}).get("public_env") and "public_names" not in data["build"]:
        data["build"]["public_names"] = listed
    return data


def toml_string(value: str, rnd: random.Random) -> str:
    if rnd.random() < 0.4 and "'" not in value and value.isprintable():
        return f"'{value}'"
    out = []
    for c in value:
        if c in '"\\':
            out.append("\\" + c)
        elif ord(c) < 0x20 or c == "\x7f":
            out.append(f"\\U{ord(c):08x}" if rnd.random() < 0.5 else f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def toml_value(value: object, rnd: random.Random) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return hex(value) if rnd.random() < 0.3 else str(value)
    if isinstance(value, str):
        return toml_string(value, rnd)
    if isinstance(value, list):
        items = list(value)
        rnd.shuffle(items)
        sep = ",\n  " if rnd.random() < 0.5 else ", "
        return "[" + sep.join(toml_value(v, rnd) for v in items) + "]"
    if isinstance(value, dict):
        return inline(value, rnd)
    raise TypeError(value)


def key(name: str, rnd: random.Random) -> str:
    return f'"{name}"' if rnd.random() < 0.2 else name


def inline(table: dict[str, Any], rnd: random.Random) -> str:
    pairs = [f"{key(k, rnd)} = {toml_value(v, rnd)}" for k, v in shuffled(table, rnd)]
    return "{ " + ", ".join(pairs) + " }" if pairs else "{}"


def shuffled(table: dict[str, Any], rnd: random.Random) -> list[tuple[str, Any]]:
    items = list(table.items())
    rnd.shuffle(items)
    return items


def render(data: dict[str, Any], seed: int) -> str:
    """The same data as TOML in a random style: key order, inline or header tables, quoting."""
    rnd = random.Random(seed)
    top: list[str] = [f"schema = {toml_string(data['schema'], rnd)}"]
    blocks: list[str] = []
    for name, value in shuffled({k: v for k, v in data.items() if k != "schema"}, rnd):
        style = rnd.choice(["header", "inline", "dotted"])
        if name == "schedules":
            items = list(value)
            rnd.shuffle(items)
            if style == "inline" or not items:
                top.append(f"schedules = {toml_value(items, rnd)}")
            else:
                blocks += ["[[schedules]]\n" + pairs(s, rnd) for s in items]
        elif style == "inline":
            top.append(f"{name} = {inline(value, rnd)}")
        elif style == "dotted":
            top += [f"{name}.{key(k, rnd)} = {toml_value(v, rnd)}" for k, v in shuffled(value, rnd)]
        else:
            blocks.append(f"[{name}]\n" + pairs(value, rnd))
    rnd.shuffle(top)
    return "\n".join(top) + "\n\n" + "\n\n".join(blocks) + "\n"


def pairs(table: dict[str, Any], rnd: random.Random) -> str:
    return "".join(
        f"{key(k, rnd)} = {toml_value(v, rnd)}  # note\n" for k, v in shuffled(table, rnd)
    )


@PROPERTY
@given(manifest_data())
def test_load_dump_load_round_trip(data: dict[str, Any]) -> None:
    m = Manifest.model_validate(data)
    assert load_manifest(dump_manifest(m)) == m
    assert dump_manifest(load_manifest(dump_manifest(m))) == dump_manifest(m)


@PROPERTY
@given(manifest_data(), st.integers(0, 2**32), st.integers(0, 2**32))
def test_digest_is_stable_under_key_and_table_reordering(
    data: dict[str, Any], seed_a: int, seed_b: int
) -> None:
    a, b = load_manifest(render(data, seed_a)), load_manifest(render(data, seed_b))
    assert a == b == Manifest.model_validate(data)
    assert manifest_digest(a) == manifest_digest(b)


@PROPERTY
@given(manifest_data(), st.integers(0, 2**32))
def test_every_key_in_valid_text_has_a_position_on_its_line(
    data: dict[str, Any], seed: int
) -> None:
    text = render(data, seed)
    at = _Positions(text).at
    lines = text.split("\n")
    for path in leaf_paths(tomllib.loads(text)):
        assert path in at, path
        line, _ = at[path]
        if isinstance(path[-1], str):
            assert path[-1] in lines[line - 1], (path, lines[line - 1])


def leaf_paths(value: object, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    if isinstance(value, dict):
        out: list[tuple[str | int, ...]] = [path] if path and value else []
        for k, v in value.items():
            out += leaf_paths(v, (*path, k))
        return out
    if isinstance(value, list):
        return [path] + [p for i, v in enumerate(value) for p in leaf_paths(v, (*path, i))]
    return [path]


@PROPERTY
@given(manifest_data(), manifest_data())
def test_digests_differ_exactly_when_manifests_differ(a: dict[str, Any], b: dict[str, Any]) -> None:
    ma, mb = Manifest.model_validate(a), Manifest.model_validate(b)
    assert (manifest_digest(ma) == manifest_digest(mb)) == (ma == mb)


BASE: dict[str, Any] = {
    "schema": "ssc/v1",
    "runtime": {
        "class": "medium",
        "port": 3000,
        "health_path": "/healthz",
        "start": "npm start",
        "sessions": False,
    },
    "build": {"public_names": ["API_URL"], "public_env": {"prod": {"API_URL": "https://a.io"}}},
    "state": {"postgres": True},
    "connections": {"names": ["warehouse"]},
    "egress": {"hosts": ["api.stripe.com"]},
    "schedules": [
        {
            "name": "nightly",
            "cron": "0 3 * * *",
            "path": "/tasks/nightly",
            "timezone": "Europe/London",
            "method": "POST",
            "timeout_seconds": 60,
        }
    ],
}
CHANGES: dict[str, Callable[[dict[str, Any]], None]] = {
    "runtime.class": lambda d: d["runtime"].update({"class": "large"}),
    "runtime.port": lambda d: d["runtime"].update(port=3001),
    "runtime.health_path": lambda d: d["runtime"].update(health_path="/health"),
    "runtime.start": lambda d: d["runtime"].pop("start"),
    "runtime.sessions": lambda d: d["runtime"].update(sessions=True),
    "build.public_names": lambda d: d["build"]["public_names"].append("CDN_URL"),
    "build.public_env": lambda d: d["build"]["public_env"]["prod"].update(API_URL="https://b.io"),
    "state.postgres": lambda d: d["state"].update(postgres=False),
    "connections.names": lambda d: d["connections"]["names"].append("crm"),
    "egress.hosts": lambda d: d["egress"]["hosts"].append("api.github.com"),
    "schedules": lambda d: d["schedules"].pop(),
    "schedules.name": lambda d: d["schedules"][0].update(name="daily"),
    "schedules.cron": lambda d: d["schedules"][0].update(cron="0 4 * * *"),
    "schedules.path": lambda d: d["schedules"][0].update(path="/tasks/other"),
    "schedules.timezone": lambda d: d["schedules"][0].update(timezone="UTC"),
    "schedules.method": lambda d: d["schedules"][0].update(method="GET"),
    "schedules.timeout_seconds": lambda d: d["schedules"][0].update(timeout_seconds=61),
}


def fresh_base() -> dict[str, Any]:
    return Manifest.model_validate(BASE).model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize("field", CHANGES)
def test_the_digest_changes_when_any_value_changes(field: str) -> None:
    data = fresh_base()
    CHANGES[field](data)
    assert manifest_digest(Manifest.model_validate(data)) != manifest_digest(
        Manifest.model_validate(BASE)
    )


def test_every_field_has_a_digest_change_case() -> None:
    dumped = fresh_base()
    fields = {f"{t}.{k}" for t, v in dumped.items() if isinstance(v, dict) for k in v}
    fields |= {f"schedules.{k}" for k in dumped["schedules"][0]} | {"schedules"}
    assert fields == set(CHANGES)


def test_the_default_digest_is_frozen() -> None:
    assert manifest_digest(default_manifest()) == (
        "sha256:0ff2f05525bd8d69855565619833b59eb4945c2d58f949300bec78b9aac1fdf3"
    )


PINNED_DIGESTS: dict[str, tuple[str, str]] = {
    "schema only": (
        'schema = "ssc/v1"\n',
        "sha256:0ff2f05525bd8d69855565619833b59eb4945c2d58f949300bec78b9aac1fdf3",
    ),
    "streamlit start": (
        'schema = "ssc/v1"\n[runtime]\nstart = "streamlit run app.py --server.port $PORT"\n',
        "sha256:2f49b1bf5b204069ce74aeb12d8540d2ae4401c6825b3bf1c0457d7c526c200c",
    ),
    "gradio start": (
        'schema = "ssc/v1"\n[runtime]\nstart = "gradio app.py"\n',
        "sha256:2b36df62482674dbb26841bc6eddad4d430908f8d07d3d2c5d01061ec5f6b76d",
    ),
    "shiny start": (
        'schema = "ssc/v1"\n[runtime]\nclass = "medium"\nstart = "shiny run app.py --port 8080"\n',
        "sha256:e254211805c5150533d485e48b0bcbe91f4ca1ee4babf6405fbff645a0015869",
    ),
    "sessions declared": (
        'schema = "ssc/v1"\n[runtime]\nsessions = true\n',
        "sha256:b79d99e9e93c3ed6ad88471c958e55c9382c5d73ca2e1af2eb9f8defa42e1707",
    ),
}
DOCTOR_FIXTURES = Path(__file__).resolve().parents[3] / "packages/ssc_cli/tests/fixtures/doctor"


@pytest.mark.parametrize("name", PINNED_DIGESTS)
def test_accepted_manifests_keep_their_digests(name: str) -> None:
    text, digest = PINNED_DIGESTS[name]
    assert manifest_digest(load_manifest(text)) == digest


def test_the_base_manifest_keeps_its_digest() -> None:
    assert manifest_digest(Manifest.model_validate(BASE)) == (
        "sha256:da1899d4e00ab7e15fe2ba06ec14c9f3c546a387dfe42fcb5e25e6f5c7c41558"
    )


def test_the_doctor_fixture_manifests_keep_their_digests() -> None:
    files = sorted(DOCTOR_FIXTURES.glob("*/ssc.toml"))
    valid = [f for f in files if f.parent.name != "MANIFEST_INVALID"]
    assert len(valid) == len(files) - 1 >= 7
    want = PINNED_DIGESTS["schema only"][1]
    for path in valid:
        assert manifest_digest(load_manifest(path.read_bytes())) == want, path


def test_files_is_asked_for_by_its_table_alone() -> None:
    plain = load_manifest('schema = "ssc/v1"\n')
    asked = load_manifest('schema = "ssc/v1"\n[files]\n')
    off = load_manifest('schema = "ssc/v1"\n[files]\nenabled = false\n')
    assert plain.files is None and not uses_files(plain)
    assert asked.files is not None and asked.files.enabled and uses_files(asked)
    assert off.files is not None and not uses_files(off)
    assert "files" not in plain.model_dump(mode="json", by_alias=True)
    assert len({manifest_digest(m) for m in (plain, asked, off)}) == 3
    assert manifest_digest(plain) == PINNED_DIGESTS["schema only"][1]
    for m in (plain, asked, off):
        assert load_manifest(dump_manifest(m)) == m
    assert ("[files]" in dump_manifest(asked), "[files]" in dump_manifest(plain)) == (True, False)


def test_files_refusals_name_the_place() -> None:
    with pytest.raises(ManifestError) as typo:
        load_manifest('schema = "ssc/v1"\n[files]\nenabld = true\n')
    assert str(typo.value) == "ssc.toml:3:1: files.enabld: unknown key; did you mean 'enabled'?"
    with pytest.raises(ManifestError) as scalar:
        load_manifest('schema = "ssc/v1"\nfiles = true\n')
    assert str(scalar.value) == "ssc.toml:2:1: files: must be a table"

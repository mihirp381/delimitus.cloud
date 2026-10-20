"""SSC-014 secret rules. Secret-shaped values are assembled at runtime (gitleaks runs on PRs)."""

import base64
import json
import time

import pytest

from ssc_bundle.secrets import MAX_LINE_SCAN, MAX_SCAN_BYTES, Finding, entropy, mask, scan
from ssc_conformance.secret_shapes import SHAPES, Shape, line

AWS_KEY = "AKIA" + "Z7Q3K9XW" + "P2LMN4RT"
AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"
GENERIC = "q8ZrT2vN" + "x7LpW4mK" + "s9HdB3jF"
PASSWORD = "Sup3r" + "Secr3tPw"


def b64(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def jwt(role: str) -> str:
    header = b64({"alg": "HS256", "typ": "JWT"})
    return f"{header}.{b64({'iss': 'supabase', 'role': role})}." + "Xk2" * 12


def one(text: str, allowed: frozenset[str] = frozenset()) -> list[Finding]:
    return scan([("src/app.js", text.encode())], allowed)


def rules(findings: list[Finding]) -> list[tuple[str, bool]]:
    return [(f.rule, f.blocking) for f in findings]


def test_aws_access_key_is_found_and_masked() -> None:
    (f,) = one(f'const key = "{AWS_KEY}";\n')
    assert (f.path, f.line, f.rule, f.blocking) == ("src/app.js", 1, "aws_access_key", True)
    assert f.masked == "AKIA…RT"
    assert AWS_KEY not in f.masked


def test_aws_documentation_key_is_not_flagged() -> None:
    assert one("AKIA" + "IOSFODNN7" + "EXAMPLE") == []


def test_aws_secret_next_to_its_name_is_found() -> None:
    found = one(f"\n\naws_secret_access_key = {AWS_SECRET}\n")
    assert rules(found) == [("aws_access_key", True)]
    assert found[0].line == 3


def test_private_key_block_is_found() -> None:
    assert rules(one("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE...\n")) == [
        ("private_key_block", True)
    ]


def test_supabase_service_role_fails_and_anon_passes() -> None:
    assert rules(one(f'createClient(url, "{jwt("service_role")}")')) == [
        ("supabase_service_key", True)
    ]
    assert one(f'const token = "{jwt("anon")}"') == []
    assert rules(one("SUPABASE_KEY=sb_secret_" + "a1B2c3D4e5F6g7H8i9J0")) == [
        ("supabase_service_key", True)
    ]
    assert one('const apiKey = "sb_publishable_' + 'a1B2c3D4e5F6g7H8i9J0"') == []


@pytest.mark.parametrize(
    ("url", "blocking"),
    [
        ("postgres://app:" + PASSWORD + "@db.example.com:5432/app", True),
        ("mongodb+srv://app:" + PASSWORD + "@cluster0.abcde.mongodb.net/x", True),
        ("redis://:" + PASSWORD + "@cache.example.com:6379", True),
        ("postgresql://postgres:postgres@localhost:5432/dev", False),
        ("postgres://app:" + PASSWORD + "@db:5432/app", False),
        ("mysql://root:${DB_PASSWORD}@mysql.example.com/app", False),
    ],
)
def test_database_urls_with_passwords(url: str, blocking: bool) -> None:
    assert rules(one(f'DATABASE_URL = "{url}"')) == [("db_url_with_password", blocking)]


def test_a_database_url_without_password_is_fine() -> None:
    assert one("postgres://db.example.com/app") == []


def test_generic_secret_assignment() -> None:
    assert rules(one(f'API_TOKEN = "{GENERIC}"')) == [("generic_secret_assignment", True)]
    assert rules(one(f"password: {GENERIC}")) == [("generic_secret_assignment", True)]
    assert one('api_key = "' + "ab" * 20 + '"') == []
    assert one(f'secret_name = "{"x" * 30}"') == []
    assert one("token: process.env.GITHUB_TOKEN_FOR_CI_2024") == []
    assert one(f'label = "{GENERIC}"') == []


def test_declared_public_values_are_allowed() -> None:
    line = f'const mapKey = "{GENERIC}"; const apiKey = "{GENERIC}"'
    assert len(one(line)) == 1
    assert one(line, frozenset({GENERIC})) == []


def test_skipped_files() -> None:
    secret = f'API_TOKEN = "{GENERIC}"\n'.encode()
    files = [
        ("package-lock.json", secret),
        ("web/yarn.lock", secret),
        ("ssc.toml", secret),
        ("img.png", b"\x89PNG\0" + secret),
        ("big.js", secret + b" " * MAX_SCAN_BYTES),
        ("nested/ssc.toml", secret),
    ]
    assert [f.path for f in scan(files)] == ["nested/ssc.toml"]


def test_mask_never_shows_most_of_a_value() -> None:
    assert mask("abcdefghijklmnopqrstuvwxyz0123456789ABCD") == "abcd…CD"
    assert mask("abcdefgh") == "ab…h"
    assert mask("abc") == "…"


def test_entropy() -> None:
    assert entropy("aaaa") == 0
    assert entropy(GENERIC) >= 4.0


LONG = 1024 * 1024


def timed_scan(text: str) -> tuple[float, list[Finding]]:
    start = time.perf_counter()
    found = one(text)
    return time.perf_counter() - start, found


@pytest.mark.parametrize(
    "text",
    [
        "a" * LONG,
        "token" * (LONG // 5),
        "eyJ" + "A" * 1_000_000,
    ],
    ids=["a", "token", "jwt-prefix"],
)
def test_scan_is_linear(text: str) -> None:
    """B-1: a one-line 1 MiB file is read in well under a second, so one upload cannot hold
    the API's GIL (the name-keyed rule once took 33 s on 3 KB of ``token``)."""
    seconds, found = timed_scan(text)
    assert seconds < 1, f"{seconds:.2f} s"
    assert found == []


def test_a_secret_at_the_start_of_a_long_line_is_still_found() -> None:
    seconds, found = timed_scan(f'API_TOKEN = "{GENERIC}";' + "x=1;" * (LONG // 4 - 16))
    assert seconds < 1
    assert rules(found) == [("generic_secret_assignment", True)]


def test_the_name_keyed_rule_reads_only_the_first_4096_characters_of_a_line() -> None:
    assert rules(one(" " * 4000 + f'API_TOKEN = "{GENERIC}"')) == [
        ("generic_secret_assignment", True)
    ]
    assert one(" " * MAX_LINE_SCAN + f'API_TOKEN = "{GENERIC}"') == []


@pytest.mark.parametrize(
    "unit",
    [
        "AKIA",
        "aws_secret_access_key = ",
        "-----BEGIN A ",
        "eyJ",
        "eyJAAAAAAAAAA.",
        "eyJAAAAAAAAAA.eyJAAAAAAAAAA.",
        "sb_secret_",
        "postgres://",
        "postgres://u:",
        "postgres://u:p@",
        "secret=",
        "token ",
        "token=a",
        "a.token.",
        "ghp_",
        "github_pat_",
        "glpat-",
        "xoxb-",
        "sk_live_",
        "sk-ant-",
        "sk-",
        "sk-proj-",
        "sk-proj-aaaaaaaaaaaaaaaaaaaaaaaa",
        "AIza",
        "npm_",
        "SG.",
        "SG.aaaaaaaaaaaaaaaaaaaaaa.",
    ],
)
def test_a_line_of_one_rule_prefix_repeated_is_linear(unit: str) -> None:
    seconds, _ = timed_scan(unit * (LONG // len(unit)))
    assert seconds < 1, f"{seconds:.2f} s"


@pytest.mark.parametrize("shape", SHAPES, ids=[s.positive[:6] for s in SHAPES])
def test_provider_keys_under_any_name_are_found(shape: Shape) -> None:
    """GA-12 F22: the build's gitleaks finds these under a plain name, so the local scan must
    too (gates/test_gitleaks_parity.py holds gitleaks to the same fixtures)."""
    found = one(line(shape.positive))
    assert rules(found) == [(shape.rule, True)]
    assert shape.positive not in found[0].masked
    assert one(line(shape.negative)) == []


def test_a_provider_key_deep_in_a_minified_line_is_found() -> None:
    key = SHAPES[0].positive
    text = "var a=1;" * 20_000 + f'fetch(u,{{headers:{{Authorization:"token {key}"}}}});' + "b();"
    seconds, found = timed_scan(text)
    assert seconds < 1
    assert rules(found) == [("github_token", True)]

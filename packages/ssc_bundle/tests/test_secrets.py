"""SSC-014 secret rules. Secret-shaped values are assembled at runtime (gitleaks runs on PRs)."""

import base64
import json

import pytest

from ssc_bundle.secrets import MAX_SCAN_BYTES, Finding, entropy, mask, scan

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

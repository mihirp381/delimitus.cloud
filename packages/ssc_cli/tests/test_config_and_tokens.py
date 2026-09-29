"""Where the API address and the token come from, and ``ssc token set`` / ``ssc token clear``."""

import json

import httpx2
import keyring
import pytest
from keyring.backends import fail, null

from ssc_cli.config import DEFAULT_API_URL, config_path, load_config, normalise_api_url
from ssc_cli.credentials import SERVICE, read_token, store_token
from ssc_cli.errors import CliError, ExitCode
from ssc_cli.shapes import ErrorResult, TokenClearResult, TokenSetResult

API = "https://api.test"
WHOAMI = {
    "org_id": "org_aaaaaaaaaaaaaaaaaaaa",
    "subject": "usr_aaaaaaaaaaaaaaaaaaaa",
    "kind": "user",
    "credential_id": "cred_1",
    "is_agent": False,
    "client_id": None,
}
TOKEN = "not-a-real-token.aaaa.bbbb"


def test_tests_never_reach_the_real_keychain(isolated):
    assert keyring.get_keyring() is isolated


# ── token ────────────────────────────────────────────────────────────────────


def test_token_precedence(isolated):
    isolated.set_password(SERVICE, API, "from-keychain")
    assert read_token(API, env={}) == "from-keychain"
    assert read_token(API, env={"SSC_TOKEN": " from-env "}) == "from-env"
    assert read_token(API, env={"SSC_TOKEN": "  "}) == "from-keychain"

    with pytest.raises(CliError) as err:
        read_token("https://other.test", env={})
    assert err.value.body.code == "NO_TOKEN"
    assert err.value.exit_code == ExitCode.AUTH


def test_no_keychain_backend_still_works_with_the_env_var():
    keyring.set_keyring(fail.Keyring())
    assert read_token(API, env={"SSC_TOKEN": "t"}) == "t"
    with pytest.raises(CliError) as err:
        read_token(API, env={})
    assert err.value.body.code == "NO_TOKEN"
    assert "SSC_TOKEN" in err.value.body.detail
    assert err.value.exit_code == ExitCode.AUTH


@pytest.mark.parametrize("backend", [fail.Keyring, null.Keyring])
def test_store_refuses_a_keychain_that_does_not_keep_the_token(backend):
    keyring.set_keyring(backend())
    with pytest.raises(CliError) as err:
        store_token(API, "t")
    assert err.value.body.code == "NO_KEYCHAIN"
    assert err.value.exit_code == ExitCode.FAILED


def test_token_set_reads_stdin_checks_and_stores(cli, fake_api, isolated):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("token", "set", "--json", input=f"{TOKEN}\n", session=fake_api.session())
    assert r.code == 0, r.stderr
    assert TokenSetResult.model_validate(r.json()).subject == WHOAMI["subject"]
    assert isolated.store == {(SERVICE, API): TOKEN}
    assert fake_api.seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in r.stdout + r.stderr


def test_token_set_human_output_hides_the_token(cli, fake_api):
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("token", "set", input=TOKEN, session=fake_api.session())
    assert r.code == 0
    assert WHOAMI["subject"] in r.stdout
    assert TOKEN not in r.stdout + r.stderr


@pytest.mark.parametrize(
    "raw",
    ["", "   \n", "two words", "line1\nline2", "café", "tab\there", "x" * (16 * 1024 + 1)],
)
def test_token_set_refuses_bad_input(cli, fake_api, isolated, raw):
    r = cli("token", "set", "--json", input=raw, session=fake_api.session())
    assert r.code == ExitCode.USAGE
    assert r.json()["error"]["code"] == "BAD_TOKEN_INPUT"
    assert isolated.store == {}
    assert fake_api.seen == []


def test_token_set_refused_by_the_api_stores_nothing(cli, fake_api, fake_problem, isolated):
    fake_api.add("GET", "/v1/whoami", fake_problem(401, "UNAUTHENTICATED"))
    r = cli("token", "set", input=TOKEN, session=fake_api.session())
    assert r.code == ExitCode.AUTH
    assert "Code: UNAUTHENTICATED" in r.stderr
    assert isolated.store == {}


def test_token_set_without_a_keychain(cli, fake_api):
    keyring.set_keyring(null.Keyring())
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("token", "set", "--json", input=TOKEN, session=fake_api.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "NO_KEYCHAIN"


def test_token_set_notes_when_the_env_var_wins(cli, fake_api, monkeypatch):
    monkeypatch.setenv("SSC_TOKEN", "other")
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("token", "set", input=TOKEN, session=fake_api.session())
    assert r.code == 0
    assert "SSC_TOKEN is set" in r.stderr
    assert fake_api.seen[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_token_clear(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, TOKEN)
    isolated.set_password(SERVICE, "https://other.test", "keep")
    r = cli("token", "clear", "--json", session=fake_api.session())
    assert TokenClearResult.model_validate(r.json()).cleared is True
    assert isolated.store == {(SERVICE, "https://other.test"): "keep"}
    again = cli("token", "clear", "--json", session=fake_api.session())
    assert again.code == 0
    assert again.json()["cleared"] is False


def test_commands_use_the_token_kept_for_that_api(cli, fake_api, isolated):
    isolated.set_password(SERVICE, API, "for-api-test")
    isolated.set_password(SERVICE, "https://other.test", "for-other")
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    assert cli("whoami", session=fake_api.session()).code == 0
    assert fake_api.seen[0].headers["authorization"] == "Bearer for-api-test"


# ── API address ──────────────────────────────────────────────────────────────


def test_config_file_lives_under_the_test_home(tmp_path):
    assert config_path().is_relative_to(tmp_path)


def test_api_url_precedence(monkeypatch):
    assert load_config().api_url == DEFAULT_API_URL
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text('api_url = "https://from-file.test/"\n')
    assert load_config().api_url == "https://from-file.test"
    monkeypatch.setenv("SSC_API_URL", "https://from-env.test")
    assert load_config().api_url == "https://from-env.test"
    assert load_config("https://from-flag.test").api_url == "https://from-flag.test"


def test_api_flag_reaches_the_client(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "http://127.0.0.1:9", TOKEN)
    fake_api.add("GET", "/v1/whoami", httpx2.Response(200, json=WHOAMI))
    r = cli("--api", "http://127.0.0.1:9/", "whoami", "--json", session=fake_api.session())
    assert r.code == 0
    assert r.json()["api_url"] == "http://127.0.0.1:9"
    assert str(fake_api.seen[0].url) == "http://127.0.0.1:9/v1/whoami"


@pytest.mark.parametrize(
    "url",
    ["https://api.example.com", "http://localhost:8000", "http://127.0.0.1", "http://[::1]:8000"],
)
def test_accepted_api_urls(url):
    assert normalise_api_url(url + "/") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "http://10.0.0.1:8000",
        "ftp://api.example.com",
        "https://user:pw@api.example.com",
        "https://api.example.com?x=1",
        "https://api.example.com#frag",
        "https://api.example.com:99999",
        "api.example.com",
        "",
    ],
)
def test_refused_api_urls(url):
    with pytest.raises(CliError) as err:
        normalise_api_url(url)
    assert err.value.body.code == "BAD_API_URL"
    assert err.value.exit_code == ExitCode.USAGE


def test_plain_http_to_another_host_is_refused_before_sending(cli, fake_api, isolated):
    isolated.set_password(SERVICE, "http://example.com", TOKEN)
    r = cli("--api", "http://example.com", "whoami", "--json", session=fake_api.session())
    assert r.code == ExitCode.USAGE
    assert ErrorResult.model_validate(r.json()).error.code == "BAD_API_URL"
    assert fake_api.seen == []


@pytest.mark.parametrize("text", ["api_url = ", "api_url = 5\n", "[x\n"])
def test_bad_config_file(cli, text):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    r = cli("whoami", "--json")
    assert r.code == ExitCode.USAGE
    body = json.loads(r.stdout)["error"]
    assert body["code"] == "BAD_CONFIG"
    assert body["status"] is None

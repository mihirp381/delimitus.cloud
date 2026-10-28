"""``ssc domains`` against a fake API: show, add, status (and --wait) and flip."""

import json

import httpx2
import pytest

from ssc_cli.commands.domains import ADMISSION_RULES, POLL_SECONDS
from ssc_cli.credentials import SERVICE
from ssc_cli.errors import ExitCode
from ssc_cli.session import Session
from ssc_cli.shapes import DomainResult, ErrorResult

SUFFIX = "tools.acme.example"
NS = ["ns-cloud-a1.googledomains.com", "ns-cloud-a2.googledomains.com"]
DEFAULT = "apps.delimitus.test"


def _domain(state: str | None = None, *, current: str = DEFAULT, **extra: object) -> dict:
    delegation = (
        None
        if state is None
        else {
            "id": "odm_aaaaaaaaaaaaaaaaaaaa",
            "suffix": SUFFIX,
            "state": state,
            "name_servers": NS,
            "observed_ns": extra.pop("observed_ns", None),
            "failure": extra.pop("failure", None),
            "provider": "cloudflare",
            "created_at": "2026-10-28T00:00:00Z",
            "updated_at": "2026-10-28T00:00:00Z",
            "last_delegated_at": None,
        }
    )
    return {
        "default_suffix": DEFAULT,
        "current_suffix": current,
        "previous": extra.pop("previous", []),
        "delegation": delegation,
        "apps_that_move": [{"slug": "pay", "hosts": [f"pay.{SUFFIX}", f"pay--preview.{SUFFIX}"]}],
        "ready_to_flip": extra.pop("ready_to_flip", False),
        "rolling": extra.pop("rolling", 0),
    }


def _ok(body: dict) -> httpx2.Response:
    return httpx2.Response(200, json=body)


@pytest.fixture
def api(fake_api, isolated):
    isolated.set_password(SERVICE, "https://api.test", "tok")
    return fake_api


def test_show_before_any_request(cli, api):
    api.add("GET", "/v1/domain", _ok(_domain()))
    r = cli("domains", "show", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stdout.splitlines() == [
        f"Apps live under: {DEFAULT}",
        "No own address yet. Ask for one with `ssc domains add <suffix>`.",
    ]
    out = DomainResult.model_validate(
        cli("domains", "show", "--json", session=api.session()).json()
    )
    assert (out.state, out.records, out.it_ticket, out.moved) == (None, [], None, False)


def test_show_pending_lists_records_and_what_the_resolvers_see(cli, api):
    seen = {"google": ["ns1.parent.example"], "cloudflare": []}
    api.add("GET", "/v1/domain", _ok(_domain("pending_dns", observed_ns=seen, rolling=2)))
    r = cli("domains", "show", session=api.session())
    assert r.code == 0, r.stderr
    lines = r.stdout.splitlines()
    assert f"Own address: {SUFFIX} (pending_dns)" in lines
    assert "NS records to add in acme.example:" in lines
    assert f"  {SUFFIX} NS {NS[0]}" in lines
    assert "What we see from the internet right now: ns1.parent.example" in lines
    assert "2 apps are still being prepared." in lines
    assert ["pay", f"pay.{SUFFIX}", f"pay--preview.{SUFFIX}"] in [line.split() for line in lines]


def test_add_puts_the_suffix_and_prints_the_ticket(cli, api):
    api.add("PUT", "/v1/domain", _ok(_domain("pending_dns")))
    r = cli("domains", "add", SUFFIX, session=api.session())
    assert r.code == 0, r.stderr
    (put,) = [q for q in api.seen if q.method == "PUT"]
    assert json.loads(put.content) == {"suffix": SUFFIX}
    assert "Please add these NS records in the DNS zone acme.example:" in r.stdout
    assert f"{SUFFIX} NS {NS[1]}" in r.stdout
    assert "Nothing else changes: no other record in acme.example is touched." in r.stdout
    out = DomainResult.model_validate(
        cli("domains", "add", SUFFIX, "--json", session=api.session()).json()
    )
    assert out.records == [f"{SUFFIX} NS {ns}" for ns in NS]
    assert out.it_ticket is not None and out.it_ticket.startswith("Please add")


def test_add_refused_names_the_rules(cli, api, fake_problem):
    api.add("PUT", "/v1/domain", fake_problem(422, "VALIDATION_FAILED"))
    r = cli("domains", "add", "acme.example", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    error = ErrorResult.model_validate(r.json()).error
    assert (error.code, error.fix) == ("VALIDATION_FAILED", ADMISSION_RULES)


def test_status_is_one_line_per_field(cli, api):
    api.add("GET", "/v1/domain", _ok(_domain("issuing", failure="validating")))
    r = cli("domains", "status", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stdout.splitlines() == [
        "state: issuing",
        f"suffix: {SUFFIX}",
        f"current_suffix: {DEFAULT}",
        f"default_suffix: {DEFAULT}",
        "observed: -",
        "failure: validating",
        "rolling: 0",
        "ready_to_flip: no",
        "moved: no",
    ]


def test_status_wait_polls_until_active(cli, api, isolated):
    slept: list[float] = []
    api.add(
        "GET",
        "/v1/domain",
        _ok(_domain("pending_dns")),
        _ok(_domain("issuing")),
        _ok(_domain("active", ready_to_flip=True)),
    )
    session = Session(
        api_override="https://api.test",
        transport=httpx2.MockTransport(api.handler),
        sleep=slept.append,
    )
    r = cli("domains", "status", "--wait", "--json", session=session)
    assert r.code == 0, r.stderr
    out = DomainResult.model_validate(r.json())
    assert (out.state, out.ready_to_flip) == ("active", True)
    assert slept == [POLL_SECONDS, POLL_SECONDS]


def test_status_wait_ends_on_failed_and_on_the_timeout(cli, api):
    api.add("GET", "/v1/domain", _ok(_domain("failed", failure="NS never seen")))
    r = cli("domains", "status", "--wait", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    error = r.json()["error"]
    assert (error["code"], error["detail"]) == ("DOMAIN_FAILED", f"{SUFFIX}: NS never seen")
    stuck = type(api)()
    stuck.add("GET", "/v1/domain", _ok(_domain("issuing")))
    r = cli("domains", "status", "--wait", "--timeout", "60", "--json", session=stuck.session())
    assert r.code == ExitCode.FAILED
    assert r.json()["error"]["code"] == "WAIT_TIMED_OUT"
    assert len(stuck.seen) == 3


def test_flip_needs_confirm_and_posts_the_direction(cli, api):
    refused = cli("domains", "flip", session=api.session())
    assert refused.code == 2
    assert not api.seen
    until = "2026-11-27T00:00:00Z"
    api.add(
        "POST",
        "/v1/domain/flip",
        _ok(_domain("active", current=SUFFIX, previous=[{"suffix": DEFAULT, "until": until}])),
        _ok(_domain("active", previous=[{"suffix": SUFFIX, "until": until}])),
    )
    r = cli("domains", "flip", "--confirm", session=api.session())
    assert r.code == 0, r.stderr
    assert r.stdout.splitlines() == [
        f"Your apps now live under {SUFFIX}.",
        f"Old addresses under {DEFAULT} redirect until {until}",
    ]
    back = cli("domains", "flip", "--back", "--confirm", "--json", session=api.session())
    assert DomainResult.model_validate(back.json()).moved is False
    posts = [q for q in api.seen if q.method == "POST"]
    assert [json.loads(q.content) for q in posts] == [
        {"to": "customer", "confirm": True},
        {"to": "default", "confirm": True},
    ]
    assert all(q.headers["idempotency-key"] for q in posts)


def test_flip_not_ready_says_what_to_wait_for(cli, api, fake_problem):
    api.add("POST", "/v1/domain/flip", fake_problem(409, "DOMAIN_NOT_READY"))
    r = cli("domains", "flip", "--confirm", "--json", session=api.session())
    assert r.code == ExitCode.FAILED
    assert "ssc domains status" in r.json()["error"]["fix"]

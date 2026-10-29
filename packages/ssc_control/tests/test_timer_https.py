"""SSC-041: the HTTPS timer dispatcher and its key, against a mock transport. Each request
carries a token the gateway's own verifier admits for exactly that request; redirects are
answers, not followed, but for one 307 from a previous suffix (CD-4.3); no answer is
``dispatch_error``, logged without the token."""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from ssc_contracts.schedule_token import MAX_TTL_SECONDS, SCHEDULE_TOKEN_HEADER
from ssc_control.timers.dispatch import DispatchResult, TimerCall
from ssc_control.timers.https import (
    USER_AGENT,
    HttpsScheduleDispatcher,
    ScheduleSigner,
    new_timer_pem,
)
from ssc_edge.schedule_token import ScheduleKeys, parse_timer_jwks

NOW = 1_790_000_000
LABEL = "bcdfghjklmnp"
DOMAIN = "apps.test"
CALL = TimerCall(
    org_id="org_" + "a" * 20,
    environment_id="env_" + "p" * 20,
    schedule_id="sch_" + "s" * 20,
    run_id="tmr_" + "r" * 20,
    method="POST",
    path="/tasks/tick?full=1",
    slug="ledger",
    environment="prod",
    cell_label=LABEL,
    health_path="/healthz",
    domain_suffix="tools.testorg.com",
)
ORIGIN = f"https://ledger.{LABEL}.{DOMAIN}"


@pytest.fixture
def signer() -> ScheduleSigner:
    return ScheduleSigner(new_timer_pem(), "timer-1", clock=lambda: NOW)


def gateway(signer: ScheduleSigner) -> ScheduleKeys:
    return ScheduleKeys(parse_timer_jwks(json.dumps(signer.jwks())), clock=lambda: NOW)


def dispatcher(
    signer: ScheduleSigner, answer: httpx2.Response | Exception, seen: list[httpx2.Request]
) -> HttpsScheduleDispatcher:
    """``answer``'s status and headers, with a body streamed as an app streams it."""

    async def body() -> AsyncIterator[bytes]:
        yield b"done"

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return httpx2.Response(answer.status_code, headers=answer.headers, content=body())

    return HttpsScheduleDispatcher(
        signer, apps_domain=DOMAIN, transport=httpx2.MockTransport(handle)
    )


async def test_the_call_carries_a_token_bound_to_it(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    d = dispatcher(signer, httpx2.Response(204), seen)
    assert await d.dispatch(CALL) == DispatchResult(http_status=204)
    await d.aclose()
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", ORIGIN + "/tasks/tick?full=1")
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["content-length"] == "0"
    token = request.headers[SCHEDULE_TOKEN_HEADER]
    claims = gateway(signer).verify(
        token, origin=ORIGIN, org=CALL.org_id, method="POST", path="/tasks/tick?full=1"
    )
    assert claims is not None
    assert (claims.sub, claims.env, claims.jti) == (
        CALL.schedule_id,
        CALL.environment_id,
        CALL.run_id,
    )
    assert claims.exp - claims.iat == MAX_TTL_SECONDS
    assert (
        gateway(signer).verify(token, origin=ORIGIN, org=CALL.org_id, method="GET", path="/")
        is None
    )


async def test_the_start_asks_the_health_path_with_its_own_token(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    d = dispatcher(signer, httpx2.Response(200), seen)
    assert await d.start(CALL) == DispatchResult(http_status=200)
    assert await d.dispatch(CALL) == DispatchResult(http_status=200)
    await d.aclose()
    start, call = seen
    assert (start.method, str(start.url)) == ("GET", ORIGIN + "/healthz")
    assert "content-length" not in start.headers
    keys = gateway(signer)
    woke = keys.verify(
        start.headers[SCHEDULE_TOKEN_HEADER],
        origin=ORIGIN,
        org=CALL.org_id,
        method="GET",
        path="/healthz",
    )
    assert woke is not None and woke.jti == CALL.run_id + ".start"
    sent = keys.verify(
        call.headers[SCHEDULE_TOKEN_HEADER],
        origin=ORIGIN,
        org=CALL.org_id,
        method="POST",
        path="/tasks/tick?full=1",
    )
    assert sent is not None and sent.jti == CALL.run_id


async def test_a_path_is_bound_as_it_is_sent(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    d = dispatcher(signer, httpx2.Response(200), seen)
    call = TimerCall(
        org_id=CALL.org_id,
        environment_id=CALL.environment_id,
        schedule_id=CALL.schedule_id,
        run_id=CALL.run_id,
        method="GET",
        path="/tasks/a b{c}",
        slug=CALL.slug,
        environment=CALL.environment,
        cell_label=CALL.cell_label,
        health_path=CALL.health_path,
    )
    await d.dispatch(call)
    await d.aclose()
    (request,) = seen
    assert request.url.raw_path == b"/tasks/a%20b%7Bc%7D"
    claims = gateway(signer).verify(
        request.headers[SCHEDULE_TOKEN_HEADER],
        origin=ORIGIN,
        org=CALL.org_id,
        method="GET",
        path="/tasks/a%20b%7Bc%7D",
    )
    assert claims is not None


@pytest.mark.parametrize("status", [302, 307, 404, 503])
async def test_a_redirect_or_an_error_status_is_the_answer(
    signer: ScheduleSigner, status: int
) -> None:
    seen: list[httpx2.Request] = []
    answer = httpx2.Response(status, headers={"location": "https://auth.example.test/login"})
    d = dispatcher(signer, answer, seen)
    assert await d.dispatch(CALL) == DispatchResult(http_status=status)
    await d.aclose()
    assert len(seen) == 1


MOVED = "https://ledger.tools.testorg.com"


def answering(
    signer: ScheduleSigner, answers: list[httpx2.Response], seen: list[httpx2.Request]
) -> HttpsScheduleDispatcher:
    async def body() -> AsyncIterator[bytes]:
        yield b""

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        answer = answers.pop(0)
        return httpx2.Response(answer.status_code, headers=answer.headers, content=body())

    return HttpsScheduleDispatcher(
        signer, apps_domain=DOMAIN, transport=httpx2.MockTransport(handle)
    )


async def test_a_redirect_from_a_previous_suffix_is_sent_once_more(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    moved = httpx2.Response(307, headers={"location": MOVED + "/tasks/tick?full=1"})
    d = answering(signer, [moved, httpx2.Response(204)], seen)
    assert await d.dispatch(CALL) == DispatchResult(http_status=204)
    first, again = seen
    assert str(first.url) == ORIGIN + "/tasks/tick?full=1"
    assert str(again.url) == MOVED + "/tasks/tick?full=1"
    claims = gateway(signer).verify(
        again.headers[SCHEDULE_TOKEN_HEADER],
        origin=MOVED,
        org=CALL.org_id,
        method="POST",
        path="/tasks/tick?full=1",
    )
    assert claims is not None and claims.jti == CALL.run_id

    seen.clear()
    back = httpx2.Response(307, headers={"location": ORIGIN + "/tasks/tick?full=1"})
    d = answering(signer, [moved, back], seen)
    assert await d.dispatch(CALL) == DispatchResult(http_status=307)
    assert len(seen) == 2
    await d.aclose()


async def test_a_flip_back_redirect_goes_to_the_default_suffix(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    on_domain = replace(CALL, apps_suffix="tools.testorg.com")
    back = httpx2.Response(307, headers={"location": ORIGIN + "/tasks/tick?full=1"})
    d = answering(signer, [back, httpx2.Response(204)], seen)
    assert await d.dispatch(on_domain) == DispatchResult(http_status=204)
    assert [str(r.url) for r in seen] == [
        MOVED + "/tasks/tick?full=1",
        ORIGIN + "/tasks/tick?full=1",
    ]
    await d.aclose()


async def test_no_own_domain_means_no_second_call(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    moved = httpx2.Response(307, headers={"location": MOVED + "/tasks/tick?full=1"})
    d = answering(signer, [moved], seen)
    no_domain = replace(CALL, domain_suffix=None)
    assert await d.dispatch(no_domain) == DispatchResult(http_status=307)
    assert len(seen) == 1
    await d.aclose()


@pytest.mark.parametrize(
    "location",
    [
        "https://payroll.tools.testorg.com/tasks/tick?full=1",
        MOVED + "/tasks/other",
        MOVED + "/tasks/tick",
        "http://ledger.tools.testorg.com/tasks/tick?full=1",
        "https://ledger.tools.testorg.com:8443/tasks/tick?full=1",
        "https://ledger.localhost/tasks/tick?full=1",
        "https://ledger.tools.elsewhere.com/tasks/tick?full=1",
        "https://ledger.169.254.169.254.nip.io/tasks/tick?full=1",
        ORIGIN + "/tasks/tick?full=1",
        "/tasks/tick?full=1",
    ],
)
async def test_any_other_307_is_the_answer(signer: ScheduleSigner, location: str) -> None:
    seen: list[httpx2.Request] = []
    d = answering(signer, [httpx2.Response(307, headers={"location": location})], seen)
    assert await d.dispatch(CALL) == DispatchResult(http_status=307)
    assert len(seen) == 1
    await d.aclose()


async def test_no_answer_is_a_dispatch_error_logged_without_the_token(
    signer: ScheduleSigner, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[httpx2.Request] = []
    d = dispatcher(signer, httpx2.ConnectError("refused"), seen)
    with caplog.at_level(logging.WARNING):
        assert await d.dispatch(CALL) == DispatchResult(error="dispatch_error")
        assert await d.start(CALL) == DispatchResult(error="dispatch_error")
    await d.aclose()
    tokens = [r.headers[SCHEDULE_TOKEN_HEADER] for r in seen]
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert "ConnectError" in logged
    assert all(t not in logged for t in tokens)


async def test_a_call_with_no_app_host_is_never_sent(signer: ScheduleSigner) -> None:
    seen: list[httpx2.Request] = []
    d = dispatcher(signer, httpx2.Response(200), seen)
    bad = TimerCall(
        org_id=CALL.org_id,
        environment_id=CALL.environment_id,
        schedule_id=CALL.schedule_id,
        run_id=CALL.run_id,
        method="POST",
        path="/x",
        slug="Not-A-Slug",
        environment="prod",
        cell_label=LABEL,
        health_path="/",
    )
    assert d.origin(bad) is None
    assert await d.dispatch(bad) == DispatchResult(error="dispatch_error")
    await d.aclose()
    assert seen == []


def test_the_signer_takes_only_a_p256_key_with_an_id() -> None:
    wrong_curve = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    not_ec = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    for pem in (wrong_curve, not_ec, b"not a key"):
        with pytest.raises(ValueError, match="timer key"):
            ScheduleSigner(pem, "timer-1")
    with pytest.raises(ValueError, match="id"):
        ScheduleSigner(new_timer_pem(), "")


def test_the_public_jwks_holds_no_private_part(signer: ScheduleSigner) -> None:
    (key,) = signer.jwks()["keys"]
    assert (key["kty"], key["crv"], key["kid"], key["alg"]) == ("EC", "P-256", "timer-1", "ES256")
    assert "d" not in key
    parse_timer_jwks(json.dumps(signer.jwks()))

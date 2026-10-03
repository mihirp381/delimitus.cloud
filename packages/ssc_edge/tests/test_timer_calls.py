"""Timer calls at the gateway (SSC-041): a schedule token admits one request with no session, the
app gets a ``schedule`` note, and nothing a timer meets is a login redirect or the "waking up"
page. Every refusal is the wrong-address ``404``."""

import json
from typing import Any

import jwt
import pytest
from edge_world import (
    HOST,
    KID,
    LABEL,
    LEDGER,
    NOW,
    NOWHERE_HOST,
    ORG,
    OTHER_ORG,
    PAY,
    PAY_HOST,
    PREVIEW,
    PREVIEW_HOST,
    PROD,
    RUN,
    SCH,
    Signer,
    World,
    config,
    snapshot,
)
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from ssc_app.identity import verify
from ssc_contracts.schedule_token import SCHEDULE_TOKEN_HEADER
from ssc_edge import pages
from ssc_edge.gate import (
    DEADLINE_HEADER,
    IDENTITY_HEADER,
    SCHEDULE_HEADER,
    UPSTREAM_HEADER,
    WAKE_HEADER,
    Allow,
    Deny,
    Facts,
    Gate,
)
from ssc_edge.identity_note import jwks
from ssc_edge.schedule_token import ScheduleKeys, ScheduleKeysError, parse_timer_jwks
from ssc_edge.server import SettingsError, create_app, gate_for, settings_from_env
from ssc_shared.access import AccessView


@pytest.fixture
def signer() -> Signer:
    return Signer()


def timer_gate(world: World, signer: Signer, **cfg: Any) -> Gate:
    return world.gate(timer=signer, **cfg)


def call(
    token: str, host: str = HOST, path: str = "/tasks/tick", method: str = "POST", **headers: str
) -> Facts:
    extra = {k.replace("_", "-"): v for k, v in headers.items()}
    return Facts(method=method, host=host, path=path, headers={SCHEDULE_HEADER: token, **extra})


def assert_wrong_address(out: Allow | Deny) -> None:
    assert isinstance(out, Deny), out
    assert (out.status, out.body) == (404, pages.NOT_FOUND)
    assert all(k.lower() not in {"location", "set-cookie"} for k, _ in out.headers)


async def test_a_timer_call_needs_no_session_and_the_app_gets_a_schedule_note(
    world: World, signer: Signer
) -> None:
    out = await timer_gate(world, signer).check(call(signer.token()))
    assert isinstance(out, Allow), out
    assert (out.user, out.environment, out.session) == (SCH, PROD, None)
    assert WAKE_HEADER not in out.headers and out.client_headers == ()
    assert out.headers[UPSTREAM_HEADER] == out.upstream
    assert int(out.headers[DEADLINE_HEADER]) > NOW
    keys = jwks((world.keyring.signing_key.public_key(), world.keyring.identity_kid))
    note = verify(
        out.headers[IDENTITY_HEADER],
        audience=f"https://{HOST}",
        keys=keys,
        issuer=f"https://keys.example.test/{LABEL}",
        now=NOW,
    )
    assert (note.sub, note.app, note.env, note.role) == (SCH, LEDGER, "prod", "schedule")
    assert (note.groups, note.name, note.email) == ((), None, None)


async def test_a_page_load_shaped_timer_call_is_never_sent_to_the_waking_page(
    world: World, signer: Signer
) -> None:
    page_load = call(
        signer.token(htm="GET", htu="/"),
        path="/",
        method="GET",
        sec_fetch_mode="navigate",
        sec_fetch_dest="document",
        accept="text/html",
    )
    out = await timer_gate(world, signer).check(page_load)
    assert isinstance(out, Allow) and WAKE_HEADER not in out.headers


async def test_the_query_is_part_of_the_bound_path(world: World, signer: Signer) -> None:
    gate = timer_gate(world, signer)
    token = signer.token(htm="GET", htu="/tasks/tick?full=1")
    assert isinstance(await gate.check(call(token, path="/tasks/tick?full=1", method="GET")), Allow)
    other = signer.token(htm="GET", htu="/tasks/tick?full=1", jti="tmr_" + "q" * 20)
    assert_wrong_address(await gate.check(call(other, path="/tasks/tick?full=2", method="GET")))


def _refused_tokens(signer: Signer) -> dict[str, tuple[str, dict[str, Any]]]:
    stranger = Signer(kid=KID)
    return {
        "other host": (signer.token(aud=f"https://{PAY_HOST}"), {}),
        "other method": (signer.token(htm="GET"), {}),
        "other path": (signer.token(htu="/tasks/other"), {}),
        "other org": (signer.token(org=OTHER_ORG), {}),
        "expired": (signer.token(iat=NOW - 200, exp=NOW - 80), {}),
        "from the future": (signer.token(iat=NOW + 60, exp=NOW + 120), {}),
        "too long a life": (signer.token(iat=NOW - 5, exp=NOW + 116), {}),
        "unknown key id": (signer.token(headers={"kid": "timer-9"}), {}),
        "a stranger's key": (stranger.token(), {}),
        "wrong type": (signer.token(headers={"typ": "JWT"}), {}),
        "unsigned": (jwt.encode({"sub": SCH}, None, algorithm="none"), {}),
        "garbage": ("not.a.token", {}),
        "extra claim": (signer.token(role="admin"), {}),
        "a session id": (signer.token(sub="usr_" + "a" * 20), {}),
        "another environment": (signer.token(env=PREVIEW), {}),
        "another app's environment": (signer.token(env=PAY), {}),
        "a stream": (signer.token(), {"accept": "text/event-stream"}),
    }


@pytest.mark.parametrize(
    "case",
    [
        "other host",
        "other method",
        "other path",
        "other org",
        "expired",
        "from the future",
        "too long a life",
        "unknown key id",
        "a stranger's key",
        "wrong type",
        "unsigned",
        "garbage",
        "extra claim",
        "a session id",
        "another environment",
        "another app's environment",
        "a stream",
    ],
)
async def test_a_refused_token_is_the_wrong_address_page_never_a_login(
    world: World, signer: Signer, case: str
) -> None:
    token, headers = _refused_tokens(signer)[case]
    gate = timer_gate(world, signer)
    out = await gate.check(call(token, **headers))
    assert_wrong_address(out)
    assert world.redeemer.calls == []


async def test_a_token_is_taken_once(world: World, signer: Signer) -> None:
    gate = timer_gate(world, signer)
    token = signer.token()
    assert isinstance(await gate.check(call(token)), Allow)
    assert_wrong_address(await gate.check(call(token)))
    start = signer.token(htm="GET", htu="/", jti=RUN + ".start")
    assert isinstance(await gate.check(call(start, path="/", method="GET")), Allow)


async def test_the_token_life_has_thirty_seconds_of_leeway(world: World, signer: Signer) -> None:
    gate = timer_gate(world, signer)
    late = signer.token(iat=NOW - 140, exp=NOW - 29, jti="tmr_" + "1" * 20)
    assert isinstance(await gate.check(call(late)), Allow)
    gone = signer.token(iat=NOW - 141, exp=NOW - 30, jti="tmr_" + "2" * 20)
    assert_wrong_address(await gate.check(call(gone)))


async def test_a_disabled_app_refuses_its_timer_calls(world: World, signer: Signer) -> None:
    doc = snapshot()
    doc["environments"][PROD]["status"] = "disabled"
    world.view = AccessView.from_document(doc)
    assert_wrong_address(await timer_gate(world, signer).check(call(signer.token())))


async def test_a_preview_timer_reaches_preview_only(world: World, signer: Signer) -> None:
    token = signer.token(aud=f"https://{PREVIEW_HOST}", env=PREVIEW)
    out = await timer_gate(world, signer).check(call(token, host=PREVIEW_HOST))
    assert isinstance(out, Allow) and out.environment == PREVIEW


async def test_a_gateway_without_timer_keys_refuses_every_timer_call(
    world: World, signer: Signer
) -> None:
    assert_wrong_address(await world.gate().check(call(signer.token())))


async def test_a_wrong_address_with_a_token_is_still_the_wrong_address(
    world: World, signer: Signer
) -> None:
    token = signer.token(aud=f"https://{NOWHERE_HOST}")
    assert_wrong_address(await timer_gate(world, signer).check(call(token, host=NOWHERE_HOST)))


async def test_no_snapshot_is_a_plain_503(world: World, signer: Signer) -> None:
    world.view = None
    out = await timer_gate(world, signer).check(call(signer.token()))
    assert isinstance(out, Deny) and (out.status, out.body) == (503, pages.UNAVAILABLE)
    assert all(k.lower() not in {"location", "set-cookie"} for k, _ in out.headers)


async def test_a_cold_gateway_loads_the_snapshot_before_it_answers(
    world: World, signer: Signer
) -> None:
    loaded: list[int] = []
    held, world.view = world.view, None

    async def refresh() -> None:
        loaded.append(1)
        world.view = held

    gate = gate_for(
        config(),
        world.keyring,
        view=lambda: world.view,
        clock=lambda: world.now,
        refresh=refresh,
        timer_keys=parse_timer_jwks(signer.jwks()),
    )
    assert isinstance(await gate.check(call(signer.token())), Allow)
    assert loaded == [1]


class Tokens:
    async def identity(self, audience: str) -> str:
        return "google.id.token"


def test_the_authz_server_reads_the_token_header(world: World, signer: Signer) -> None:
    gate = timer_gate(world, signer)
    client = TestClient(create_app(lambda: gate, tokens=Tokens()))
    token = signer.token(htm="GET", htu="/tasks/tick?full=1")
    r = client.get("/authz/tasks/tick?full=1", headers={"host": HOST, SCHEDULE_TOKEN_HEADER: token})
    assert r.status_code == 200, r.text
    assert IDENTITY_HEADER in r.headers and WAKE_HEADER not in r.headers


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("nope", "not JSON"),
        ('{"keys": []}', "1 to 2"),
        ('{"keys": [{}, {}, {}]}', "1 to 2"),
        ('{"keys": [{"kty": "RSA", "kid": "a"}]}', "P-256"),
        ('{"keys": [{"kty": "EC", "crv": "P-256"}]}', "P-256"),
    ],
)
def test_timer_jwks_must_be_one_or_two_named_public_p256_keys(raw: str, message: str) -> None:
    with pytest.raises(ScheduleKeysError, match=message):
        parse_timer_jwks(raw)


def test_a_private_key_in_the_timer_jwks_is_refused(signer: Signer) -> None:
    jwk = dict(ECAlgorithm.to_jwk(signer.key, as_dict=True))
    with pytest.raises(ScheduleKeysError, match="public"):
        parse_timer_jwks(json.dumps({"keys": [{**jwk, "kid": KID}]}))


def test_two_keys_verify_during_a_rotation(signer: Signer) -> None:
    second = Signer(kid="timer-2")
    both = json.loads(signer.jwks())["keys"] + json.loads(second.jwks())["keys"]
    keys = ScheduleKeys(parse_timer_jwks(json.dumps({"keys": both})), clock=lambda: NOW)
    bound = {"origin": f"https://{HOST}", "org": ORG, "method": "POST", "path": "/tasks/tick"}
    assert keys.verify(signer.token(), **bound) is not None
    assert keys.verify(second.token(jti="tmr_" + "t" * 20), **bound) is not None


def test_the_gateway_reads_ssc_timer_jwks(signer: Signer) -> None:
    base = {
        "SSC_ORG_ID": ORG,
        "SSC_CELL_LABEL": LABEL,
        "SSC_PROJECT_NUMBER": "123456789012",
        "SSC_CELL_BUCKET": f"ssc-c-{LABEL}-cell",
        "SSC_GATEWAY_KEYRING": "Y2lwaGVy",
        "SSC_GATEWAY_KMS_KEY": "projects/p/locations/l/keyRings/r/cryptoKeys/k",
    }
    assert settings_from_env(base).timer_keys is None
    keys = settings_from_env({**base, "SSC_TIMER_JWKS": signer.jwks()}).timer_keys
    assert keys is not None and keys[KID] is not None
    with pytest.raises(SettingsError, match="SSC_TIMER_JWKS"):
        settings_from_env({**base, "SSC_TIMER_JWKS": '{"keys": []}'})

"""The gateway's decision (SSC-018): every stage, and a forbidden app answered exactly like an
address with no app."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from edge_world import (
    ADA,
    BEN,
    CY,
    DOMAIN,
    FIN,
    HOST,
    LABEL,
    LEDGER,
    LOGIN,
    NONCE,
    NOW,
    NOWHERE_HOST,
    ORG,
    OTHER_ORG,
    PAY_HOST,
    PREVIEW_HOST,
    PROD,
    World,
    session,
    snapshot,
)

from ssc_app.identity import verify
from ssc_edge import pages
from ssc_edge.gate import IDENTITY_HEADER, UPSTREAM_HEADER, Allow, Deny, Facts, binding_of
from ssc_edge.identity_note import jwks
from ssc_edge.session import clear_cookie, clear_login_cookie, login_cookie
from ssc_shared.access import AccessView

UPSTREAM = "ssc-a-" + "p" * 20 + "-123456789012.us-central1.run.app"


def facts(host: str = HOST, path: str = "/", method: str = "GET", **headers: str) -> Facts:
    return Facts(
        method=method,
        host=host,
        path=path,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
    )


async def check(world: World, f: Facts, **cfg: Any) -> Allow | Deny:
    return await world.gate(**cfg).check(f)


async def denied(world: World, f: Facts) -> Deny:
    out = await check(world, f)
    assert isinstance(out, Deny), out
    return out


def same_answer(a: Deny, b: Deny) -> bool:
    return (a.status, a.headers, a.body) == (b.status, b.headers, b.body)


async def test_an_allowed_request_carries_a_note_the_app_helper_accepts(world: World) -> None:
    out = await check(world, facts(cookie=world.cookie()))
    assert isinstance(out, Allow)
    assert out.upstream == UPSTREAM and out.headers[UPSTREAM_HEADER] == UPSTREAM
    assert (out.user, out.environment) == (ADA, PROD)
    keys = jwks((world.keyring.signing_key.public_key(), world.keyring.identity_kid))
    note = verify(
        out.headers[IDENTITY_HEADER],
        audience=f"https://{HOST}",
        keys=keys,
        issuer=f"https://keys.example.test/{LABEL}",
        now=NOW,
    )
    assert (note.sub, note.app, note.env, note.role) == (ADA, LEDGER, "prod", "builder")
    assert note.groups == (FIN,)  # OPS is Ada's too, but the rule does not name it
    assert (note.name, note.email) == ("Ada L", "ada@example.test")


async def test_a_forbidden_app_answers_exactly_like_an_address_with_no_app(world: World) -> None:
    ben = world.cookie(session(BEN), PAY_HOST)
    forbidden = await denied(world, facts(PAY_HOST, cookie=ben))
    nowhere = await denied(
        world, facts(NOWHERE_HOST, cookie=world.cookie(session(BEN), NOWHERE_HOST))
    )
    other_cell = await denied(world, facts(f"payroll.zzzzzzzzzz.{DOMAIN}", cookie=ben))
    foreign = await denied(world, facts("payroll.example.com", cookie=ben))
    below_floor = await denied(
        world, facts(PREVIEW_HOST, cookie=world.cookie(session(BEN), PREVIEW_HOST))
    )
    deactivated = await denied(world, facts(cookie=world.cookie(session(CY))))
    assert forbidden.status == 404
    for other in (nowhere, other_cell, foreign, below_floor, deactivated):
        assert same_answer(forbidden, other)
    assert {forbidden.reason, nowhere.reason, foreign.reason, below_floor.reason} == {
        "not_granted",
        "unknown_host_label",
        "not_app_host",
    }


async def test_no_session_goes_to_login_whether_or_not_an_app_lives_there(world: World) -> None:
    for host in (HOST, NOWHERE_HOST, PAY_HOST):
        out = await denied(world, facts(host, "/books?year=2026"))
        assert out.status == 302 and out.reason == "no_session"
        location = dict(out.headers)["location"]
        assert location.startswith("https://auth.example.test/login?")
        assert parse_qs(urlsplit(location).query) == {
            "org": [ORG],
            "return_to": [f"https://{host}/books?year=2026"],
            "binding": [binding_of(NONCE)],
        }
        assert [v for k, v in out.headers if k == "set-cookie"] == [login_cookie(NONCE)]


@pytest.mark.parametrize(
    "make",
    [
        lambda w: w.cookie(session(), PAY_HOST),  # sealed for another app
        lambda w: w.cookie(session(org=OTHER_ORG)),  # another org's session
        lambda w: w.cookie(session(iat=NOW - 7200, life=3600)),  # expired
        lambda w: "__Host-ssc-session=v1.s1.garbage",
    ],
)
async def test_an_unusable_session_is_cleared_and_sent_to_login(world: World, make) -> None:  # noqa: ANN001
    out = await denied(world, facts(cookie=make(world)))
    assert out.status == 302
    assert "Max-Age=0" in dict(out.headers)["set-cookie"]


async def test_no_snapshot_is_unavailable_for_every_host(world: World) -> None:
    world.view = None
    for host in (HOST, NOWHERE_HOST):
        out = await denied(world, facts(host, cookie=world.cookie(session(), host)))
        assert (out.status, out.reason) == (503, "no_view")


async def test_a_new_snapshot_takes_effect_on_the_next_request(world: World) -> None:
    f = facts(PAY_HOST, cookie=world.cookie(session(), PAY_HOST))
    assert isinstance(await check(world, f), Allow)
    world.view = AccessView.from_document(
        snapshot(2, grants={**snapshot()["grants"], "env_" + "y" * 20: []})
    )
    assert (await denied(world, f)).reason == "not_granted"


async def test_a_host_label_pointing_at_the_wrong_environment_is_not_found(world: World) -> None:
    world.view = AccessView.from_document(snapshot(hosts={"ledger--preview": PROD}))
    out = await denied(world, facts(PREVIEW_HOST, cookie=world.cookie(session(), PREVIEW_HOST)))
    assert out.status == 404


@pytest.mark.parametrize(
    ("method", "headers", "allowed"),
    [
        ("GET", {"sec_fetch_site": "same-origin", "sec_fetch_mode": "cors"}, True),
        ("POST", {"sec_fetch_site": "same-origin", "sec_fetch_mode": "cors"}, True),
        ("POST", {"sec_fetch_site": "none"}, True),
        ("POST", {}, True),  # no Fetch-Metadata: an old browser or a script with the cookie
        (
            "GET",
            {
                "sec_fetch_site": "cross-site",
                "sec_fetch_mode": "navigate",
                "sec_fetch_dest": "document",
            },
            True,
        ),
        ("HEAD", {"sec_fetch_site": "same-site", "sec_fetch_mode": "navigate"}, True),
        (
            "POST",
            {
                "sec_fetch_site": "cross-site",
                "sec_fetch_mode": "navigate",
                "sec_fetch_dest": "document",
            },
            False,
        ),
        ("GET", {"sec_fetch_site": "same-site", "sec_fetch_mode": "cors"}, False),  # another app
        (
            "GET",
            {
                "sec_fetch_site": "cross-site",
                "sec_fetch_mode": "navigate",
                "sec_fetch_dest": "iframe",
            },
            False,
        ),
        (
            "GET",
            {
                "sec_fetch_site": "cross-site",
                "sec_fetch_mode": "no-cors",
                "sec_fetch_dest": "image",
            },
            False,
        ),
    ],
)
async def test_fetch_metadata(
    world: World, method: str, headers: dict[str, str], allowed: bool
) -> None:
    out = await check(world, facts(method=method, cookie=world.cookie(), **headers))
    if allowed:
        assert isinstance(out, Allow)
    else:
        assert isinstance(out, Deny) and (out.status, out.reason) == (403, "cross_origin")


async def test_a_websocket_must_come_from_the_apps_own_origin(world: World) -> None:
    ws = {"upgrade": "websocket", "cookie": world.cookie()}
    assert isinstance(await check(world, facts(origin=f"https://{HOST}", **ws)), Allow)
    for origin in (f"https://{PAY_HOST}", f"http://{HOST}", "null"):
        assert (await denied(world, facts(origin=origin, **ws))).reason == "websocket_origin"
    assert (await denied(world, facts(**ws))).reason == "websocket_origin"


async def test_a_declared_body_over_the_cap_is_refused_before_the_session(world: World) -> None:
    out = await denied(world, facts(method="POST", content_length="1025"))
    assert (out.status, out.reason) == (413, "too_large")
    assert isinstance(
        await check(world, facts(method="POST", content_length="1024", cookie=world.cookie())),
        Allow,
    )


async def test_the_callback_sets_a_host_only_session_and_returns_to_a_local_path(
    world: World,
) -> None:
    world.redeemer.sessions["c0de"] = session()
    path = "/.ssc/callback?code=c0de&next=/books%3Fy%3D1"
    out = await denied(world, facts(path=path, cookie=LOGIN))
    headers = dict(out.headers)
    assert (out.status, out.reason, headers["location"]) == (302, "signed_in", "/books?y=1")
    assert world.redeemer.calls == [("c0de", HOST, NONCE)]
    cookies = [v for k, v in out.headers if k == "set-cookie"]
    assert cookies[1] == clear_login_cookie()
    value = cookies[0].split(";")[0].split("=", 1)[1]
    assert world.codec.open(value, HOST, now=NOW) is not None
    assert "domain" not in cookies[0].lower()


@pytest.mark.parametrize("nxt", ["//evil.example", "https://evil.example/", "/\\evil.example", ""])
async def test_the_callback_never_leaves_the_host(world: World, nxt: str) -> None:
    world.redeemer.sessions["c0de"] = session()
    out = await denied(world, facts(path=f"/.ssc/callback?code=c0de&next={nxt}", cookie=LOGIN))
    assert dict(out.headers)["location"] == "/"


@pytest.mark.parametrize(
    ("path", "cookie"),
    [
        ("/.ssc/callback?code=wrong", LOGIN),
        ("/.ssc/callback", LOGIN),
        ("/.ssc/callback?code=other-org", LOGIN),
        ("/.ssc/callback?code=c0de", ""),  # no login nonce: started in another browser
        ("/.ssc/callback?code=c0de", "__Host-ssc-login=" + "x" * 43),  # another login's nonce
    ],
)
async def test_a_bad_callback_is_a_page_not_a_redirect_loop(
    world: World, path: str, cookie: str
) -> None:
    world.redeemer.sessions["c0de"] = session()
    world.redeemer.sessions["other-org"] = session(org=OTHER_ORG)
    out = await denied(world, facts(path=path, cookie=cookie))
    assert (out.status, out.reason, out.body) == (400, "bad_callback", pages.LOGIN_FAILED)
    assert [v for k, v in out.headers if k == "set-cookie"] == [clear_login_cookie()]


async def test_the_last_login_nonce_is_the_one_presented(world: World) -> None:
    world.redeemer.sessions["c0de"] = session()
    cookie = f"__Host-ssc-login=old; {LOGIN}"
    out = await denied(world, facts(path="/.ssc/callback?code=c0de", cookie=cookie))
    assert out.reason == "signed_in"


async def test_a_session_from_before_a_revocation_is_sent_back_to_login(world: World) -> None:
    s = session(iat=NOW - 600)
    world.view = AccessView.from_document(
        snapshot(
            users={
                ADA: {"status": "active", "sessions_not_before": NOW - 300},
                BEN: {"status": "active"},
                CY: {"status": "deactivated"},
            }
        )
    )
    out = await denied(world, facts(cookie=world.cookie(s)))
    assert (out.status, out.reason) == (302, "revoked")
    cookies = [v for k, v in out.headers if k == "set-cookie"]
    assert cookies == [login_cookie(NONCE), clear_cookie()]
    later = session(iat=NOW - 300)
    assert isinstance(await check(world, facts(cookie=world.cookie(later))), Allow)


async def test_logout_clears_the_session_only_from_the_app_itself(world: World) -> None:
    out = await denied(world, facts(path="/.ssc/logout", cookie=world.cookie()))
    assert dict(out.headers)["location"] == "https://auth.example.test/logout"
    assert "Max-Age=0" in dict(out.headers)["set-cookie"]
    cross = await denied(world, facts(path="/.ssc/logout", sec_fetch_site="cross-site"))
    assert cross.status == 404


async def test_other_platform_paths_are_never_sent_to_the_app(world: World) -> None:
    for path in ("/.ssc/", "/.ssc/jwks.json", "/.ssc/callback/x"):
        assert (await denied(world, facts(path=path, cookie=world.cookie()))).status == 404


async def test_host_case_port_and_trailing_dot_are_normalised(world: World) -> None:
    for host in (HOST.upper(), f"{HOST}:443", f"{HOST}."):
        assert isinstance(await check(world, facts(host, cookie=world.cookie())), Allow)

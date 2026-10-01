"""Session cookies (SSC-018): sealed per host, never a Domain, at most 12 hours."""

import secrets

import pytest
from edge_world import ADA, HOST, NOW, ORG, PAY_HOST, session

from ssc_edge.session import (
    COOKIE_NAME,
    MAX_SESSION_SECONDS,
    Session,
    SessionCodec,
    clear_cookie,
    cookie_values,
    is_platform_cookie,
    set_cookie,
)

K1, K2 = secrets.token_bytes(32), secrets.token_bytes(32)


def test_a_session_opens_only_on_its_own_host_and_only_while_live() -> None:
    codec = SessionCodec({"k1": K1}, active="k1")
    s = session()
    value = codec.seal(s, HOST)
    assert codec.open(value, HOST, now=NOW) == s
    assert codec.open(value, PAY_HOST, now=NOW) is None
    assert codec.open(value, HOST, now=s.exp) is None
    assert codec.open(value, HOST, now=s.iat - 1) is None


@pytest.mark.parametrize(
    "mangle",
    [
        lambda v: v[:-2] + ("AA" if not v.endswith("AA") else "BB"),
        lambda v: v.replace("v1.", "v2.", 1),
        lambda v: v.replace(".k1.", ".k9.", 1),
        lambda v: "v1.k1.!!!",
        lambda v: "",
        lambda v: "v1",
    ],
)
def test_a_tampered_or_unknown_value_is_no_session(mangle) -> None:  # noqa: ANN001
    codec = SessionCodec({"k1": K1}, active="k1")
    assert codec.open(mangle(codec.seal(session(), HOST)), HOST, now=NOW) is None


def test_rotation_keeps_old_sessions_until_the_old_key_is_dropped() -> None:
    old = SessionCodec({"k1": K1}, active="k1").seal(session(), HOST)
    both = SessionCodec({"k1": K1, "k2": K2}, active="k2")
    assert both.open(old, HOST, now=NOW) is not None
    assert both.seal(session(), HOST).startswith("v1.k2.")
    assert SessionCodec({"k2": K2}, active="k2").open(old, HOST, now=NOW) is None


def test_sessions_are_bounded() -> None:
    with pytest.raises(ValueError, match="at most"):
        session(life=MAX_SESSION_SECONDS + 1)
    with pytest.raises(ValueError, match="usr_"):
        Session(sid="x" * 22, sub="ada@example.test", org=ORG, name="", email="", iat=1, exp=2)
    with pytest.raises(ValueError, match="32 bytes"):
        SessionCodec({"k1": b"short"}, active="k1")
    with pytest.raises(ValueError, match="no session key"):
        SessionCodec({"k1": K1}, active="k2")


def test_the_cookie_is_host_only_secure_and_http_only() -> None:
    header = set_cookie("v1.k1.abc", max_age=60)
    assert header.startswith(f"{COOKIE_NAME}=v1.k1.abc;")
    attrs = {a.strip().split("=")[0].lower() for a in header.split(";")[1:]}
    assert attrs == {"path", "max-age", "secure", "httponly", "samesite"}
    assert "Path=/" in header and "SameSite=Lax" in header
    assert "Max-Age=0" in clear_cookie()
    with pytest.raises(ValueError):
        set_cookie("a;Domain=evil", max_age=60)
    with pytest.raises(ValueError):
        set_cookie("v", max_age=MAX_SESSION_SECONDS + 1)


def test_cookie_values_and_platform_names() -> None:
    header = f"a=1; {COOKIE_NAME}=one;{COOKIE_NAME}= two ; {COOKIE_NAME}x=3"
    assert cookie_values(header) == ["one", "two"]
    assert cookie_values("") == []
    for name in (COOKIE_NAME, "__host-SSC-other", "__Secure-ssc-x", " __HOST-SSC"):
        assert is_platform_cookie(name)
    for name in ("ssc", "__Host-app", "session", ADA):
        assert not is_platform_cookie(name)

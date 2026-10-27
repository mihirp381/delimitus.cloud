"""The auth host's pages: one look, no script, the CSP unchanged."""

import pytest

from ssc_control.identity import pages

ALL = [
    pages.REFUSED,
    pages.BAD_REQUEST,
    pages.SIGNED_OUT,
    pages.DEVICE_DONE,
    pages.UNKNOWN_CLIENT,
    pages.NO_SIGN_IN,
    pages.TOO_MANY,
    pages.UNAVAILABLE,
    pages.continue_to("https://workos.test/sso/authorize?a=1&b=2"),
    pages.device_form("org_x", "ABCD-EFGH"),
    pages.agent_consent("org_x", "ABCD-EFGH", "claude-code"),
    pages.work_email("sealed"),
    pages.oauth_consent(
        client_name="Claude Code", destination="127.0.0.1", org_name="Acme", answer="tok"
    ),
]


@pytest.mark.parametrize("html", ALL)
def test_every_page_has_the_style_once_and_no_script(html: str) -> None:
    assert html.count(pages.STYLE) == 1 and html.count("<style") == 1
    assert "<script" not in html.lower()
    assert "<p class=brand>Delimitus</p>" in html


def test_the_csp_is_unchanged() -> None:
    assert pages.headers("https://workos.test", answer_to="http://127.0.0.1:*") == {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self' https://workos.test http://127.0.0.1:*; frame-ancestors 'none'; "
        "base-uri 'none'",
    }
    assert pages.headers("https://workos.test")["content-security-policy"] == (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://workos.test; "
        "frame-ancestors 'none'; base-uri 'none'"
    )

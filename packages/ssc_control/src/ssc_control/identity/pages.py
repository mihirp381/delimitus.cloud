"""The auth host's own pages. Every refused sign-in gets :data:`REFUSED`, byte for byte: the
reason is in the log and the audit, never on the page."""

from html import escape
from typing import Final


def headers(workos_base: str) -> dict[str, str]:
    """``form-action`` names WorkOS because the device form's answer redirects there."""
    return {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action 'self' {workos_base}; frame-ancestors 'none'; base-uri 'none'",
    }


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        f"<title>{escape(title)}</title><h1>{escape(title)}</h1>{body}</html>\n"
    )


REFUSED: Final = page(
    "We could not sign you in",
    "<p>Your company account could not be used to sign in here. If you think it should, ask"
    " your IT admin.</p>",
)
BAD_REQUEST: Final = page(
    "This sign-in link is not valid", "<p>Go back to the app and try again.</p>"
)
SIGNED_OUT: Final = page("Signed out", "<p>You are signed out.</p>")
DEVICE_DONE: Final = page(
    "You are signed in", "<p>Return to your terminal. You can close this window.</p>"
)


def device_form(org_id: str, user_code: str) -> str:
    code = escape(user_code)
    return page(
        "Sign in to the ssc command line",
        "<p>Continue only if you just ran <code>ssc login</code> yourself and your terminal shows"
        " this code.</p>"
        "<form method=post action='/device'>"
        f"<input type=hidden name=org value='{escape(org_id)}'>"
        f"<label>Code <input name=user_code value='{code}' autocomplete=off required></label> "
        "<button>Continue</button></form>",
    )

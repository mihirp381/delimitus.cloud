"""The auth host's own pages. Every refused sign-in gets :data:`REFUSED`, byte for byte: the
reason is in the log and the audit, never on the page. Every work email that finds no sign-in
gets :data:`NO_SIGN_IN`, byte for byte, whatever the reason (decision 029)."""

from html import escape
from typing import Final


def headers(workos_base: str, *, answer_to: str = "") -> dict[str, str]:
    """``form-action`` names WorkOS for older pages; the device and work-email forms now answer
    with :func:`continue_to`, since WorkOS redirects on to hosts no list can name. A consent page
    also names ``answer_to``, where its answer redirects (the client's redirect URI: CSP applies
    ``form-action`` to a form's redirects too)."""
    targets = f"{workos_base} {answer_to}".strip()
    return {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "x-frame-options": "DENY",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action 'self' {targets}; frame-ancestors 'none'; base-uri 'none'",
    }


def page(title: str, body: str, head: str = "") -> str:
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width'>{head}"
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


def continue_to(url: str) -> str:
    """The answer to a form whose next stop is single sign-on: a page that moves on by itself.

    A redirect would not do. CSP applies the form page's ``form-action`` to every redirect after
    the form is sent, and WorkOS redirects on to the company's identity provider, a host no list
    here can name, so Chrome and WebKit stopped there (GA-3.2, 2026-10-08). A refresh is a new
    navigation, not the form's, and needs no script."""
    target = escape(url)
    return page(
        "Continuing to your company sign-in",
        f"<p>If nothing happens, <a href='{target}'>continue</a>.</p>",
        head=f"<meta http-equiv=refresh content='0;url={target}'>",
    )


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


def agent_consent(org_id: str, user_code: str, agent: str) -> str:
    """The step before single sign-on when the login is for a coding agent (SSC-048)."""
    name = escape(agent)
    return page(
        "Let a coding agent act as you",
        f"<p>This login is for the coding agent <strong>{name}</strong>. Once you sign in, it can "
        "do what you can do in ssc for up to 12 hours: deploy to preview, roll back, read logs "
        "and ask for access. It cannot approve anything or handle secrets, and every call it "
        "makes is recorded as made by it on your behalf.</p>"
        f"<p>Continue only if you just ran <code>ssc login --agent {name}</code> yourself.</p>"
        "<form method=post action='/device'>"
        f"<input type=hidden name=org value='{escape(org_id)}'>"
        f"<input type=hidden name=user_code value='{escape(user_code)}'>"
        f"<input type=hidden name=agent value='{name}'>"
        f"<button>Let {name} act as me</button></form>",
    )


# ── OAuth (decision 029) ─────────────────────────────────────────────────────

UNKNOWN_CLIENT: Final = page(
    "This sign-in request is not valid",
    "<p>The app that sent you here is not registered, or asked to send you somewhere it did not"
    " register. Nothing was shared with it. Go back to the app and connect again.</p>",
)
NO_SIGN_IN: Final = page(
    "We couldn't find a sign-in for that email",
    "<p>Check the address and go back to try again. If it is right, ask your IT admin whether"
    " your company signs in to Delimitus.</p>",
)
TOO_MANY: Final = page(
    "Too many tries", "<p>Wait an hour, then go back to the app and connect again.</p>"
)
UNAVAILABLE: Final = page("Sign-in is unavailable", "<p>Try again in a few minutes.</p>")


def work_email(pending: str) -> str:
    """The step that finds the company's sign-in when nothing else names the org."""
    return page(
        "Sign in to Delimitus",
        "<p>Enter your work email to find your company's sign-in.</p>"
        "<form method=post action='/authorize'>"
        f"<input type=hidden name=pending value='{escape(pending)}'>"
        "<label>Work email <input type=email name=email autocomplete=email required></label> "
        "<button>Continue</button></form>",
    )


def oauth_consent(*, client_name: str, destination: str, org_name: str, answer: str) -> str:
    """A third-party client asks to act as the person; the page names who and where."""
    name, place = escape(client_name), escape(destination)
    return page(
        f"Let {client_name} act as you",
        f"<p><strong>{name}</strong> asks to use Delimitus as you in <strong>"
        f"{escape(org_name)}</strong> for up to 12 hours: see your apps and their logs, create "
        "apps, deploy to preview, roll back and ask for access. It cannot approve anything or "
        "handle secrets, and every call it makes is recorded as made by it on your behalf.</p>"
        f"<p>If you approve, you go back to <strong>{place}</strong>. The name is the app's "
        "own; approve only if you just connected it yourself.</p>"
        "<form method=post action='/authorize/consent'>"
        f"<input type=hidden name=answer_token value='{escape(answer)}'>"
        "<button name=answer value=approve>Approve</button> "
        "<button name=answer value=deny>Deny</button></form>",
    )

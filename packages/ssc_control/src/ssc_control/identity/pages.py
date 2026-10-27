"""The auth host's own pages. Every refused sign-in gets :data:`REFUSED`, byte for byte: the
reason is in the log and the audit, never on the page. Every work email that finds no sign-in
gets :data:`NO_SIGN_IN`, byte for byte, whatever the reason (decision 029).

The colours in :data:`STYLE` mirror ``console/src/tokens`` (Python has no token file)."""

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


STYLE: Final = (
    "<style>"
    ":root{color-scheme:light dark;--bg:#f5f5f7;--card:#ffffff;--line:#e3e3e8;--text:#1d1d1f;"
    "--text2:#424245;--muted:#6e6e73;--field:#86868b;--accent:#0071e3;--accent-hi:#0062c4;"
    "--on-accent:#ffffff;--ink:#0066cc;--ink-hi:#0062c4;--ring:#0071e3}"
    "@media (prefers-color-scheme:dark){:root{--bg:#0b0b0c;--card:#1d1d1f;--line:#2c2c2e;"
    "--text:#f5f5f7;--text2:#d2d2d7;--muted:#aeaeb2;--field:#aeaeb2;--accent:#2997ff;"
    "--accent-hi:#5eb0ff;--on-accent:#0b0b0c;--ink:#6eb6ff;--ink-hi:#8cc4ff;--ring:#2997ff}}"
    "*,*::before,*::after{box-sizing:border-box}"
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%;background:var(--bg)}"
    "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;"
    "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Helvetica,"
    "Arial,sans-serif;font-size:14px;line-height:1.52;letter-spacing:-0.012em;"
    "color:var(--text);background:var(--bg);-webkit-font-smoothing:antialiased}"
    "main{position:relative;display:grid;gap:16px;width:min(420px,100%);"
    "padding:32px 24px 24px;overflow:hidden;overflow-wrap:anywhere;background:var(--card);"
    "border:1px solid var(--line);border-radius:8px}"
    "main::before{content:'';position:absolute;top:0;right:0;left:0;height:2px;"
    "background:linear-gradient(90deg,#0a84ff 0%,#6e5bff 22%,#c24bf0 44%,#ff378c 64%,"
    "#ff5e3a 82%,#ff9f0a 100%)}"
    ".brand{font-size:15px;font-weight:650;letter-spacing:-0.015em;line-height:1}"
    "h1{margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',"
    "'Helvetica Neue',Helvetica,Arial,sans-serif;font-size:21px;font-weight:650;"
    "line-height:1.2;letter-spacing:-0.02em}"
    "p{margin:0}"
    "code{font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;font-size:0.92em;"
    "letter-spacing:0}"
    "a{color:var(--ink);text-decoration:underline;text-underline-offset:0.18em}"
    "a:hover{color:var(--ink-hi)}"
    "form{display:grid;gap:12px}"
    "label{display:grid;gap:4px;font-size:13px;font-weight:500;color:var(--text2)}"
    "input{width:100%;min-height:32px;padding:4px 8px;border:1px solid var(--field);"
    "border-radius:6px;font:inherit;font-size:14px;color:var(--text);background:var(--card)}"
    "button{min-height:40px;padding:0 16px;border:1px solid var(--accent);border-radius:6px;"
    "font:inherit;font-weight:500;color:var(--on-accent);background:var(--accent);"
    "cursor:pointer}"
    "button:hover{background:var(--accent-hi);border-color:var(--accent-hi)}"
    "button[value=deny]{color:var(--text);background:var(--card);border-color:var(--line)}"
    "button[value=deny]:hover{background:var(--bg)}"
    ":focus-visible{outline:2px solid var(--ring);outline-offset:2px}"
    "input:focus-visible{outline-offset:0;border-color:var(--ring)}"
    "</style>"
)


def page(title: str, body: str, head: str = "") -> str:
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width'>{head}"
        f"<title>{escape(title)}</title>{STYLE}"
        f"<main><p class=brand>Delimitus</p><h1>{escape(title)}</h1>{body}</main></html>\n"
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

"""The gateway's own answers. Each is one constant, so two refusals of one kind are identical
byte for byte: an app the caller may not reach is answered exactly like a host that does not
exist (SSC-018 done-when)."""

from typing import Final

HEADERS: Final = (
    ("content-type", "text/html; charset=utf-8"),
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
)
CLIENT_HEADERS: Final = (*(name for name, _ in HEADERS), "location", "set-cookie")
"""Every header a gateway answer may carry to the browser; Envoy passes these and no others."""


def _page(title: str, text: str, head: str = "") -> bytes:
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width'>{head}"
        f"<title>{title}</title><h1>{title}</h1><p>{text}</p></html>\n"
    ).encode()


NOT_FOUND: Final = _page(
    "Not found", "There is no app at this address, or you do not have access to it."
)
REFUSED: Final = _page("Request refused", "This request was refused.")
TOO_LARGE: Final = _page("Request too large", "The request body is too large.")
LOGIN_FAILED: Final = _page(
    "Sign-in did not finish",
    "This sign-in link has expired or was opened in another browser. Go"
    " back to the app and try again.",
)
UNAVAILABLE: Final = _page("Unavailable", "This app cannot be reached right now. Try again soon.")
WAKE_RETRY_SECONDS: Final = 2
WAKING: Final = _page(
    "Waking up",
    "This app was asleep and is starting. The page will load by itself in a few seconds.",
    f"<meta http-equiv=refresh content={WAKE_RETRY_SECONDS}>",
)
"""A browser page load that the app has not answered within 2 seconds (``ssc_edge.envoy``)."""

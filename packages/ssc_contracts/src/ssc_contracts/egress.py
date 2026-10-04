"""Outbound internet through the cell's egress proxy (SSC-053).

One proxy per customer cell, at the reserved internal address, leaving through Cloud NAT with
the cell's fixed IP. An app reaches it as an explicit ``CONNECT`` proxy: ``HTTPS_PROXY`` is a
URL carrying the app environment's own credential and ``NODE_USE_ENV_PROXY=1`` makes Node's
``fetch`` use it. Only a tunnel to port ``TUNNEL_PORT`` of a host on the org's allowlist opens;
raw IP addresses, other ports, UDP, QUIC and IPv6 never do.

An allowlist entry is a host name or a ``*.`` pattern. The wildcard stands for exactly one label:
``*.stripe.com`` matches ``api.stripe.com``, never ``stripe.com`` or ``a.b.stripe.com``. The
control plane checks entries with :func:`host_pattern_problem`; the proxy renders each one with
:func:`authority_regex`, so both read a pattern the same way.

A credential's user is ``<env_id>.<credential_id>`` and its token a random secret; the proxy
holds only :func:`token_digest` of the token, in the form Envoy's ``htpasswd`` takes. An
environment has at most ``MAX_CREDENTIALS`` at once, so a new one is valid before the old goes.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Final

from ssc_contracts.app_env import HTTPS_PROXY, NO_PROXY, NODE_USE_ENV_PROXY

PROXY_PORT: Final = 3128
TUNNEL_PORT: Final = 443
"""The only port a tunnel may open to. It keeps plain HTTP, and so the metadata server, out."""
DRAIN_SECONDS: Final = 5
"""How long an open tunnel lasts after the allowlist or a credential changes."""
MAX_CREDENTIALS: Final = 2
MAX_HOSTS: Final = 200
NO_PROXY_VALUE: Final = (
    "localhost,127.0.0.1,169.254.169.254,metadata.google.internal,.internal,.run.app,"
    ".googleapis.com"
)
"""Names an app reaches without the proxy: its own instance, the metadata server, Google's APIs
on Private Google Access and the cell's own services."""
PLAIN_ENV: Final = {NODE_USE_ENV_PROXY: "1", NO_PROXY: NO_PROXY_VALUE}
"""Plain variables every environment with outbound hosts gets beside ``HTTPS_PROXY``."""
SECRETS: Final = (HTTPS_PROXY,)
"""The secret the cell agent writes for an environment's proxy credential."""
CREDENTIAL_ID: Final = re.compile(r"[a-z0-9]{12}")
DIGEST: Final = re.compile(r"[A-Za-z0-9+/]{27}=")
"""Base64 of a SHA-1 digest."""

_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_LABEL_RE2: Final = "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz0123456789"


def host_pattern_problem(pattern: str) -> str | None:
    """Why ``pattern`` is not an allowlist entry, or None. A lower-case DNS name of at least two
    labels, optionally ``*.`` and then at least two labels; no IP address, port or scheme."""
    name = pattern.removeprefix("*.")
    labels = name.split(".")
    checks = (
        ("://" in pattern or "/" in pattern, "write the host name only, without a scheme or path"),
        (
            pattern.startswith("[") or ":" in pattern,
            "write the host name only: no port, and no IP address",
        ),
        (pattern != pattern.lower(), "write host names in lower case"),
        (len(pattern) > 253, "a host name is at most 253 characters"),  # noqa: PLR2004  (DNS)
        ("*" in name, "a wildcard is only '*.' at the start, standing for one label"),
        (
            len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels),  # noqa: PLR2004
            "must be a DNS host name such as api.example.com, or *.example.com",
        ),
        (not labels[-1][:1].isalpha(), "IP addresses are never allowed; name the host"),
    )
    return next((problem for failed, problem in checks if failed), None)


def pattern_matches(pattern: str, host: str) -> bool:
    """True when ``host`` (a name, no port) is one ``pattern`` allows."""
    host = host.lower().rstrip(".")
    if not pattern.startswith("*."):
        return host == pattern
    first, _, rest = host.partition(".")
    return rest == pattern[2:] and _LABEL.fullmatch(first) is not None


def authority_regex(pattern: str) -> str:
    """The RE2 regex for a ``CONNECT`` request's authority, ``host:443``, that ``pattern`` allows.
    Case-insensitive, as DNS is. The wildcard becomes one label; every other character is a
    letter, digit, ``-`` or a dot."""
    name = pattern.removeprefix("*.")
    body = name.replace(".", "\\.")
    if pattern.startswith("*."):
        body = f"{_LABEL_RE2}\\.{body}"
    return f"(?i)^{body}:{TUNNEL_PORT}$"


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """A destination IT often allows. ``high_risk`` marks a host data can leave by in bulk: a
    public AI API or a file-sharing site; an admin is warned before adding it."""

    host: str
    purpose: str
    high_risk: bool = False
    note: str = ""


CATALOGUE: Final = (
    CatalogueEntry("api.stripe.com", "Stripe payments"),
    CatalogueEntry("api.github.com", "GitHub API"),
    CatalogueEntry("hooks.slack.com", "Slack incoming webhooks"),
    CatalogueEntry("slack.com", "Slack Web API"),
    CatalogueEntry("api.sendgrid.com", "SendGrid email"),
    CatalogueEntry("api.twilio.com", "Twilio SMS and voice"),
    CatalogueEntry("*.atlassian.net", "Jira and Confluence Cloud sites"),
    CatalogueEntry("graph.microsoft.com", "Microsoft 365 (Graph API)"),
    CatalogueEntry("login.microsoftonline.com", "Microsoft sign-in for Graph API tokens"),
    CatalogueEntry(
        "api.openai.com", "OpenAI API", True, "a public AI service: company data sent leaves"
    ),
    CatalogueEntry(
        "api.anthropic.com", "Anthropic API", True, "a public AI service: company data sent leaves"
    ),
    CatalogueEntry(
        "api.dropboxapi.com", "Dropbox API", True, "file sharing: files can leave in bulk"
    ),
    CatalogueEntry(
        "content.dropboxapi.com", "Dropbox file transfer", True, "file sharing: files can leave"
    ),
    CatalogueEntry("wetransfer.com", "WeTransfer", True, "file sharing: files can leave in bulk"),
)


def catalogue_entry(host: str) -> CatalogueEntry | None:
    return next((e for e in CATALOGUE if e.host == host), None)


def refusal_message(host: str) -> str:
    """What an app gets when the proxy refuses a tunnel to ``host``."""
    return (
        f"The cell's egress proxy refused {host}: only HTTPS (CONNECT to port {TUNNEL_PORT}) to "
        "a host on your organisation's allowlist goes out, and never to a raw IP address. To "
        "request it, declare the host under [egress] hosts in ssc.toml (a production deploy "
        "then asks an org admin to approve it), or ask an org admin to add it to the allowlist."
    )


def new_credential() -> tuple[str, str]:
    """A fresh ``(credential_id, token)``."""
    credential_id = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return credential_id, secrets.token_urlsafe(32)


def credential_user(env_id: str, credential_id: str) -> str:
    return f"{env_id}.{credential_id}"


def token_digest(token: str) -> str:
    """Base64 of the token's SHA-1, as Envoy's ``htpasswd`` ``{SHA}`` form holds it. The token is
    256 random bits, so a fast digest does not weaken it."""
    return base64.b64encode(hashlib.sha1(token.encode()).digest()).decode()  # noqa: S324


def proxy_url(env_id: str, credential_id: str, token: str, address: str) -> str:
    """The ``HTTPS_PROXY`` value: the proxy's address with the credential in it."""
    return f"http://{credential_user(env_id, credential_id)}:{token}@{address}:{PROXY_PORT}"

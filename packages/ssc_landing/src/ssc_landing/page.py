"""The landing page as served: its bytes, and the headers that hold it to its own origin.

The apex is the same site as ``auth.`` and ``console.``, so the page loads nothing from another
origin and runs only the code it ships. The content security policy allows exactly the inline
``<script>`` and ``<style>`` blocks of ``landing/index.html``, by hash, computed here when the page
is loaded; a page that needs anything else (an attribute on those tags, a ``style=`` attribute,
an inline event handler) is refused at load, because the browser would refuse it anyway.
"""

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_PAGE_BYTES: Final = 400 * 1024
"""SSC-065: the page weighs under 400 KB, bitmaps included."""

SECURITY_HEADERS: Final = {
    # No preload and no includeSubDomains until every delimitus.com host serves HTTPS.
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

PLAIN_CSP: Final = "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
"""For every answer that is not the page: it runs nothing and loads nothing."""

_INLINE: Final = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)
_TAG_WITH_ATTRIBUTES: Final = re.compile(r"<(?:script|style)\s", re.IGNORECASE)
_STYLE_ATTRIBUTE: Final = re.compile(r"\sstyle\s*=", re.IGNORECASE)
_HANDLER_ATTRIBUTE: Final = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
_EXTERNAL: Final = re.compile(r"""(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//""", re.I)


class PageError(ValueError):
    """The page breaks a rule the browser or SSC-065 would enforce."""


@dataclass(frozen=True, slots=True)
class Page:
    body: bytes
    etag: str
    csp: str


def _source_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def content_security_policy(html: str) -> str:
    """The policy for ``html``: its inline blocks by hash, data: images, same-origin posts."""
    scripts = [_source_hash(m[2]) for m in _INLINE.finditer(html) if m[1] == "script"]
    styles = [_source_hash(m[2]) for m in _INLINE.finditer(html) if m[1] == "style"]
    return "; ".join(
        [
            "default-src 'none'",
            "script-src " + (" ".join(scripts) or "'none'"),
            "style-src " + (" ".join(styles) or "'none'"),
            "img-src data:",
            "connect-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        ]
    )


def check_page(html: str, size: int) -> None:
    """Refuse a page the policy would break, or that reaches another origin."""
    if size > MAX_PAGE_BYTES:
        raise PageError(f"the page is {size} bytes; the limit is {MAX_PAGE_BYTES}")
    if _TAG_WITH_ATTRIBUTES.search(html):
        raise PageError("a <script> or <style> tag has attributes; only bare inline blocks run")
    outside = _INLINE.sub("", html)
    if _STYLE_ATTRIBUTE.search(outside):
        raise PageError("a style= attribute would be blocked by the content security policy")
    if _HANDLER_ATTRIBUTE.search(outside):
        raise PageError("an inline event handler would be blocked by the content security policy")
    if _EXTERNAL.search(outside):
        raise PageError("the page links a resource or action on another origin")


def load_page(path: Path) -> Page:
    body = path.read_bytes()
    html = body.decode("utf-8")
    check_page(html, len(body))
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    return Page(body=body, etag=etag, csp=content_security_policy(html))

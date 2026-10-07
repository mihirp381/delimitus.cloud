"""SSC-065: the page in ``landing/`` keeps the moat rule, its claims, its policy and its weight."""

import base64
import json
import re
from pathlib import Path

import pytest

from ssc_landing.page import (
    MAX_PAGE_BYTES,
    PageError,
    check_page,
    content_security_policy,
    load_page,
)

LANDING = Path(__file__).resolve().parents[3] / "landing"
HTML = (LANDING / "index.html").read_text(encoding="utf-8")
DATA_URI = re.compile(r"data:image/[a-z+]+[;,][A-Za-z0-9+/=;,%'.:\-]*")
STYLE = re.compile(r"<style>.*?</style>", re.DOTALL)


def _text_without_styles() -> str:
    """What a reader or a crawler sees: markup, copy and script strings, without CSS or bitmaps."""
    return DATA_URI.sub("", STYLE.sub("", HTML))


def _denylist() -> list[str]:
    lines = (LANDING / "denylist.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _term(term: str) -> re.Pattern[str]:
    start = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
    end = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
    return re.compile(start + re.escape(term) + end, re.IGNORECASE)


def test_the_page_loads_under_its_own_policy_and_weight() -> None:
    page = load_page(LANDING / "index.html")
    assert len(page.body) < MAX_PAGE_BYTES
    assert page.csp.startswith("default-src 'none'; script-src 'sha256-")
    assert "form-action 'self'" in page.csp
    assert "frame-ancestors 'none'" in page.csp
    assert "unsafe-inline" not in page.csp


def test_every_inline_block_is_allowed_by_hash() -> None:
    blocks = re.findall(r"<(script|style)>", HTML)
    csp = content_security_policy(HTML)
    assert csp.count("'sha256-") == len(blocks) == HTML.count("</script>") + HTML.count("</style>")


def test_the_denylist_names_what_the_ticket_bans() -> None:
    terms = {t.lower() for t in _denylist()}
    banned = {"small software", "ristretto", "cell", "snapshot", "gateway", "identity note"}
    banned |= {"cell agent", "egress proxy", "envoy", "railpack", "procrastinate", "workos"}
    banned |= {"gitleaks", "ssc-", "$50"}
    assert banned <= terms


def test_no_denylisted_word_is_on_the_page() -> None:
    text = _text_without_styles()
    found = [t for t in _denylist() if _term(t).search(text)]
    assert found == []


def test_no_dollar_figure_outside_the_cost_section() -> None:
    """Our price is never on the page; the only figures are the do-it-yourself costs."""
    outside = re.sub(r'<section[^>]*id="cost".*?</section>', "", _text_without_styles(), flags=re.S)
    outside = re.sub(r"/\* calc:start \*/.*?/\* calc:end \*/", "", outside, flags=re.S)
    assert re.findall(r"\$\s?\d", outside) == []
    assert "Pricing is agreed with each pilot" in HTML


def test_nothing_is_loaded_from_another_origin() -> None:
    text = _text_without_styles()
    assert re.findall(r"""(?:src|href|action|srcset)\s*=\s*["']?(?:https?:)?//""", text) == []
    assert "@import" not in HTML
    assert re.findall(r"url\(\s*['\"]?(?:https?:)?//", HTML) == []
    assert "fonts.googleapis" not in HTML


def test_the_horizon_is_the_only_bitmap() -> None:
    uris = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", HTML)
    horizon = (LANDING / "art" / "fade.png").read_bytes()
    assert [base64.b64decode(u) for u in uris] == [horizon]


def test_claim_ids_on_the_page_match_the_register() -> None:
    on_page: set[str] = set()
    for ids in re.findall(r'data-claim="([^"]+)"', HTML):
        on_page |= set(ids.split())
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| `([a-z-]+)` \|", register, flags=re.M))
    assert on_page == listed


def test_the_no_script_table_shows_the_default_vector() -> None:
    vectors = json.loads((LANDING / "calculator_vectors.json").read_text(encoding="utf-8"))
    default = vectors["vectors"][0]
    for name in ("apps", "rate", "setup", "perApp", "upkeep"):
        field = re.search(rf'<input[^>]*value="([\d.]+)" data-in="{name}"', HTML)
        assert field is not None, name
        assert float(field[1]) == default["in"][name]
    for key, dollars in default["out"].items():
        cell = re.search(rf'data-out="{key}">\$([\d,]+)<', HTML)
        assert cell is not None, key
        assert int(cell[1].replace(",", "")) == dollars


def test_the_cost_card_shows_the_default_total() -> None:
    vectors = json.loads((LANDING / "calculator_vectors.json").read_text(encoding="utf-8"))
    total = vectors["vectors"][0]["out"]["total"]
    card = re.search(r'id="trioDiy">\$([\d,]+)<', HTML)
    assert card is not None
    assert int(card[1].replace(",", "")) == total


def test_the_agent_is_never_shown_approving() -> None:
    """An agent asks; another admin approves (SSC-045, SSC-048). The copy must not say otherwise."""
    text = _text_without_styles()
    assert "waiting for IT" in text
    assert "an agent can never approve a request" in text
    assert not re.search(r"(?i)agent[^.<]{0,40}\bapproved\b", text)


def test_the_calculator_constants_match_the_sources_shown() -> None:
    assert "var CLOUD_FIXED_CENTS = 12953;" in HTML
    assert "var CLOUD_PER_APP_CENTS = 986;" in HTML
    assert "A month costs $129.53 plus $9.86 for each app" in HTML
    assert "one instance kept warm so the first visit is not slow (minimum instances 1" in HTML
    assert "$6.57 for CPU and $3.29 for memory" in HTML
    assert round(6.57 + 3.29, 2) == 9.86
    parts = [18.25, 102.02, 4.67, 2.50, 1.23, 0.36, 0.50]
    assert round(sum(parts), 2) == 129.53


def test_the_page_tells_visitors_what_is_kept() -> None:
    assert "12 months" in HTML
    assert 'href="mailto:privacy@delimitus.com"' in HTML
    assert 'action="/pilot-request"' in HTML
    assert 'name="website"' in HTML


# Text colour pairs, foreground on background. The gradient text uses ``--spectrum-text``.
def _tokens() -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\n\}", HTML, flags=re.S)
    assert root is not None
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", root[1]))


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _pairs() -> list[tuple[str, str]]:
    t = _tokens()
    grounds = [t["ground"], t["ground-alt"]]
    text = ["ink", "ink-2", "ink-3", "accent-ink", "live", "caution", "danger"]
    text += ["t-blue", "t-violet", "t-purple", "t-magenta", "t-orange"]
    spectrum = re.search(r"--spectrum-text:\s*linear-gradient\(([^;]*)\);", HTML)
    assert spectrum is not None
    stops = re.findall(r"#[0-9A-Fa-f]{6}", spectrum[1])
    pairs = [(t[name], g) for name in text for g in grounds] + [
        (s, g) for s in stops for g in grounds
    ]
    pairs += [
        (t["live"], t["live-soft"]),
        (t["caution"], t["caution-soft"]),
        (t["danger"], t["danger-soft"]),
        (t["t-violet"], t["violet-soft"]),
        (t["accent-ink"], t["accent-soft"]),
        (t["ink-2"], t["accent-soft"]),
        ("#FFFFFF", t["accent"]),
        ("#FFFFFF", t["accent-hi"]),
        ("#FFFFFF", t["t-blue"]),
        ("#FFFFFF", t["t-violet"]),
        ("#FFFFFF", t["t-orange"]),
    ]
    # The terminal, on its own ground and under a highlighted line (8% white over #1D1D1F).
    for fg in ("#FFFFFF", "#C7C7CC", "#7EE2A0", "#8CC4FF", "#AEAEB2", "#FFD479"):
        pairs += [(fg, "#1D1D1F"), (fg, "#2B2B2D")]
    pairs.append(("#C7C7CC", "#2C2C2E"))
    return pairs


@pytest.mark.parametrize(("fg", "bg"), _pairs())
def test_text_contrast_is_at_least_4_5_to_1(fg: str, bg: str) -> None:
    assert _contrast(fg, bg) >= 4.5


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ('<script src="/x.js"></script>', "has attributes"),
        ("<style media=print>a{}</style>", "has attributes"),
        ('<p style="color:red">x</p>', "style="),
        ('<button onclick="go()">x</button>', "event handler"),
        ('<img src="https://example.com/a.png">', "another origin"),
        ('<link rel="stylesheet" href="//example.com/a.css">', "another origin"),
        ('<form action="https://example.com/post"></form>', "another origin"),
    ],
)
def test_a_page_the_policy_would_break_is_refused(page: str, message: str) -> None:
    with pytest.raises(PageError, match=message):
        check_page(page, len(page))


def test_a_page_over_the_weight_limit_is_refused() -> None:
    with pytest.raises(PageError, match="limit"):
        check_page("<p>x</p>", MAX_PAGE_BYTES + 1)


def test_inline_blocks_and_mailto_links_are_allowed() -> None:
    page = '<style>a{}</style><script>1</script><a href="mailto:x@example.com">x</a><a href="#a">'
    check_page(page, len(page))

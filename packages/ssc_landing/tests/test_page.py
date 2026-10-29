"""SSC-065: the page in ``landing/`` keeps the moat rule, its claims, its policy and its weight."""

import base64
import json
import re
from pathlib import Path

import pytest

from ssc_landing.page import (
    MAX_PAGE_BYTES,
    SIGN_IN,
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
    assert text.count(f'href="{SIGN_IN}"') == 1
    text = text.replace(f'href="{SIGN_IN}"', "")
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
    """An agent asks; an org admin approves (SSC-045, SSC-048). The copy must not say otherwise."""
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
        (t["ink"], t["orange-soft"]),
        (t["ink-2"], t["orange-soft"]),
        (t["t-blue"], t["orange-soft"]),
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
    # The pipeline's pills: 5% white and 12% #7EE2A0 over the terminal's ground.
    pairs += [("#AEAEB2", "#28282A"), ("#7EE2A0", "#29352E")]
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
        ('<a href="https://console.delimitus.com/other">x</a>', "another origin"),
        ('<img src="https://console.delimitus.com/x.png">', "another origin"),
        ('<form action="https://console.delimitus.com/login"></form>', "another origin"),
        ('<a href="https://console.delimitus.com/login/x">x</a>', "another origin"),
    ],
)
def test_a_page_the_policy_would_break_is_refused(page: str, message: str) -> None:
    with pytest.raises(PageError, match=message):
        check_page(page, len(page))


def test_a_page_over_the_weight_limit_is_refused() -> None:
    with pytest.raises(PageError, match="limit"):
        check_page("<p>x</p>", MAX_PAGE_BYTES + 1)


def test_the_sign_in_link_is_the_one_link_to_another_origin() -> None:
    assert SIGN_IN == "https://console.delimitus.com/login"
    page = f'<a class="btn" href="{SIGN_IN}">Sign in</a><a href="{SIGN_IN}">Sign in</a>'
    check_page(page, len(page))


def test_inline_blocks_and_mailto_links_are_allowed() -> None:
    page = '<style>a{}</style><script>1</script><a href="mailto:x@example.com">x</a><a href="#a">'
    check_page(page, len(page))


def test_the_egress_example_does_not_promise_a_per_app_allowlist() -> None:
    """The allowlist is org-wide: an app reaches every host its admin allowed (egress contract)."""
    text = _text_without_styles()
    assert "Reaches only the internet hosts your admin allowed" in text
    assert "tracking site and nothing else" not in text


def test_the_example_share_applies_only_after_both_approvals() -> None:
    """A data-connected production app: the share and the audience widening are both approved."""
    convo = re.search(r'<ol class="convo".*?</ol>', HTML, flags=re.S)
    assert convo is not None
    events = re.findall(r'<li class="convo__event"[^>]*>(.*?)</li>', convo[0])
    approved = [e for e in events if "Admin approved" in e]
    assert len(approved) == 2
    assert "share request" in approved[0]
    assert "Widening the audience" in approved[1]
    assert events[-1] == approved[1]
    assert [e for e in events if "Shared with" in e] == [approved[1]]
    assert "One prompt and two approvals later" in HTML
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    assert "F21 fixed in code on 2026-10-20" in register


TOOLS = (
    "Claude Code and Codex deploy directly today; Cursor through the same MCP server; "
    "Lovable and Replit through a GitHub push."
)


def test_only_the_tools_proven_live_are_shown_deploying_directly() -> None:
    assert TOOLS in HTML
    direct = re.search(
        r'Deploys directly today</h3>\s*<ul class="works__list"[^>]*>(.*?)</ul>', HTML
    )
    assert direct is not None
    assert re.findall(r"<li>(.*?)</li>", direct[1]) == ["Claude Code", "Codex"]
    chips = re.search(r'<span class="hiw__chips"[^>]*>(.*?)</span>\s*</button>', HTML, flags=re.S)
    assert chips is not None
    assert "Cursor" not in chips[1]
    assert "Claude Code, Codex and Cursor deploy directly" not in HTML
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    row = re.search(r"^\| `tools` \|.*$", register, flags=re.M)
    assert row is not None
    assert TOOLS in row[0]
    assert "not proven end to end" in row[0]


def test_every_approval_names_who_approves() -> None:
    text = _text_without_styles()
    assert not re.search(r"(?i)\b(after|once|until) (an |the )?approv", text)
    assert not re.search(
        r"(?i)\b(once|after) IT approved|connections IT approved|lead approves", text
    )
    assert "Only an admin can approve" not in text
    assert not re.search(r"(?i)\b(another|a second|a different) admin", text)
    assert text.lower().count("an admin of your company") == 7


REMOVED = (
    "Private pilot. Not generally available",
    "Illustration ·",
    "Example data",
    "example data",
    "Product names belong to their owners",
    "Your data connections",
    "The rest is ours.",
    "Example Freight",
    "Sample Linehaul",
    "Demo Carriers",
    "Placeholder Haulage",
    "The expensive part isn’t running the app.",
    "It’s everything around it.",
    'id="limits"',
    "Opinionated on purpose",
    "No custom domains",
    "No inbound webhooks",
    "Measured: one prompt to a running preview",
    'id="race"',
    "data-race",
    'id="bars"',
)
ADDED = (
    "Secure data connections",
    "<span>“Deploy this on Delimitus.”</span> <span>Say it once. We run everything behind it."
    " IT approves what matters.</span>",
    "Northline Freight",
    "Cascade Linehaul",
    "Redwood Carriers",
    "Pioneer Haulage",
    'Building apps with AI is <span class="grad">easy and fun.</span>',
    "Deploying and maintaining hundreds of them is hard and expensive.",
    "<span>Preview in minutes.</span>",
)


def test_the_refreshed_copy_replaces_the_old() -> None:
    assert [s for s in REMOVED if s in HTML] == []
    assert [s for s in ADDED if s not in HTML] == []
    og = re.search(r'<meta property="og:description" content="([^"]*)">', HTML)
    assert og is not None
    assert "Private pilot" not in og[1]
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    assert "| `names` |" not in register


def _bound(name: str) -> str:
    found = re.search(rf'data-bind="{name}"[^>]*>(.*?)</(?:span|ul)>', HTML, flags=re.S)
    assert found is not None, name
    return found[1]


def test_the_case_panel_shows_the_warehouse_asks_without_javascript() -> None:
    wh = re.search(r"wh:\s*\{(.*?)\n\s*\},?\n", HTML, flags=re.S)
    assert wh is not None
    simple = re.search(r'simple:\s*\{\s*ask:\s*"([^"]+)",\s*get:\s*\[([^\]]*)\]', wh[1])
    big = re.search(r'ask:\s*"([^"]+)",\s*tool:\s*"([^"]+)",\s*get:\s*\[([^\]]*)\]', wh[1])
    assert simple is not None and big is not None
    assert _bound("askSimple") == simple[1]
    assert re.findall(r"<li>(.*?)</li>", _bound("getSimple")) == re.findall(r'"([^"]+)"', simple[2])
    assert _bound("ask") == big[1]
    assert _bound("tool") == big[2]
    assert re.findall(r"<li>(.*?)</li>", _bound("get")) == re.findall(r'"([^"]+)"', big[3])
    assert simple[1] == "A page that shows which doors are free right now."
    assert "Company login on, with no setup" in simple[2]


def _curve() -> str:
    found = re.search(r'<div class="curve" id="curve">.*?\n      </div>\n', HTML, flags=re.S)
    assert found is not None
    return found[0]


def _sources() -> str:
    found = re.search(r'<details class="src" id="sources">.*?</details>', HTML, flags=re.S)
    assert found is not None
    return found[0]


def test_the_chart_citations_are_in_the_sources_details() -> None:
    model = re.search(
        r'<details class="src model" id="model">.*?</details>\s*</div>\s*</details>', HTML, re.S
    )
    assert model is not None and _sources() in model[0]
    notes = dict(re.findall(r'<li data-claim="([a-z-]+)">([^<]+)</li>', _sources()))
    marmelab = notes["src-marmelab"]
    assert "Marmelab blog, 15 October 2026" in marmelab
    assert "Hours at 40 a week and 160 a person-month: 80 + 1–2 × 160 = 240–400 h." in marmelab
    assert "marmelab.com/blog/2026/10/15/building-your-own-ai-platform.html" in marmelab
    forrester = notes["src-forrester-okta"]
    assert "commissioned by Okta, July 2026" in forrester
    assert "tei.forrester.com/go/Okta/IdentitySecurityFabric/index.html?lang=en-us" in forrester
    metricnet = notes["src-metricnet"]
    assert "desktop-support service request of any kind, not only access" in metricnet
    assert "brighttalk.com/resource/core/447335/" in metricnet
    assert "Our estimate: about 15 minutes" in notes["approve-estimate"]
    assert "footnote" not in HTML


def _half_up(v: float) -> int:
    return int(v + 0.5)


def _line() -> tuple[dict[str, int], int, int]:
    block = re.search(r"/\* line:start \*/(.*?)/\* line:end \*/", HTML, flags=re.S)
    assert block is not None
    consts = re.search(r"var LINE = \{([^}]*)\}", block[1])
    assert consts is not None
    defaults = {k: int(v) for v, k in re.findall(r'value="(\d+)" data-in="(\w+)"', HTML)}
    return (
        {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", consts[1])},
        defaults["setup"],
        defaults["rate"],
    )


def _ends() -> dict[str, str]:
    c, setup, rate = _line()
    diy = setup + c["requests"] * c["diyMin"] / 60
    dlm = c["dlmSetup"] + c["requests"] * c["dlmMin"] / 60
    return {
        "diy": f"Yourself {_half_up(diy)} h · ${_half_up(diy * rate):,}",
        "dlm": f"Delimitus {_half_up(dlm)} h · ${_half_up(dlm * rate):,}",
        "gap": f"{_half_up(diy - dlm)} h of IT time saved",
        "aria": f"IT hours over 0 to {c['requests']} requests: doing it yourself reaches"
        f" {_half_up(diy)} h (${_half_up(diy * rate):,}), Delimitus {_half_up(dlm)} h"
        f" (${_half_up(dlm * rate):,}), {_half_up(diy - dlm)} h less.",
    }


def test_the_cost_line_is_built_from_the_sourced_constants() -> None:
    consts, setup, rate = _line()
    assert consts == {"diyMin": 98, "dlmMin": 15, "dlmSetup": 10, "requests": 200}
    assert (setup, rate) == (240, 68)
    assert 'var setupH = given("setup"), rate = given("rate");' in HTML
    assert "var cl = costLine(setupH, rate);" in HTML


def test_the_cost_line_end_values() -> None:
    e = _ends()
    assert e["diy"] == "Yourself 567 h · $38,533"
    assert e["dlm"] == "Delimitus 60 h · $4,080"
    assert e["gap"] == "507 h of IT time saved"


def test_the_no_script_chart_matches_the_computed_values() -> None:
    card, e = _curve(), _ends()
    labels = [
        re.sub(r"<[^>]+>", "", t)
        for t in re.findall(r'<text class="curve__lab"[^>]*>(.*?)</text>', card)
    ]
    assert labels == [e["diy"], e["dlm"]]
    assert re.findall(r'<text class="curve__save"[^>]*>([^<]*)</text>', card) == [e["gap"]]
    assert re.findall(r'<text class="curve__n"[^>]*>([^<]*)</text>', card) == ["200 requests"]
    svg = re.search(
        r'<svg class="curve__svg" role="img" aria-label="([^"]*)" viewBox="0 0 \d+ \d+">', card
    )
    assert svg is not None and svg[1] == e["aria"]
    base = re.search(r'<line class="curve__base" x1="[\d.]+" x2="[\d.]+" y1="([\d.]+)"', card)
    top = re.search(r'y1="([\d.]+)" y2="[\d.]+"></line><text[^>]*>600</text>', card)
    assert base is not None and top is not None
    y0, y600 = float(base[1]), float(top[1])

    def hours(y: str | float) -> float:
        return 600 * (y0 - float(y)) / (y0 - y600)

    consts, setup, rate = _line()
    ends = {
        k: re.search(rf'curve__ln--{k}" d="M[\d.]+ ([\d.]+)L[\d.]+ ([\d.]+)"', card)
        for k in ("diy", "dlm")
    }
    diy, dlm = ends["diy"], ends["dlm"]
    assert diy is not None and dlm is not None
    assert abs(hours(diy[1]) - setup) < 0.5
    assert abs(hours(diy[2]) - (setup + 200 * consts["diyMin"] / 60)) < 0.5
    assert abs(hours(dlm[1]) - consts["dlmSetup"]) < 0.5
    assert abs(hours(dlm[2]) - (consts["dlmSetup"] + 200 * consts["dlmMin"] / 60)) < 0.5
    for k in (10, 20, 30, 40):
        tick = re.search(rf'<text x="[\d.]+" y="([\d.]+)">\${k}k</text>', card)
        assert tick is not None
        assert abs(hours(float(tick[1]) - 4) - k * 1000 / rate) < 0.5
    assert '<figure class="curve__fig" data-claim="cost-line">' in card
    assert not re.search(r"\s(style|on[a-z]+)=", card)
    for gone in ("bars", "<sup", "<ol", "<li>", "<li ", "http", "caution", "race"):
        assert gone not in card
    end = re.search(r'<p class="curve__end" data-claim="approve-estimate">(.*?)</p>', card)
    assert end is not None
    assert (
        end[1]
        == '~15 min is our estimate. <a href="#model" id="toSources">Sources and method</a> ↓'
    )


def test_the_cost_line_claim_shows_its_arithmetic() -> None:
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    row = re.search(r"^\| `cost-line` \|.*$", register, flags=re.M)
    assert row is not None
    for cited in (
        "src-marmelab",
        "src-forrester-okta",
        "src-metricnet",
        "approve-estimate",
        "econ-labour",
    ):
        assert f"`{cited}`" in row[0]
    for step in (
        "240 + 200 × 98 / 60 = 566.7 h",
        "$38,533",
        "10 + 200 × 15 / 60 = 60 h",
        "$4,080",
        "506.7 h",
    ):
        assert step in row[0]


def test_the_approval_time_is_our_estimate() -> None:
    note = re.search(r'<li data-claim="approve-estimate">([^<]+)</li>', HTML)
    assert note is not None
    assert "Our estimate: about 15 minutes for IT to review and approve one request" in note[1]
    assert "~15 min is our estimate." in _curve()
    register = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
    assert "MEASURE" not in HTML
    assert "MEASURE" not in register
    assert "18 to 24 min" in register
    assert "18 to 24" not in HTML

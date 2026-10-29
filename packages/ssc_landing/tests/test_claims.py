"""M-1: every claim on the page is registered, covered and free of absolute wording."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

LANDING = Path(__file__).resolve().parents[3] / "landing"
HTML = (LANDING / "index.html").read_text(encoding="utf-8")
REGISTER = (LANDING / "CLAIMS.md").read_text(encoding="utf-8")
CLAIM_ID = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
TEXT_TAGS = frozenset({"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"})
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
SVG = re.compile(r"<svg\b.*?</svg>", re.DOTALL)
STYLE = re.compile(r"<style>.*?</style>", re.DOTALL)


class _Claims(HTMLParser):
    """Collects claim ids and the text elements of ``data-claims`` regions that carry none."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.uncovered: list[str] = []
        self._open: list[tuple[str, bool, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        claim = a.get("data-claim")
        if claim is not None:
            self.ids += claim.split() or [""]
        if tag in VOID:
            return
        region = "data-claims" in a or any(r for _, r, _ in self._open)
        covered = claim is not None or any(c for _, _, c in self._open)
        if region and tag in TEXT_TAGS and not covered:
            if "label" not in (a.get("class") or "").split():
                self.uncovered.append(f"<{tag}> on line {self.getpos()[0]}")
        self._open.append((tag, region, covered))

    def handle_endtag(self, tag: str) -> None:
        while self._open and self._open.pop()[0] != tag:
            pass


def claim_problems(html: str, register: str) -> list[str]:
    parser = _Claims()
    parser.feed(SVG.sub("", html))
    listed = re.findall(r"^\| `([^`]*)` \|", register, flags=re.M)
    on_page = set(parser.ids)
    problems = [f"bad id {i!r}" for i in sorted(on_page | set(listed)) if not CLAIM_ID.match(i)]
    problems += [f"not in CLAIMS.md: {i}" for i in sorted(on_page - set(listed))]
    problems += [f"not on the page: {i}" for i in sorted(set(listed) - on_page)]
    problems += [f"no data-claim: {e}" for e in parser.uncovered]
    return problems


def forbidden_phrases() -> list[str]:
    lines = (LANDING / "forbidden_claims.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def forbidden_found(html: str, phrases: list[str]) -> list[str]:
    text = re.sub(r"\s+", " ", STYLE.sub("", html)).lower()
    return [p for p in phrases if p.lower() in text]


def test_the_page_and_the_register_agree() -> None:
    assert claim_problems(HTML, REGISTER) == []


def test_the_page_marks_its_claim_sections() -> None:
    marked = set(re.findall(r'<section[^>]*id="([a-z]+)"[^>]*data-claims', HTML))
    assert {"how", "example", "works", "get", "security", "faq"} <= marked


def test_the_forbidden_list_names_what_the_ticket_bans() -> None:
    banned = {"nothing else", "zero trust", "guaranteed", "cannot be breached", "soc 2", "hipaa"}
    banned |= {"compliant", "unlimited", "any cloud", "instantly"}
    assert banned <= {p.lower() for p in forbidden_phrases()}


def test_no_forbidden_phrase_is_on_the_page() -> None:
    assert forbidden_found(HTML, forbidden_phrases()) == []


def test_every_public_source_on_the_page_has_a_source_row() -> None:
    sources = REGISTER.split("## Public sources", 1)[1].split("\n## ", 1)[0]
    on_page = set(
        re.findall(r"\bsrc-[a-z0-9-]+", " ".join(re.findall(r'data-claim="([^"]+)"', HTML)))
    )
    assert on_page
    assert [i for i in sorted(on_page) if f"`{i}`:" not in sources] == []


def test_the_commissioned_study_says_who_paid_for_it() -> None:
    row = re.search(r"^\| `src-forrester-okta` \|.*$", REGISTER, flags=re.M)
    assert row is not None
    assert "commissioned by Okta" in row[0]
    assert "commissioned by Okta" in REGISTER.split("## Public sources", 1)[1]
    assert "commissioned by Okta" in HTML


def test_the_approval_row_says_it_is_our_estimate() -> None:
    row = re.search(r"^\| `approve-estimate` \|.*$", REGISTER, flags=re.M)
    assert row is not None
    assert "Our estimate, set by the founder on 2026-10-28" in row[0]
    assert "No study measures this" in row[0]


REG = "| `login` | x | y | z |\n| `share` | x | y | z |\n"


@pytest.mark.parametrize(
    ("page", "problem"),
    [
        ('<section data-claims><p data-claim="login share">x</p></section>', None),
        ('<section data-claims><ul data-claim="login share"><li>x</li></ul></section>', None),
        ('<section data-claims><p class="label">x</p><p data-claim="login share">', None),
        ('<p data-claim="login">x</p>', "not on the page: share"),
        ('<p data-claim="login share pricing">x</p>', "not in CLAIMS.md: pricing"),
        ('<p data-claim="login share Bad_Id">x</p>', "bad id 'Bad_Id'"),
        ('<p data-claim="login share x">x</p>', "bad id 'x'"),
        ('<section data-claims><h2>x</h2><p data-claim="login share">', "no data-claim: <h2>"),
        (
            '<section data-claims><li data-claim="login share"></li><li>x</li>',
            "no data-claim: <li>",
        ),
        ('<section data-claims><p data-claim="">x</p><p data-claim="login share">', "bad id ''"),
    ],
)
def test_a_page_that_breaks_a_claim_rule_is_caught(page: str, problem: str | None) -> None:
    problems = claim_problems(page, REG)
    if problem is None:
        assert problems == []
    else:
        assert any(p.startswith(problem) for p in problems), problems


def test_an_uncovered_element_outside_a_claim_section_is_not_counted() -> None:
    assert claim_problems('<section><p>x</p><p data-claim="login share">x</p></section>', REG) == []


@pytest.mark.parametrize("phrase", ["Zero Trust", "SOC 2", "instantly", "nothing\n  else"])
def test_a_forbidden_phrase_is_caught_in_any_case_or_spacing(phrase: str) -> None:
    page = f"<p>Our apps are {phrase} by design.</p>"
    assert forbidden_found(page, ["zero trust", "soc 2", "instantly", "nothing else"]) != []


def test_forbidden_phrases_in_styles_are_ignored() -> None:
    assert forbidden_found("<style>.unlimited{}</style><p>x</p>", ["unlimited"]) == []

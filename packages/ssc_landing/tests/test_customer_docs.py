"""N-2, N-3: customer-facing docs carry no template placeholder and runnable commands."""

from pathlib import Path

DOCS = Path(__file__).resolve().parents[3] / "docs"


def test_no_doc_carries_the_support_email_placeholder() -> None:
    found = [
        str(p.relative_to(DOCS))
        for p in sorted(DOCS.rglob("*"))
        if p.is_file()
        and p.suffix in {".md", ".txt", ".html", ".json"}
        and "<support_email>" in p.read_text(encoding="utf-8")
    ]
    assert found == []


def test_the_help_page_names_where_the_support_address_is() -> None:
    text = (DOCS / "support" / "how-to-get-help.md").read_text(encoding="utf-8")
    assert "the address on the console's Help page" in text
    assert "VITE_SSC_SUPPORT_EMAIL" in text
    assert "`support_email` setting" not in text


def test_the_kill_drill_creates_the_app_before_deploying_it() -> None:
    text = (DOCS / "kill-switch-drill.md").read_text(encoding="utf-8")
    create = text.index("`ssc apps create kill-drill`")
    deploy = text.index("`ssc deploy conformance/kill_drill_app --app kill-drill --wait`")
    assert create < deploy


README = DOCS.parent / "README.md"


def _section(text: str, heading: str) -> str:
    return text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def test_the_readme_gives_the_customer_path_before_the_source_path() -> None:
    text = README.read_text(encoding="utf-8")
    assert text.index("\n## Using SSC\n") < text.index("\n## Get started from source\n")
    section = _section(text, "## Using SSC")
    for fact in ("org id", "Okta", "console"):
        assert fact in section, fact
    steps = [
        "uv tool install 'ssc-cli[mcp]'",
        "ssc login --org",
        "ssc init",
        "ssc apps create",
        "ssc deploy . --app",
        "ssc promote",
    ]
    places = [section.index(s) for s in steps]
    assert places == sorted(places)
    assert "--wait" in section.split("ssc deploy . --app", 1)[1].splitlines()[0]
    for where in ("ssc status", "ssc logs", "--source build", "Fix:", "exit code"):
        assert where in section, where
    assert "(docs/support/how-to-get-help.md)" in section


def test_the_readme_says_who_promotes() -> None:
    text = " ".join(README.read_text(encoding="utf-8").split())
    assert "human-controlled approval path" not in text
    assert "Coding agents cannot promote" in text
    assert "a builder of the app promotes" in text

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

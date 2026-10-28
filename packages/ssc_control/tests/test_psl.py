"""CD-5.3: the vendored Public Suffix List, its parser and tools/psl_refresh.py."""

import importlib.util
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import ModuleType

import pytest

from ssc_control.domains.psl import parse, public_suffix, vendored

MAX_AGE: timedelta = timedelta(days=90)
TOOL = Path(__file__).resolve().parents[3] / "tools" / "psl_refresh.py"

LIST = """// VERSION: 2026-10-22_08-36-41_UTC
// ===BEGIN ICANN DOMAINS===
com
uk
co.uk
*.ck
!www.ck
*.kawasaki.jp
!city.kawasaki.jp
公司.cn  trailing words are ignored
// ===END ICANN DOMAINS===
"""


@pytest.mark.parametrize(
    ("name", "public"),
    [
        ("tools.example.com", "com"),
        ("tools.co.uk", "co.uk"),
        ("co.uk", "co.uk"),
        ("a.b.ck", "b.ck"),
        ("b.ck", "b.ck"),
        ("ck", "ck"),
        ("www.ck", "ck"),
        ("a.www.ck", "ck"),
        ("a.x.kawasaki.jp", "x.kawasaki.jp"),
        ("tools.city.kawasaki.jp", "kawasaki.jp"),
        ("x.xn--55qx5d.cn", "xn--55qx5d.cn"),
        ("tools.example.zzz", "zzz"),
    ],
)
def test_the_parser_follows_plain_wildcard_and_exception_rules(name: str, public: str) -> None:
    assert public_suffix(name, parse(LIST)) == public


def test_a_list_without_a_version_is_refused() -> None:
    with pytest.raises(ValueError, match="VERSION"):
        parse("com\n")


def test_the_vendored_list_is_the_icann_section_and_ships_in_the_package() -> None:
    text = files("ssc_control.domains").joinpath("public_suffix_list.dat").read_text("utf-8")
    assert "// ===BEGIN ICANN DOMAINS===" in text
    assert text.rstrip().endswith("// ===END ICANN DOMAINS===")
    assert "PRIVATE DOMAINS" not in text
    rules = vendored()
    assert {"com", "co.uk", "xn--55qx5d.cn"} <= rules.plain
    assert "github.io" not in rules.plain
    assert ("kawasaki.jp" in rules.wildcards, "city.kawasaki.jp" in rules.exceptions) == (
        True,
        True,
    )


def test_the_vendored_list_is_at_most_ninety_days_old() -> None:
    age = datetime.now(UTC).date() - vendored().version
    assert age <= MAX_AGE, f"run tools/psl_refresh.py: the list is {age.days} days old"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("psl_refresh", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_refresh_keeps_the_header_and_the_icann_section(tmp_path: Path) -> None:
    tool = load_tool()
    source = tmp_path / "in.dat"
    source.write_text(
        "// header\n\n// VERSION: 2026-10-22_08-36-41_UTC\n\n"
        "// ===BEGIN ICANN DOMAINS===\ncom\n*.ck\n!www.ck\n// ===END ICANN DOMAINS===\n\n"
        "// ===BEGIN PRIVATE DOMAINS===\ngithub.io\n// ===END PRIVATE DOMAINS===\n",
        "utf-8",
    )
    out = tmp_path / "out.dat"
    assert tool.main(["--source", str(source), "--out", str(out)]) == 0
    assert out.read_text("utf-8") == (
        "// header\n\n// VERSION: 2026-10-22_08-36-41_UTC\n\n"
        "// ===BEGIN ICANN DOMAINS===\ncom\n*.ck\n!www.ck\n// ===END ICANN DOMAINS===\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        "// ===BEGIN ICANN DOMAINS===\ncom\n// ===END ICANN DOMAINS===\n",
        "// VERSION: 2026-10-22_08-36-41_UTC\n// ===BEGIN ICANN DOMAINS===\nco m\n"
        "// ===END ICANN DOMAINS===\n",
        "// VERSION: 2026-10-22_08-36-41_UTC\ncom\n",
    ],
)
def test_the_refresh_refuses_a_list_it_cannot_read(text: str) -> None:
    with pytest.raises(ValueError):
        load_tool().icann(text)

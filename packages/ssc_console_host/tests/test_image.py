"""M-8: the console image's build context never carries a developer's console env files."""

import re
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]
IGNORE = (HOST / "Dockerfile.dockerignore").read_text(encoding="utf-8")


def _pattern(glob: str) -> re.Pattern[str]:
    out = ""
    for part in re.split(r"(\*\*/|\*\*|\*|\?)", glob.strip("/")):
        out += {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(out + r"\Z")


def sent(path: str, ignore: str = IGNORE) -> bool:
    """Whether ``docker build`` sends ``path``: the last pattern matching it or a parent wins."""
    parents = ["/".join(path.split("/")[: i + 1]) for i in range(path.count("/") + 1)]
    keep = True
    for line in ignore.splitlines():
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        negated = rule.startswith("!")
        pattern = _pattern(rule.removeprefix("!"))
        if any(pattern.match(p) for p in parents):
            keep = negated
    return keep


@pytest.mark.parametrize(
    "path",
    ["console/.env", "console/.env.local", "console/.env.production", "console/.env.x.local"],
)
def test_console_env_files_stay_out_of_the_build(path: str) -> None:
    assert not sent(path)


@pytest.mark.parametrize("path", ["console/package.json", "console/src/main.tsx", "uv.lock"])
def test_the_console_source_is_sent(path: str) -> None:
    assert sent(path)


def test_the_matcher_reads_negation_and_globs() -> None:
    rules = "*\n!console/\n**/node_modules\nconsole/.env.*\n"
    assert sent("console/a.ts", rules)
    assert not sent("console/node_modules/x.js", rules)
    assert not sent("console/.env.local", rules)
    assert sent("console/.env", rules)
    assert not sent("infra/x.py", rules)


def runtime_copies(dockerfile: str) -> list[str]:
    """The ``COPY`` lines of the last stage, the one that ships."""
    last = re.split(r"^FROM ", dockerfile, flags=re.M)[-1]
    return [" ".join(line.split()[1:]) for line in last.splitlines() if line.startswith("COPY ")]


def test_the_console_image_ships_only_the_venv_and_the_build() -> None:
    dockerfile = (HOST / "Dockerfile").read_text(encoding="utf-8")
    assert runtime_copies(dockerfile) == [
        "--from=builder /app/.venv /app/.venv",
        "--from=console /console/dist /app/dist",
    ]
    assert all("@sha256:" in line for line in re.findall(r"^FROM .*", dockerfile, flags=re.M))


REPO = HOST.parents[1]


def _read(*paths: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _table_names(readme: str, heading: str) -> set[str]:
    section = readme.split(heading, 1)[1].split("\n#", 1)[0]
    return set(re.findall(r"^\| `([A-Z_]+)` \|", section, flags=re.M))


def test_the_console_readme_lists_the_settings_the_build_reads() -> None:
    console = REPO / "console"
    sources = [*sorted((console / "src").rglob("*.ts*")), console / "vite.config.ts"]
    read = set(re.findall(r"\b(VITE_SSC_[A-Z_]+|SSC_API_URL)\b", _read(*sources)))
    readme = (console / "README.md").read_text(encoding="utf-8")
    assert _table_names(readme, "## Settings") == read
    assert "reads two settings" not in readme


def test_the_host_readme_lists_the_settings_the_host_and_image_read() -> None:
    main = (HOST / "src" / "ssc_console_host" / "__main__.py").read_text(encoding="utf-8")
    dockerfile = (HOST / "Dockerfile").read_text(encoding="utf-8")
    readme = (HOST / "README.md").read_text(encoding="utf-8")
    assert _table_names(readme, "## Settings") == set(re.findall(r'"(SSC_[A-Z_]+|PORT)"', main))
    args = set(re.findall(r"^ARG (VITE_SSC_[A-Z_]+)=", dockerfile, flags=re.M))
    assert _table_names(readme, "## Build arguments") == args

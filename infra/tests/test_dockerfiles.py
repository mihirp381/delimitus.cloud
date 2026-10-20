"""Every download an image build makes is pinned by its sha256.

A Dockerfile's ``ADD <url>`` takes whatever the server sends that day. The deployer image runs
with the deployer's rights and the build tools image runs every build, so each remote ``ADD`` in
the repository carries ``--checksum=sha256:<digest>``.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKIP = {".git", ".venv", "node_modules", "Cloud_for_small_soft"}
CHECKSUM = re.compile(r"--checksum=sha256:[0-9a-f]{64}(\s|$)")


def dockerfiles() -> list[Path]:
    found = []
    for path in ROOT.rglob("*Dockerfile"):
        if SKIP.isdisjoint(path.relative_to(ROOT).parts) and path.is_file():
            found.append(path)
    return sorted(found)


def instructions(text: str) -> list[str]:
    """The file's instructions, each with its continuation lines joined and comments dropped."""
    out: list[str] = []
    current = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if line.rstrip().endswith("\\"):
            current += line.rstrip()[:-1] + " "
            continue
        current += line
        if current.strip():
            out.append(current.strip())
        current = ""
    if current.strip():
        out.append(current.strip())
    return out


def remote_adds(text: str) -> list[str]:
    return [
        i
        for i in instructions(text)
        if re.match(r"ADD\s", i, re.IGNORECASE) and re.search(r"\shttps?://", i)
    ]


def test_the_repository_s_images_are_found() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in dockerfiles()}
    assert {"infra/deployer/Dockerfile", "infra/build_tools/Dockerfile"} <= names


@pytest.mark.parametrize("path", dockerfiles(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_remote_add_carries_a_sha256_checksum(path: Path) -> None:
    unpinned = [i for i in remote_adds(path.read_text()) if not CHECKSUM.search(i)]
    assert unpinned == []


def test_an_unpinned_or_malformed_add_is_found() -> None:
    text = (
        "FROM x\n"
        "ADD https://example.invalid/a.tar.gz /tmp/a\n"
        "ADD --checksum=sha256:abc \\\n    https://example.invalid/b /tmp/b\n"
        f"ADD --checksum=sha256:{'a' * 64} \\\n    https://example.invalid/c /tmp/c\n"
        "ADD local.txt /tmp/d\n"
    )
    adds = remote_adds(text)
    assert len(adds) == 3
    assert [bool(CHECKSUM.search(a)) for a in adds] == [False, False, True]


def test_the_deployer_downloads_are_checked() -> None:
    adds = remote_adds((ROOT / "infra" / "deployer" / "Dockerfile").read_text())
    urls = {u for a in adds for u in re.findall(r"https?://\S+", a)}
    assert urls == {
        "https://get.pulumi.com/releases/sdk/pulumi-v${PULUMI_VERSION}-linux-x64.tar.gz",
        "https://github.com/pulumi/pulumi-gcp/releases/download/v${PULUMI_GCP_VERSION}/"
        "pulumi-resource-gcp-v${PULUMI_GCP_VERSION}-linux-amd64.tar.gz",
    }

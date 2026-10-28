"""The Public Suffix List's ICANN section, vendored by ``tools/psl_refresh.py`` (CD-5.3)."""

import re
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files

_FILE = "public_suffix_list.dat"
_VERSION = re.compile(r"// VERSION: (\d{4}-\d{2}-\d{2})_")


@dataclass(frozen=True, slots=True)
class Rules:
    plain: frozenset[str]
    wildcards: frozenset[str]
    exceptions: frozenset[str]
    version: date


def _ascii(name: str) -> str:
    return ".".join(
        label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
        for label in name.lower().split(".")
    )


def parse(text: str) -> Rules:
    plain: set[str] = set()
    wildcards: set[str] = set()
    exceptions: set[str] = set()
    version: date | None = None
    for line in text.splitlines():
        if version is None and (m := _VERSION.match(line)):
            version = date.fromisoformat(m[1])
        fields = line.split()
        if not fields or fields[0].startswith("//"):
            continue
        rule = fields[0]
        if rule.startswith("!"):
            exceptions.add(_ascii(rule[1:]))
        elif rule.startswith("*."):
            wildcards.add(_ascii(rule[2:]))
        else:
            plain.add(_ascii(rule))
    if version is None:
        raise ValueError("the list has no VERSION line")
    return Rules(frozenset(plain), frozenset(wildcards), frozenset(exceptions), version)


@cache
def vendored() -> Rules:
    return parse(files(__package__).joinpath(_FILE).read_text("utf-8"))


def public_suffix(name: str, rules: Rules | None = None) -> str:
    """The public suffix of the lower-case ASCII ``name``; an unlisted top label is one."""
    r = rules or vendored()
    labels = name.split(".")
    tails = [".".join(labels[i:]) for i in range(len(labels))]
    for i, tail in enumerate(tails):
        if tail in r.exceptions:
            return tails[i + 1]
    for i, tail in enumerate(tails):
        if tail in r.plain or (i + 1 < len(tails) and tails[i + 1] in r.wildcards):
            return tail
    return labels[-1]


__all__ = ["Rules", "parse", "public_suffix", "vendored"]

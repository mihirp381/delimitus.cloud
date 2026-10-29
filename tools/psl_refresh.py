"""Refresh the vendored Public Suffix List: its header and ICANN section only (CD-5.3).

``--source FILE`` reads a copy already downloaded instead of the network."""

import argparse
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://publicsuffix.org/list/public_suffix_list.dat"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packages/ssc_control/src/ssc_control/domains/public_suffix_list.dat"
BEGIN = "// ===BEGIN ICANN DOMAINS==="
END = "// ===END ICANN DOMAINS==="
_VERSION = re.compile(r"// VERSION: \d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_UTC")
_RULE = re.compile(r"(!|\*\.)?[^\s!*/]+")


def icann(text: str) -> str:
    lines = text.splitlines()
    begin, end = lines.index(BEGIN), lines.index(END)
    header = [line for line in lines[:begin] if line.startswith("//") or not line.strip()]
    if not any(_VERSION.fullmatch(line) for line in header):
        raise ValueError("no VERSION line before the ICANN section")
    body = lines[begin : end + 1]
    for line in body:
        if line and not line.startswith("//") and not _RULE.fullmatch(line):
            raise ValueError(f"not a rule: {line!r}")
    return "\n".join([*header, *body]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    if args.source is not None:
        text = args.source.read_text("utf-8")
    else:
        with urllib.request.urlopen(URL, timeout=60) as r:  # noqa: S310
            text = r.read().decode("utf-8")
    args.out.write_text(icann(text), "utf-8")
    sys.stderr.write(f"wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

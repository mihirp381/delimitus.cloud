"""The nightly organisation-policy check (SSC-056): ``python -m ssc_conformance.org_policies``.

Lists the policies set on the ``ssc-cells`` folder and on the cell project, as ``ssc-nightly``
over REST, and compares them with ``expected_org_policies.json``. The folder must set exactly
those constraints with exactly those values, and the project none of its own, so no cell can
loosen a policy (SSC-095). The infra tests hold the file equal to what the platform stack
declares; the operator is written ``<operator>`` in it and is any ``user:`` here. The one
exception rule must carry the platform's exact condition, ``resource.matchTagId(...)`` on the
public-invoker tag, whose ids the file writes ``${SSC_PUBLIC_TAG_KEY}`` and
``${SSC_PUBLIC_TAG_VALUE}`` and the run takes from those variables, as it takes the organisation
in the principal set from ``${SSC_ORG_ID}``; and no policy may inherit
from the parent. The listing goes to stdout and the job summary, and any difference to the page
as a failure.

Configuration and exit status as ``ssc_conformance.least_privilege``: ``SSC_PROBE_PROJECT``,
``SSC_NIGHT_FOLDER_ID``, ``SSC_EVIDENCE_FILE``, and ``SSC_PUBLIC_TAG_KEY`` (``tagKeys/<id>``)
and ``SSC_PUBLIC_TAG_VALUE`` (``tagValues/<id>``), the platform stack's ``public_invoker_tag``,
and ``SSC_ORG_ID``, the organisation's numeric id.
A 403 is ``skipped (no read access)``.
"""

import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

import httpx2

from ssc_conformance import cloud_read as cloud
from ssc_conformance import evidence as ev
from ssc_conformance import matrix, nightly
from ssc_conformance.evidence import Result
from ssc_conformance.least_privilege import FOLDER_ENV, PROJECT_ENV

EXPECTED: Final = Path(__file__).with_name("expected_org_policies.json")
OPERATOR: Final = "<operator>"
SUBJECTS: Final = "allowedMemberSubjects"
TAG_KEY_ENV: Final = "SSC_PUBLIC_TAG_KEY"
TAG_VALUE_ENV: Final = "SSC_PUBLIC_TAG_VALUE"
ORG_ENV: Final = "SSC_ORG_ID"
FORMS: Final = {
    TAG_KEY_ENV: (re.compile(r"tagKeys/[0-9]+"), "tagKeys/<number>"),
    TAG_VALUE_ENV: (re.compile(r"tagValues/[0-9]+"), "tagValues/<number>"),
    ORG_ENV: (re.compile(r"[0-9]+"), "the organisation's numeric id"),
}

type Json = dict[str, Any]


def _parameters(value: object) -> dict[str, list[str]]:
    found = cast(Json, value) if isinstance(value, dict) else {}
    shaped = {name: sorted(cloud.strings(values)) for name, values in found.items()}
    if SUBJECTS in shaped:
        shaped[SUBJECTS] = sorted(
            OPERATOR if m.startswith("user:") else m for m in shaped[SUBJECTS]
        )
    return shaped


def load_expected(environ: Mapping[str, str], path: Path = EXPECTED) -> dict[str, Json]:
    """The expected file with the tag's ids and the org id put in from ``environ``."""
    raw = path.read_text(encoding="utf-8")
    wanted = [name for name in FORMS if f"${{{name}}}" in raw]
    missing = [name for name in wanted if not environ.get(name)]
    if missing:
        raise cloud.CloudReadError(
            f"missing {', '.join(missing)}: the public-invoker tag's key and value ids "
            "(tagKeys/<id>, tagValues/<id>; the platform stack's public_invoker_tag) and the "
            "organisation's numeric id"
        )
    bad = [name for name in wanted if not FORMS[name][0].fullmatch(environ[name])]
    if bad:
        raise cloud.CloudReadError("; ".join(f"{name} must be {FORMS[name][1]}" for name in bad))
    for name in wanted:
        raw = raw.replace(f"${{{name}}}", environ[name])
    return cast(dict[str, Json], json.loads(raw))


def normalise(policy: Json) -> Json:
    """One live policy in the form of the expected file: ``mode`` (``enforce``, ``deny_all`` or
    ``allow``, else a description of what it is), its sorted ``values``, its ``parameters``, the
    condition of the one rule that turns it off (``None`` without one, else a description), and
    whether it inherits from the parent."""
    spec = cast(Json, policy.get("spec") or {})
    rules = cloud.objects(spec.get("rules"))
    plain = [r for r in rules if not r.get("condition")]
    tagged = [r for r in rules if r.get("condition")]
    main = plain[0] if len(plain) == 1 else {}
    values = sorted(cloud.strings(cast(Json, main.get("values") or {}).get("allowedValues")))
    if main.get("denyAll") is True:
        mode = "deny_all"
    elif "values" in main:
        mode = "allow"
    elif main.get("enforce") is True:
        mode = "enforce"
    else:
        mode = f"unrecognised ({len(plain)} unconditional rules)"
    if not tagged:
        exception = None
    elif len(tagged) == 1 and tagged[0].get("enforce") is False:
        exception = str(cast(Json, tagged[0].get("condition") or {}).get("expression", ""))
    else:
        exception = f"unrecognised ({len(tagged)} conditional rules)"
    return {
        "mode": mode,
        "values": values,
        "parameters": _parameters(main.get("parameters")),
        "exception": exception,
        "inherit_from_parent": spec.get("inheritFromParent") is True,
    }


def _constraint(policy: Json) -> str:
    return str(policy.get("name", "")).rsplit("/", 1)[-1]


def summary(spec: Mapping[str, Any]) -> str:
    text = {"deny_all": "deny all", "enforce": "enforced"}.get(
        str(spec["mode"]), f"allow {', '.join(spec['values'])}"
    )
    for name, values in spec["parameters"].items():
        text += f"; {name} {', '.join(values)}"
    if spec["exception"] is not None:
        text += f"; off where {spec['exception']}"
    return text + ("; inherits from the parent" if spec["inherit_from_parent"] else "")


def problems(
    folder: Sequence[Json], project: Sequence[Json], expected: Mapping[str, Any]
) -> list[str]:
    """What differs between the live folder and project policies and the expected ones."""
    live = {_constraint(p): normalise(p) for p in folder}
    found: list[str] = []
    for constraint in sorted(expected):
        if constraint not in live:
            found.append(f"{constraint}: not set on the folder")
        elif live[constraint] != expected[constraint]:
            found.append(
                f"{constraint}: expected {summary(expected[constraint])}, "
                f"found {summary(live[constraint])}"
            )
    found += [f"{c}: set on the folder and not expected" for c in sorted(live) if c not in expected]
    found += [f"{_constraint(p)}: set on the cell project itself" for p in project]
    return found


def listing(folder: Sequence[Json]) -> str:
    live = sorted((_constraint(p), normalise(p)) for p in folder)
    lines = ["| Constraint | In force |", "| --- | --- |"]
    lines += [f"| {constraint} | {summary(spec)} |" for constraint, spec in live]
    return "\n".join(lines) + "\n"


async def check(
    reader: cloud.CloudReader,
    project: str,
    folder: str,
    *,
    expected: Mapping[str, Any],
) -> tuple[Result, str]:
    """The proof's result and the folder's listing; the listing is empty when unread."""
    try:
        on_folder = await reader.org_policies("folders", folder)
        on_project = await reader.org_policies("projects", project)
    except cloud.NoReadAccessError:
        return Result(matrix.ORG_POLICIES, ev.SKIPPED, ev.NO_READ_ACCESS), ""
    differences = problems(on_folder, on_project, expected)
    status = ev.FAIL if differences else ev.OK
    return Result(matrix.ORG_POLICIES, status, "; ".join(differences)), listing(on_folder)


async def main_async(
    environ: Mapping[str, str], *, client: httpx2.AsyncClient | None = None
) -> tuple[Result, str]:
    missing = [name for name in (PROJECT_ENV, FOLDER_ENV) if not environ.get(name)]
    if missing:
        raise cloud.CloudReadError(f"missing {', '.join(missing)}")
    project = environ[PROJECT_ENV]
    expected = load_expected(environ)
    reader = cloud.CloudReader(nightly.gcloud_access_tokens(), client=client)
    try:
        result, text = await check(reader, project, environ[FOLDER_ENV], expected=expected)
    finally:
        await reader.aclose()
    if path := environ.get(ev.EVIDENCE_ENV):
        ev.write(Path(path), ev.Evidence(project, peer=False, results=(result,)))
    return result, text


def main() -> int:
    try:
        result, text = asyncio.run(main_async(os.environ))
    except cloud.CloudReadError as exc:
        sys.stderr.write(f"org policies: {exc}\n")
        return 1
    sys.stdout.write(text)
    if result.status != ev.OK:
        sys.stdout.write(f"{result.status}: {result.reason}\n")
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as f:
            f.write("## SSC-056 organisation policies\n\n" + text)
    return 0 if result.status == ev.OK else 1


if __name__ == "__main__":
    sys.exit(main())

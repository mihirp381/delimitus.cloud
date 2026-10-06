"""The nightly least-privilege checks (SSC-056): ``python -m ssc_conformance.least_privilege``.

Four checks, each read from how the cell and the ``ssc-cells`` folder are configured, as
``ssc-nightly`` and over REST (``cloud_read``). Each names its failures; none changes anything.

* ``build_account_bundle_only``: ``ssc-build`` holds ``roles/logging.logWriter`` in the cell
  project and nothing in the cell bucket or the folder, so it cannot list the bucket (SSC-015).
* ``no_standing_staff_access``: the cell project and the folder grant no person, group or
  domain anything, except a just-in-time ``roles/writer`` that expires within the hour
  (decision 022).
* ``secret_read_denied``: the cell's deny policy refuses ``versions.access`` to every cell
  identity (``ssc-data`` only without the connection tag) and the folder's refuses it to every
  control-plane account. ``expected_deny.json`` lists them; the infra tests hold it equal to the
  stacks, and a deny policy may name more accounts than it.
* ``control_adds_versions_only``: no control-plane account holds a role that reads a secret
  value in the cell project or on the folder, and the folder's deny covers them all.

Configuration: ``SSC_PROBE_PROJECT`` (the cell project, also what the cell is called on the
page), ``SSC_NIGHT_FOLDER_ID`` (the ``ssc-cells`` folder) and the access token as in
``ssc_conformance.nightly``. A 403 makes a check ``skipped (no read access)``, which fails the
night. ``SSC_EVIDENCE_FILE`` names the file the results are added to. Exits 1 on any failure.
"""

import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import httpx2

from ssc_conformance import cloud_read as cloud
from ssc_conformance import evidence as ev
from ssc_conformance import matrix, nightly
from ssc_conformance.evidence import Result

FOLDER_ENV: Final = "SSC_NIGHT_FOLDER_ID"
PROJECT_ENV: Final = "SSC_PROBE_PROJECT"
EXPECTED_DENY: Final = Path(__file__).with_name("expected_deny.json")
BUILD_ACCOUNT: Final = "ssc-build"
BUILD_ROLES: Final = frozenset({"roles/logging.logWriter"})
JIT_ROLE: Final = "roles/writer"
JIT_SECONDS: Final = 3600
JIT_SLACK_SECONDS: Final = 300
DENY_POLICY: Final = "ssc-deny-secret-read"
PEOPLE: Final = (
    "user:",
    "group:",
    "domain:",
    "allUsers",
    "allAuthenticatedUsers",
    "projectOwner:",
    "projectEditor:",
    "projectViewer:",
)
READS_SECRETS: Final = frozenset(
    {
        "roles/secretmanager.secretAccessor",
        "roles/secretmanager.admin",
        "roles/editor",
        "roles/owner",
    }
)
EXPIRY: Final = re.compile(r'request\.time\s*<\s*timestamp\(\s*["\']([^"\']+)["\']\s*\)')
PRINCIPAL: Final = "principal://iam.googleapis.com/projects/-/serviceAccounts/"

type Json = dict[str, Any]


def _bindings(policy: Json) -> list[Json]:
    return cloud.objects(policy.get("bindings"))


def _members(binding: Json) -> list[str]:
    return cloud.strings(binding.get("members"))


def _account(member: str) -> str:
    """The account id of ``serviceAccount:<id>@...`` or a deny policy principal, else ``""``."""
    email = member.removeprefix("serviceAccount:").removeprefix(PRINCIPAL)
    if email == member or "@" not in email:
        return ""
    return email.split("@", 1)[0]


def _held(policy: Json, member: str) -> set[str]:
    return {str(b.get("role")) for b in _bindings(policy) if member in _members(b)}


def build_account_problems(
    project: str, project_policy: Json, bucket_policy: Json, folder_policy: Json
) -> list[str]:
    member = f"serviceAccount:{BUILD_ACCOUNT}@{project}.iam.gserviceaccount.com"
    problems: list[str] = []
    extra = _held(project_policy, member) - BUILD_ROLES
    if extra:
        problems.append(f"{BUILD_ACCOUNT} holds {', '.join(sorted(extra))} in the project")
    if _held(bucket_policy, member):
        problems.append(f"{BUILD_ACCOUNT} is bound on the cell bucket")
    if _held(folder_policy, member):
        problems.append(f"{BUILD_ACCOUNT} is bound on the folder")
    return problems


def _expiry(binding: Json) -> datetime | None:
    condition = cast(Json, binding.get("condition") or {})
    found = EXPIRY.search(str(condition.get("expression", "")))
    if not found:
        return None
    try:
        return datetime.fromisoformat(found.group(1))
    except ValueError:
        return None


def staff_problems(where: str, policy: Json, now: datetime) -> list[str]:
    """A person, group or domain with a binding that is not a just-in-time one. An entitlement
    grant is ``roles/writer`` with a condition that expires within the hour; one past its
    expiry grants nothing and is ignored."""
    problems: list[str] = []
    for binding in _bindings(policy):
        role = str(binding.get("role"))
        expires = _expiry(binding)
        for member in _members(binding):
            if not member.startswith(PEOPLE):
                continue
            if expires is not None and expires <= now:
                continue
            soon = expires is not None and expires <= now + timedelta(
                seconds=JIT_SECONDS + JIT_SLACK_SECONDS
            )
            if role != JIT_ROLE or not soon:
                limit = f"expires {expires:%Y-%m-%dT%H:%MZ}" if expires else "no expiry"
                problems.append(f"{where}: {member} holds {role} ({limit})")
    return problems


def _rules(policies: Sequence[Json]) -> list[Json]:
    """The deny rules of the SSC policy among ``policies``."""
    ours = [p for p in policies if str(p.get("name", "")).endswith(f"/{DENY_POLICY}")]
    return [
        cast(Json, r["denyRule"])
        for p in ours
        for r in cloud.objects(p.get("rules"))
        if isinstance(r.get("denyRule"), dict)
    ]


def _strings(rule: Json, key: str) -> list[str]:
    return cloud.strings(rule.get(key))


def _refuses(rule: Json, permission: str) -> bool:
    return permission in _strings(rule, "deniedPermissions")


def _unconditional(rules: Sequence[Json], permission: str) -> set[str]:
    """The accounts refused ``permission`` with no condition and no exception."""
    found: set[str] = set()
    for rule in rules:
        if _refuses(rule, permission) and not rule.get("denialCondition"):
            if rule.get("exceptionPrincipals"):
                continue
            found |= {a for p in _strings(rule, "deniedPrincipals") if (a := _account(p))}
    return found


def deny_problems(
    cell_policies: Sequence[Json], folder_policies: Sequence[Json], expected: Mapping[str, Any]
) -> list[str]:
    permission = str(expected["permission"])
    problems: list[str] = []
    cell_rules = _rules(cell_policies)
    if not cell_rules:
        problems.append(f"the cell has no {DENY_POLICY} policy")
    missing = set(expected["cell"]) - _unconditional(cell_rules, permission)
    if cell_rules and missing:
        problems.append(f"the cell's deny does not name {', '.join(sorted(missing))}")
    data = str(expected["cell_data"])
    tagged = [
        r
        for r in cell_rules
        if _refuses(r, permission)
        and data in {_account(p) for p in _strings(r, "deniedPrincipals")}
        and "matchTagId" in str((r.get("denialCondition") or {}).get("expression", ""))
        and not r.get("exceptionPrincipals")
    ]
    if cell_rules and data not in _unconditional(cell_rules, permission) and not tagged:
        problems.append(f"the cell's deny does not refuse {data} secrets without the tag")
    folder_rules = _rules(folder_policies)
    if not folder_rules:
        problems.append(f"the folder has no {DENY_POLICY} policy")
    gap = set(expected["folder"]) - _unconditional(folder_rules, permission)
    if folder_rules and gap:
        problems.append(f"the folder's deny does not name {', '.join(sorted(gap))}")
    return problems


def control_problems(
    where_policies: Mapping[str, Json], folder_policies: Sequence[Json], expected: Mapping[str, Any]
) -> list[str]:
    """No role that reads a secret value, or that cannot be told, for any control account."""
    control = set(expected["folder"])
    problems: list[str] = []
    for where, policy in where_policies.items():
        for binding in _bindings(policy):
            role = str(binding.get("role"))
            for member in _members(binding):
                if _account(member) not in control:
                    continue
                if role in READS_SECRETS:
                    problems.append(f"{where}: {_account(member)} holds {role}")
                elif not role.startswith("roles/"):
                    problems.append(f"{where}: {_account(member)} holds the custom role {role}")
    gap = control - _unconditional(_rules(folder_policies), str(expected["permission"]))
    if gap:
        problems.append(f"the folder's deny does not name {', '.join(sorted(gap))}")
    return problems


def _result(proof: str, problems: Sequence[str]) -> Result:
    return Result(proof, ev.FAIL if problems else ev.OK, "; ".join(problems))


class _Reads:
    """Each policy read once for the four checks; a refused read is refused for each check
    that needs it."""

    def __init__(self, reader: cloud.CloudReader, project: str, folder: str) -> None:
        self._reader = reader
        self._project = project
        self._folder = folder
        self._done: dict[str, Json | list[Json] | cloud.NoReadAccessError] = {}

    async def _once[T: Json | list[Json]](self, key: str, read: Callable[[], Awaitable[T]]) -> T:
        if key not in self._done:
            try:
                self._done[key] = await read()
            except cloud.NoReadAccessError as exc:
                self._done[key] = exc
        found = self._done[key]
        if isinstance(found, cloud.NoReadAccessError):
            raise found
        return cast(T, found)

    async def project(self) -> Json:
        return await self._once("project", lambda: self._reader.project_policy(self._project))

    async def folder(self) -> Json:
        return await self._once("folder", lambda: self._reader.folder_policy(self._folder))

    async def bucket(self) -> Json:
        bucket = f"{self._project}-cell"
        return await self._once("bucket", lambda: self._reader.bucket_policy(bucket))

    async def cell_deny(self) -> list[Json]:
        return await self._once(
            "cell-deny", lambda: self._reader.deny_policies("projects", self._project)
        )

    async def folder_deny(self) -> list[Json]:
        return await self._once(
            "folder-deny", lambda: self._reader.deny_policies("folders", self._folder)
        )


async def check(
    reader: cloud.CloudReader,
    project: str,
    folder: str,
    *,
    now: datetime | None = None,
    expected: Mapping[str, Any] | None = None,
) -> list[Result]:
    """The four checks against the cell project and the folder. A check whose reads are refused
    reports no read access; the others still run."""
    now = now or datetime.now(UTC)
    want = expected or json.loads(EXPECTED_DENY.read_text(encoding="utf-8"))
    reads = _Reads(reader, project, folder)

    async def build() -> list[str]:
        return build_account_problems(
            project, await reads.project(), await reads.bucket(), await reads.folder()
        )

    async def staff() -> list[str]:
        return staff_problems(f"project {project}", await reads.project(), now) + staff_problems(
            f"folder {folder}", await reads.folder(), now
        )

    async def deny() -> list[str]:
        return deny_problems(await reads.cell_deny(), await reads.folder_deny(), want)

    async def control() -> list[str]:
        where = {
            f"project {project}": await reads.project(),
            f"folder {folder}": await reads.folder(),
        }
        return control_problems(where, await reads.folder_deny(), want)

    results: list[Result] = []
    for proof, problems in (
        (matrix.BUILD_BUNDLE, build),
        (matrix.STAFF, staff),
        (matrix.DENY_READ, deny),
        (matrix.VERSIONS_ONLY, control),
    ):
        try:
            results.append(_result(proof, await problems()))
        except cloud.NoReadAccessError:
            results.append(Result(proof, ev.SKIPPED, ev.NO_READ_ACCESS))
    return results


def markdown(results: Sequence[Result]) -> str:
    lines = ["| Check | Result |", "| --- | --- |"]
    for r in results:
        lines.append(f"| {r.proof} | {r.status}{f' ({r.reason})' if r.reason else ''} |")
    return "\n".join(lines) + "\n"


async def main_async(
    environ: Mapping[str, str], *, client: httpx2.AsyncClient | None = None
) -> list[Result]:
    missing = [name for name in (PROJECT_ENV, FOLDER_ENV) if not environ.get(name)]
    if missing:
        raise cloud.CloudReadError(f"missing {', '.join(missing)}")
    project = environ[PROJECT_ENV]
    reader = cloud.CloudReader(nightly.gcloud_access_tokens(), client=client)
    try:
        results = await check(reader, project, environ[FOLDER_ENV])
    finally:
        await reader.aclose()
    if path := environ.get(ev.EVIDENCE_ENV):
        ev.write(Path(path), ev.Evidence(project, peer=False, results=tuple(results)))
    return results


def main() -> int:
    try:
        results = asyncio.run(main_async(os.environ))
    except cloud.CloudReadError as exc:
        sys.stderr.write(f"least privilege: {exc}\n")
        return 1
    text = markdown(results)
    sys.stdout.write(text)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as f:
            f.write("## SSC-056 least privilege\n\n" + text)
    return 0 if all(r.status == ev.OK for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

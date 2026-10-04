"""Done-when check 1: two cells are identical once their label, project number, addresses and
timestamps are taken out, apart from what their flags name.

    uv run python -m ssc_infra.cell_diff testcell01 testcell02

Compares ``pulumi stack export`` of both stacks: the same resources, the same inputs, and the same
outputs apart from values the cloud assigns. A stack's own cloud-assigned values (project number,
load balancer address, certificate authorisation record) and its customer's settings (``org_id``,
``gateway_keyring``, ``gateway_jwks``) become placeholders wherever they appear, so a record
pointing at another cell's address still shows; so do the database's DNS name and private
address, and the IDs of the cell's connection tag (SSC-051). The connections the data gateway
mounts (``datagw_connections``) are the customer's own too and are left out. Where the stacks'
``flags`` output differ, the lazy resources of a differing flag, the
agent's ``SSC_SQL_INSTANCE`` for ``database`` and the gateway's minimum (``gateway_min``,
``warm``) are left out. Prints each difference; exit 1 if there is any.

Then, for each cell, the organisation policies in force (SSC-095): the table the platform stack
applied to the ``ssc-cells`` folder (its ``cell_policies`` output), and what would weaken it there
— the project outside a stage folder, a policy the cell stack declares, or a policy set on the
project itself. The last is the one cloud read, ``gcloud org-policies list`` on the project;
exit 1 if any is found or the read fails.
"""

import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ssc_infra import naming as n
from ssc_infra.run import CommandError, gcloud_json, pulumi

type Json = Any
type Flat = dict[str, str]

SKIP_TYPES: Final = ("pulumi:pulumi:Stack", "pulumi:providers:")
ASSIGNED_KEYS: Final = frozenset(
    {
        "createTime",
        "creationTimestamp",
        "updateTime",
        "etag",
        "fingerprint",
        "labelFingerprint",
        "uid",
        "uniqueId",
        "oauth2ClientId",
        "generation",
        "observedGeneration",
        "latestCreatedRevision",
        "latestReadyRevision",
        "revision",
        "lastModifier",
        "creator",
        "conditions",
        "terminalConditions",
        "reconciling",
        "timeCreated",
        "updated",
        "serverCaCerts",
        "serverCaCert",
        "ipAddresses",
        "publicIpAddress",
        "privateIpAddress",
        "firstIpAddress",
        "dnsName",
        "dnsNames",
        "nameServers",
        "gatewayAddress",
        "gatewayIpv4",
        "managedZoneId",
        "pscServiceAttachmentLink",
        "dnsZone",
        "keyVersions",
        "creationTime",
        "effectiveTime",
        "numericId",
        "networkId",
        "subnetworkId",
        "addressId",
        "forwardingRuleId",
        "generatedId",
        "proxyId",
        "mapId",
        "serviceAccountEmailAddress",
        "primary",
        "effectiveLabels",
        "pulumiLabels",
        "ciphertext",
        "secretDataWoVersion",
        "certificate",
        "cert",
        "commonName",
        "expirationTime",
        "sha1Fingerprint",
        "urls",
        "uri",
        "maintenanceVersion",
        "availableMaintenanceVersions",
        "timeoutSec",
        "lastUpdateTime",
    }
)
ASSIGNED_PATHS: Final = {
    "gcp:billing/budget:Budget": frozenset({"out.name"}),
    "gcp:certificatemanager/certificate:Certificate": frozenset(
        {"out.managed.authorizationAttemptInfos", "out.managed.provisioningIssues"}
    ),
}
DNS_AUTHORIZATION: Final = "gcp:certificatemanager/dnsAuthorization:DnsAuthorization"
SQL_INSTANCE: Final = "gcp:sql/databaseInstance:DatabaseInstance"
PROJECT_TYPE: Final = "gcp:organizations/project:Project"
TAG_TYPES: Final = {
    "gcp:tags/tagKey:TagKey": "<tag-key>",
    "gcp:tags/tagValue:TagValue": "<tag-value>",
}
DATAGW_SERVICE: Final = f"gcp:cloudrunv2/service:Service::{n.DATA_GATEWAY}"
CONNECTION_ENVS: Final = ".envs.SSC_CONNECTION_"
FLAG_DEFAULTS: Final[dict[str, Json]] = {
    "database": False,
    "egress": False,
    "connections": False,
    "gateway_min": 0,
    "warm": False,
}
IPV4: Final = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
OWN_SETTINGS: Final = {
    "org_id": "<org>",
    "gateway_keyring": "<gateway-keyring>",
    "gateway_jwks": "<identity-jwks>",
}


def export(stack: str) -> Json:
    return json.loads(pulumi("stack", "export", "--stack", stack))


type Swaps = Sequence[tuple[re.Pattern[str], str]]


def _swap(value: str, swaps: Swaps, *, outputs: bool) -> str:
    """Inputs keep their addresses (fixed ranges); outputs lose them (the cloud assigns them)."""
    out = value
    for pattern, placeholder in swaps:
        out = pattern.sub(placeholder, out)
    return IPV4.sub("<ip>", out) if outputs else out


def flatten(value: Json, swaps: Swaps, prefix: str = "", *, outputs: bool) -> Flat:
    """Dotted paths to normalised scalars. Null counts as absent, as the provider writes either.

    For outputs, keys the cloud assigns are left out. A container's variables are keyed by name,
    so one a flag adds shows as itself.
    """
    flat: Flat = {}
    if value is None:
        return flat
    if isinstance(value, Mapping):
        items: Mapping[str, Json] = value  # pyright: ignore[reportUnknownVariableType]
        if "4dabf18193072939515e22adb298388d" in items:
            return {prefix: "<secret>"}
        for key, inner in items.items():
            if outputs and key in ASSIGNED_KEYS:
                continue
            flat |= flatten(inner, swaps, f"{prefix}.{key}" if prefix else key, outputs=outputs)
    elif isinstance(value, list) and prefix.endswith(".envs"):
        envs: list[Mapping[str, Json]] = value  # pyright: ignore[reportUnknownVariableType]
        for env in envs:
            flat |= flatten(env, swaps, f"{prefix}.{env.get('name')}", outputs=outputs)
    elif isinstance(value, list):
        for i, inner in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            flat |= flatten(inner, swaps, f"{prefix}[{i}]", outputs=outputs)
    else:
        flat[prefix] = _swap(json.dumps(value), swaps, outputs=outputs)
    return flat


def _stack_outputs(state: Json) -> Mapping[str, Json]:
    for res in state["deployment"].get("resources", []):
        if res["type"] == "pulumi:pulumi:Stack":
            return res.get("outputs", {})
    return {}


def _own_values(state: Json, label: str) -> Swaps:
    """The label, the customer's own settings and the values the cloud gave this stack, each with
    its placeholder. Settings are matched as ``flatten`` writes them, inside a JSON string."""
    outputs = _stack_outputs(state)
    settings: Mapping[str, Json] = outputs.get("config") or {}
    found = [
        (json.dumps(str(settings[key]))[1:-1], placeholder)
        for key, placeholder in OWN_SETTINGS.items()
        if settings.get(key)
    ]
    found += [(label, "<cell>"), (str(outputs.get("project_number") or ""), "<number>")]
    addresses = [(str(outputs.get("entry_address") or ""), "<entry-address>")]
    for res in state["deployment"].get("resources", []):
        assigned: Mapping[str, Json] = res.get("outputs", {})
        if res["type"] == DNS_AUTHORIZATION:
            records: list[Mapping[str, Json]] = assigned.get("dnsResourceRecords") or []
            found += [(str(r.get("data") or ""), "<dns-authorization>") for r in records]
        elif res["type"] == SQL_INSTANCE:
            found.append((str(assigned.get("dnsName") or "").rstrip("."), "<sql-dns>"))
            addresses.append((str(assigned.get("privateIpAddress") or ""), "<sql-address>"))
        elif res["type"] in TAG_TYPES:
            addresses.append((str(assigned.get("name") or ""), TAG_TYPES[res["type"]]))
    swaps = [(re.compile(re.escape(value)), placeholder) for value, placeholder in found if value]
    for address, placeholder in addresses:
        if address:
            exact = re.compile(rf"(?<![\d.]){re.escape(address)}(?!\d|\.\d)")
            swaps.append((exact, placeholder))
    return swaps


def _assigned(path: str, prefixes: frozenset[str]) -> bool:
    return any(path == p or path.startswith((f"{p}.", f"{p}[")) for p in prefixes)


def normalise(state: Json, label: str) -> dict[str, Flat]:
    """``type::name`` → flattened inputs and outputs, for every cloud resource in a stack."""
    swaps = _own_values(state, label)
    out: dict[str, Flat] = {}
    for res in state["deployment"].get("resources", []):
        kind: str = res["type"]
        if kind.startswith(SKIP_TYPES):
            continue
        name = res["urn"].rsplit("::", 1)[-1]
        flat = {
            f"in.{k}": v for k, v in flatten(res.get("inputs", {}), swaps, outputs=False).items()
        }
        flat |= {
            f"out.{k}": v for k, v in flatten(res.get("outputs", {}), swaps, outputs=True).items()
        }
        assigned = ASSIGNED_PATHS.get(kind, frozenset())
        out[f"{kind}::{name}"] = {k: v for k, v in flat.items() if not _assigned(k, assigned)}
    return out


def flags(state: Json) -> dict[str, Json]:
    """The five stack flags as the stack exported them; a stack without them has the defaults."""
    return FLAG_DEFAULTS | dict(_stack_outputs(state).get("flags") or {})


def differing(a: Mapping[str, Json], b: Mapping[str, Json]) -> list[str]:
    return [f for f in n.FLAGS if a.get(f) != b.get(f)]


def _customers(key: str, path: str) -> bool:
    """A connection the data gateway mounts, which only the customer's connections decide."""
    return key == DATAGW_SERVICE and CONNECTION_ENVS in path


def _flagged(key: str, path: str, flags_differ: Sequence[str]) -> bool:
    """Whether a differing flag names this resource, or this path of it."""
    if any(key in n.LAZY_RESOURCES.get(f, ()) for f in flags_differ):
        return True
    if "database" in flags_differ and key == n.AGENT_SERVICE:
        return f".envs.{n.SQL_INSTANCE_ENV}." in f"{path}."
    gateway_min = {"gateway_min", "warm"} & set(flags_differ)
    return bool(gateway_min) and key == n.GATEWAY_SERVICE and path.endswith(n.GATEWAY_MIN_PATH)


def compare(
    a: Mapping[str, Flat], b: Mapping[str, Flat], flags_differ: Sequence[str] = ()
) -> list[str]:
    diffs = [
        f"only in first: {k}"
        for k in sorted(a.keys() - b.keys())
        if not _flagged(k, "", flags_differ)
    ]
    diffs += [
        f"only in second: {k}"
        for k in sorted(b.keys() - a.keys())
        if not _flagged(k, "", flags_differ)
    ]
    for key in sorted(a.keys() & b.keys()):
        left, right = a[key], b[key]
        for path in sorted(left.keys() | right.keys()):
            if left.get(path) == right.get(path) or _customers(key, path):
                continue
            if not _flagged(key, path, flags_differ):
                diffs.append(f"{key} {path}: {left.get(path)} != {right.get(path)}")
    return diffs


def platform_outputs() -> Json:
    return json.loads(pulumi("stack", "output", "--stack", n.PLATFORM_STACK, "--json"))


def project_policies(label: str) -> list[str]:
    """Constraints set on the cell's project itself, not inherited from its folders."""
    found: list[Mapping[str, Json]] = (
        gcloud_json(
            "org-policies",
            "list",
            f"--project={n.cell_project(label)}",
            f"--billing-project={n.BOOTSTRAP_PROJECT}",
        )
        or []
    )
    return [str(p["constraint"]) for p in found]


def in_force(
    platform: Mapping[str, Json], state: Json, on_project: Sequence[str]
) -> tuple[list[str], list[str]]:
    """The folder's policies a cell inherits, and each thing that would override them."""
    table: Mapping[str, str] = platform.get("cell_policies") or {}
    stages: Mapping[str, Json] = platform.get("stage_folder_ids") or {}
    folders = {str(f) for f in stages.values()}
    resources: list[Mapping[str, Json]] = state["deployment"].get("resources", [])
    overrides = [] if table else ["the platform stack exports no cell policies"]
    for res in resources:
        name = res["urn"].rsplit("::", 1)[-1]
        if res["type"] == PROJECT_TYPE:
            folder = str(res.get("inputs", {}).get("folderId") or "")
            if folder.removeprefix("folders/") not in folders:
                overrides.append(f"project in folder {folder or '?'}, not a stage folder")
        elif res["type"].startswith("gcp:orgpolicy/"):
            overrides.append(f"the cell stack declares {res['type']}::{name}")
    if not any(res["type"] == PROJECT_TYPE for res in resources):
        overrides.append("no project in the cell stack")
    overrides += [f"set on the project: {constraint}" for constraint in on_project]
    return [f"{c}: {s}" for c, s in sorted(table.items())], overrides


def _report_policies(labels: Sequence[str], exports: Sequence[Json]) -> int:
    try:
        platform = platform_outputs()
        on_project = [project_policies(label) for label in labels]
    except (CommandError, ValueError) as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    failed = 0
    for label, state, local in zip(labels, exports, on_project, strict=True):
        lines, overrides = in_force(platform, state, local)
        print(f"policies in force on {n.cell_project(label)}:")  # noqa: T201
        for line in lines:
            print(f"  {line}")  # noqa: T201
        for line in overrides:
            print(f"  override: {line}")  # noqa: T201
        failed |= bool(overrides)
    return failed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    try:
        exports = [export(n.cell_stack(label)) for label in argv]
    except (CommandError, ValueError) as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    states = [normalise(state, label) for state, label in zip(exports, argv, strict=True)]
    flags_differ = differing(flags(exports[0]), flags(exports[1]))
    if flags_differ:
        left_out = ", ".join(flags_differ)
        print(f"flags differ, their resources left out: {left_out}", file=sys.stderr)  # noqa: T201
    diffs = compare(*states, flags_differ=flags_differ)
    for line in diffs:
        print(line)  # noqa: T201
    count = len(states[0])
    print(f"{count} resources compared, {len(diffs)} difference(s)", file=sys.stderr)  # noqa: T201
    policies_failed = _report_policies(argv, exports)
    return 1 if diffs or count == 0 or policies_failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

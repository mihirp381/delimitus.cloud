"""Done-when check 1: two cells are identical once their label, project number, addresses and
timestamps are taken out.

    uv run python -m ssc_infra.cell_diff testcell01 testcell02

Compares ``pulumi stack export`` of both stacks: the same resources, the same inputs, and the same
outputs apart from values the cloud assigns. Prints each difference; exit 1 if there is any.
"""

import json
import re
import sys
from collections.abc import Mapping
from typing import Any, Final

from ssc_infra import naming as n
from ssc_infra.run import CommandError, pulumi

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
IPV4: Final = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def export(stack: str) -> Json:
    return json.loads(pulumi("stack", "export", "--stack", stack))


def _swap(value: str, label: str, number: str, *, outputs: bool) -> str:
    """Inputs keep their addresses (fixed ranges); outputs lose them (the cloud assigns them)."""
    out = value.replace(label, "<cell>")
    if number:
        out = out.replace(number, "<number>")
    return IPV4.sub("<ip>", out) if outputs else out


def flatten(value: Json, label: str, number: str, prefix: str = "", *, outputs: bool) -> Flat:
    """Dotted paths to normalised scalars. Null counts as absent, as the provider writes either.

    For outputs, keys the cloud assigns are left out.
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
            flat |= flatten(
                inner, label, number, f"{prefix}.{key}" if prefix else key, outputs=outputs
            )
    elif isinstance(value, list):
        for i, inner in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            flat |= flatten(inner, label, number, f"{prefix}[{i}]", outputs=outputs)
    else:
        flat[prefix] = _swap(json.dumps(value), label, number, outputs=outputs)
    return flat


def _stack_outputs(state: Json) -> Mapping[str, Json]:
    for res in state["deployment"].get("resources", []):
        if res["type"] == "pulumi:pulumi:Stack":
            return res.get("outputs", {})
    return {}


def normalise(state: Json, label: str) -> dict[str, Flat]:
    """``type::name`` → flattened inputs and outputs, for every cloud resource in a stack."""
    number = str(_stack_outputs(state).get("project_number", ""))
    out: dict[str, Flat] = {}
    for res in state["deployment"].get("resources", []):
        kind: str = res["type"]
        if kind.startswith(SKIP_TYPES):
            continue
        name = res["urn"].rsplit("::", 1)[-1]
        flat = {
            f"in.{k}": v
            for k, v in flatten(res.get("inputs", {}), label, number, outputs=False).items()
        }
        flat |= {
            f"out.{k}": v
            for k, v in flatten(res.get("outputs", {}), label, number, outputs=True).items()
        }
        out[f"{kind}::{name}"] = flat
    return out


def compare(a: Mapping[str, Flat], b: Mapping[str, Flat]) -> list[str]:
    diffs = [f"only in first: {k}" for k in sorted(a.keys() - b.keys())]
    diffs += [f"only in second: {k}" for k in sorted(b.keys() - a.keys())]
    for key in sorted(a.keys() & b.keys()):
        left, right = a[key], b[key]
        for path in sorted(left.keys() | right.keys()):
            if left.get(path) != right.get(path):
                diffs.append(f"{key} {path}: {left.get(path)} != {right.get(path)}")
    return diffs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)  # noqa: T201
        return 2
    try:
        states = [normalise(export(n.cell_stack(label)), label) for label in argv]
    except (CommandError, ValueError) as exc:
        print(exc, file=sys.stderr)  # noqa: T201
        return 1
    diffs = compare(*states)
    for line in diffs:
        print(line)  # noqa: T201
    count = len(states[0])
    print(f"{count} resources compared, {len(diffs)} difference(s)", file=sys.stderr)  # noqa: T201
    return 1 if diffs or count == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

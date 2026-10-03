"""The organisation policies on the ``ssc-cells`` folder (SSC-095), as one table.

The platform stack applies this table; ``violations`` checks a cell stack's declared resources
against the same table, so a cell stack that would be refused at apply fails a local test first.
"""

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ssc_infra import naming as n

type Inputs = Mapping[str, Any]
type Resource = tuple[str, str, Inputs]

LOCATIONS: Final = "in:us-central1-locations"
GLOBAL: Final = "global"
ORG_PRINCIPALS: Final = f"//cloudresourcemanager.googleapis.com/organizations/{n.ORG_ID}"
GOOGLE_PRODUCERS: Final = "under:organizations/433637338589"
PUBLIC_TAG_KEY: Final = "ssc-public-invoker"
PUBLIC_TAG_VALUE: Final = "gateway"
PUBLIC_MEMBERS: Final = frozenset({"allUsers", "allAuthenticatedUsers"})
PUBLIC_SERVICES: Final = frozenset({n.GATEWAY, n.SECRET_INTAKE})
INGRESS: Final = {
    "is:all": "INGRESS_TRAFFIC_ALL",
    "is:internal": "INGRESS_TRAFFIC_INTERNAL_ONLY",
    "is:internal-and-cloud-load-balancing": "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
}
SERVICE_AGENT: Final = re.compile(
    r"^serviceAccount:service-(org-)?\d+@gcp-sa-[a-z0-9-]+\.iam\.gserviceaccount\.com$"
)
OWN_ACCOUNT: Final = re.compile(
    r"^serviceAccount:[a-z0-9-]+@ssc-[a-z0-9-]+\.iam\.gserviceaccount\.com$"
)
DEFAULT_ACCOUNT: Final = re.compile(
    r"^serviceAccount:(\d+-compute@developer|[a-z][a-z0-9-]+@appspot)\.gserviceaccount\.com$"
)
RUN_SERVICE: Final = re.compile(
    r"^//run\.googleapis\.com/projects/[^/]+/locations/[^/]+/services/([^/]+)$"
)


@dataclass(frozen=True, slots=True)
class Rule:
    """One folder policy: ``enforce`` for boolean and managed constraints, ``allowed`` or
    ``deny_all`` for list constraints. ``tag_exception`` turns it off where the public-invoker
    tag is bound, which only the ``PUBLIC_SERVICES`` carry."""

    key: str
    constraint: str
    enforce: bool = False
    allowed: tuple[str, ...] = ()
    deny_all: bool = False
    parameters: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    tag_exception: bool = False

    def summary(self) -> str:
        if self.deny_all:
            text = "deny all"
        elif self.allowed:
            text = "allow " + ", ".join(self.allowed)
        else:
            text = "enforced"
        for name, values in self.parameters.items():
            text += f"; {name} " + ", ".join(values)
        if self.tag_exception:
            text += f"; off where tag {PUBLIC_TAG_KEY}={PUBLIC_TAG_VALUE}"
        return text


def cell_rules(operator: str, peering: Sequence[str] | None = None) -> tuple[Rule, ...]:
    """The table. ``operator`` is the one person who may hold roles in a cell; ``peering`` the
    networks a cell network may peer with, by default Google's service producers (Cloud SQL's
    private services access)."""
    return (
        Rule("locations", "gcp.resourceLocations", allowed=(LOCATIONS, GLOBAL)),
        Rule("public-access-prevention", "storage.publicAccessPrevention", enforce=True),
        Rule(
            "policy-members",
            "iam.managed.allowedPolicyMembers",
            enforce=True,
            parameters={
                "allowedMemberSubjects": (operator,),
                "allowedPrincipalSets": (ORG_PRINCIPALS,),
            },
            tag_exception=True,
        ),
        Rule("sa-keys", "iam.disableServiceAccountKeyCreation", enforce=True),
        Rule("default-grants", "iam.automaticIamGrantsForDefaultServiceAccounts", enforce=True),
        Rule(
            "vpc-peering",
            "compute.restrictVpcPeering",
            allowed=tuple(peering or (GOOGLE_PRODUCERS,)),
        ),
        Rule("shared-vpc", "compute.restrictSharedVpcHostProjects", deny_all=True),
        Rule(
            "run-ingress",
            "run.allowedIngress",
            allowed=("is:internal", "is:internal-and-cloud-load-balancing"),
        ),
        Rule("vm-external-ip", "compute.vmExternalIpAccess", deny_all=True),
        Rule("sql-public-ip", "sql.restrictPublicIp", enforce=True),
    )


def summaries(rules: Iterable[Rule]) -> dict[str, str]:
    return {rule.constraint: rule.summary() for rule in rules}


def _kind(type_: str) -> str:
    return type_.rsplit(":", 1)[-1].lower()


def _members(type_: str, inputs: Inputs) -> list[str]:
    kind = _kind(type_)
    if kind.endswith("iammember"):
        return [str(inputs.get("member", ""))]
    if kind.endswith("iambinding"):
        return [str(m) for m in inputs.get("members", [])]
    if kind.endswith("iampolicy"):
        bindings = json.loads(str(inputs.get("policyData") or "{}")).get("bindings", [])
        return [str(m) for b in bindings for m in b.get("members", [])]
    return []


def _tagged_services(resources: Sequence[Resource], tag: str) -> set[str]:
    found: set[str] = set()
    for type_, _name, inputs in resources:
        if type_.startswith("gcp:tags/") and inputs.get("tagValue") == tag:
            match = RUN_SERVICE.match(str(inputs.get("parent", "")))
            if match:
                found.add(match.group(1))
    return found


def _locations(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    region = n.REGION
    for type_, name, inputs in resources:
        for key in ("region", "location"):
            value = str(inputs.get(key, region)).lower()
            if value not in (GLOBAL, region) and not value.startswith(f"{region}-"):
                yield f"{type_}::{name} {key} {value}"


def _public_access(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, inputs in resources:
        if not type_.startswith("gcp:storage/"):
            continue
        bucket = type_ == "gcp:storage/bucket:Bucket"
        if bucket and inputs.get("publicAccessPrevention") != "enforced":
            yield f"{type_}::{name} publicAccessPrevention not enforced"
        if PUBLIC_MEMBERS & set(_members(type_, inputs)):
            yield f"{type_}::{name} grants a public member"


def _policy_members(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    subjects = set(rule.parameters["allowedMemberSubjects"])
    tagged = _tagged_services(resources, tag)
    for type_, name, inputs in resources:
        if type_.startswith("gcp:tags/") and inputs.get("tagValue") == tag:
            match = RUN_SERVICE.match(str(inputs.get("parent", "")))
            if not match or match.group(1) not in PUBLIC_SERVICES:
                yield f"{type_}::{name} binds the public-invoker tag to {inputs.get('parent')}"
        for member in _members(type_, inputs):
            if member in PUBLIC_MEMBERS:
                on_tagged = type_ == "gcp:cloudrunv2/serviceIamMember:ServiceIamMember" and (
                    inputs.get("name") in tagged
                )
                if not on_tagged:
                    yield f"{type_}::{name} grants {member} outside a tagged public service"
            elif member not in subjects and not (
                OWN_ACCOUNT.match(member) or SERVICE_AGENT.match(member)
            ):
                yield f"{type_}::{name} grants {member}, outside the organisation"


def _sa_keys(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, _inputs in resources:
        if type_ == "gcp:serviceaccount/key:Key":
            yield f"{type_}::{name} creates a service account key"


def _default_grants(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, inputs in resources:
        for member in _members(type_, inputs):
            if DEFAULT_ACCOUNT.match(member):
                yield f"{type_}::{name} grants {inputs.get('role')} to the default {member}"


def _peering(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, inputs in resources:
        if type_ == "gcp:compute/networkPeering:NetworkPeering":
            yield f"{type_}::{name} peers with {inputs.get('peerNetwork')}"
        if (
            type_ == "gcp:servicenetworking/connection:Connection"
            and inputs.get("service") != "servicenetworking.googleapis.com"
        ):
            yield f"{type_}::{name} peers with {inputs.get('service')}"


def _shared_vpc(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, _inputs in resources:
        if type_.startswith("gcp:compute/sharedVPC"):
            yield f"{type_}::{name} joins a Shared VPC"


def _ingress(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    allowed = {INGRESS[value] for value in rule.allowed}
    for type_, name, inputs in resources:
        if type_ == "gcp:cloudrunv2/service:Service":
            ingress = inputs.get("ingress", INGRESS["is:all"])
            if ingress not in allowed:
                yield f"{type_}::{name} ingress {ingress}"
        elif type_ == "gcp:cloudrun/service:Service":
            yield f"{type_}::{name} uses the v1 API; use cloudrunv2"


def _vm_external_ip(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, inputs in resources:
        if type_ in (
            "gcp:compute/instance:Instance",
            "gcp:compute/instanceTemplate:InstanceTemplate",
            "gcp:compute/regionInstanceTemplate:RegionInstanceTemplate",
        ):
            for nic in inputs.get("networkInterfaces", []):
                if nic.get("accessConfigs"):
                    yield f"{type_}::{name} gives a VM an external address"


def _sql_public_ip(rule: Rule, resources: Sequence[Resource], tag: str) -> Iterator[str]:
    for type_, name, inputs in resources:
        if type_ == "gcp:sql/databaseInstance:DatabaseInstance":
            ip = inputs.get("settings", {}).get("ipConfiguration", {})
            if ip.get("ipv4Enabled", True) is not False:
                yield f"{type_}::{name} has a public address"


type Check = Callable[[Rule, Sequence[Resource], str], Iterator[str]]
CHECKS: Final[dict[str, Check]] = {
    "gcp.resourceLocations": _locations,
    "storage.publicAccessPrevention": _public_access,
    "iam.managed.allowedPolicyMembers": _policy_members,
    "iam.disableServiceAccountKeyCreation": _sa_keys,
    "iam.automaticIamGrantsForDefaultServiceAccounts": _default_grants,
    "compute.restrictVpcPeering": _peering,
    "compute.restrictSharedVpcHostProjects": _shared_vpc,
    "run.allowedIngress": _ingress,
    "compute.vmExternalIpAccess": _vm_external_ip,
    "sql.restrictPublicIp": _sql_public_ip,
}


def violations(
    rules: Iterable[Rule], resources: Iterable[Resource], tag: str
) -> dict[str, list[str]]:
    """Constraint → what in ``resources`` it would refuse. ``tag`` is the public-invoker tag
    value's ID (``tagValues/<n>``)."""
    declared = list(resources)
    found: dict[str, list[str]] = {}
    for rule in rules:
        hits = list(CHECKS[rule.constraint](rule, declared, tag))
        if hits:
            found[rule.constraint] = hits
    return found

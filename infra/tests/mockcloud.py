"""Runs a stack's program against Pulumi mocks and records every resource it declares."""

import asyncio
import zlib
from dataclasses import dataclass
from typing import Any

import pulumi
from pulumi.runtime import config as runtime_config

from ssc_infra import cell, naming, platform

FOLDERS = {"prod": "111111111111", "staging": "222222222222"}
NIGHTLY = naming.sa_email(naming.NIGHTLY_SA, naming.control_project("staging"))
CONTROL = {s: naming.sa_email(naming.CONTROL_SA, naming.control_project(s)) for s in naming.STAGES}
WORKERS = {
    s: naming.sa_email(naming.CONTROL_WORKER_SA, naming.control_project(s)) for s in naming.STAGES
}
PUBLIC_TAG = "tagValues/555555555555"
DEPLOYER = naming.sa_email(naming.DEPLOYER, naming.BOOTSTRAP_PROJECT)


@dataclass(frozen=True, slots=True)
class Declared:
    type: str
    name: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]


def project_number(project_id: str) -> str:
    return str(100_000_000_000 + zlib.crc32(project_id.encode()))


def entry_address(project_id: str) -> str:
    """A different public address for each project, as the cloud would assign."""
    crc = zlib.crc32(project_id.encode())
    return f"34.{crc >> 16 & 255}.{crc >> 8 & 255}.{crc & 255}"


def nat_address(project_id: str) -> str:
    """A different fixed outbound address for each project, as the cloud would assign."""
    crc = zlib.crc32(project_id.encode())
    return f"35.{crc >> 16 & 255}.{crc >> 8 & 255}.{crc & 255}"


class Recorder(pulumi.runtime.Mocks):
    def __init__(self, platform_outputs: dict[str, Any] | None = None) -> None:
        self.declared: list[Declared] = []
        self.platform_outputs = platform_outputs or {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict[str, Any]]:
        if args.typ == "pulumi:pulumi:StackReference":
            outputs = {
                "stage_folder_ids": FOLDERS,
                "control_service_accounts": CONTROL,
                "control_workers": WORKERS,
                "nightly_service_account": NIGHTLY,
                "public_invoker_tag": PUBLIC_TAG,
                "cell_deployer": {"service_account": DEPLOYER},
                **self.platform_outputs,
            }
            return f"{args.name}-id", {"name": args.name, "outputs": outputs}
        state = dict(args.inputs)
        project = state.get("project") or state.get("projectId") or ""
        match args.typ:
            case "gcp:organizations/project:Project":
                state["number"] = project_number(state["projectId"])
            case "gcp:organizations/folder:Folder":
                folder_id = str(900_000_000_000 + zlib.crc32(args.name.encode()))
                state |= {"folderId": folder_id, "name": f"folders/{folder_id}"}
            case "gcp:serviceaccount/account:Account":
                email = naming.sa_email(state["accountId"], project)
                state |= {
                    "email": email,
                    "member": f"serviceAccount:{email}",
                    "name": f"projects/{project}/serviceAccounts/{email}",
                }
            case "gcp:projects/serviceIdentity:ServiceIdentity":
                agent = (
                    f"service-{project_number(project)}@gcp-sa-{args.name}.iam.gserviceaccount.com"
                )
                state |= {"email": agent, "member": f"serviceAccount:{agent}"}
            case "gcp:iam/workloadIdentityPool:WorkloadIdentityPool":
                number = project_number(project)
                state["name"] = (
                    f"projects/{number}/locations/global/workloadIdentityPools/"
                    f"{state['workloadIdentityPoolId']}"
                )
            case "gcp:tags/tagKey:TagKey" | "gcp:tags/tagValue:TagValue":
                state["name"] = str(700_000_000_000 + zlib.crc32(args.name.encode()))
            case "gcp:monitoring/notificationChannel:NotificationChannel":
                state["name"] = (
                    f"projects/{project}/notificationChannels/{zlib.crc32(project.encode())}"
                )
            case "gcp:projects/iAMCustomRole:IAMCustomRole":
                state["name"] = f"projects/{project}/roles/{state['roleId']}"
            case "gcp:compute/globalAddress:GlobalAddress" if (
                state.get("addressType") != "INTERNAL"
            ):
                state["address"] = entry_address(project)
            case "gcp:compute/address:Address" if state.get("addressType") == "EXTERNAL":
                state["address"] = nat_address(project)
            case "gcp:certificatemanager/dnsAuthorization:DnsAuthorization":
                state["dnsResourceRecords"] = [
                    {
                        "name": f"_acme-challenge.{state['domain']}.",
                        "type": "CNAME",
                        "data": f"{zlib.crc32(project.encode()):08x}.7.authorize."
                        "certificatemanager.goog.",
                    }
                ]
            case "gcp:sql/databaseInstance:DatabaseInstance":
                crc = zlib.crc32(project.encode())
                state |= {
                    "connectionName": f"{project}:{state['region']}:{state['name']}",
                    "dnsName": "",
                    "dnsNames": [
                        {
                            "connectionType": "PRIVATE_SERVICES_ACCESS",
                            "dnsScope": "INSTANCE",
                            "name": f"{crc:08x}.{crc:08x}.{state['region']}.sql-psa.goog.",
                        }
                    ],
                    "privateIpAddress": f"10.21.0.{3 + crc % 250}",
                }
            case "gcp:dns/managedZone:ManagedZone" if state.get("visibility") == "public":
                state["nameServers"] = [f"ns-cloud-a{i}.googledomains.com." for i in range(1, 5)]
            case _:
                pass
        if args.typ != "pulumi:providers:gcp":
            self.declared.append(Declared(args.typ, args.name, dict(args.inputs), state))
        return f"{args.name}-id", state

    def call(
        self, args: pulumi.runtime.MockCallArgs
    ) -> tuple[dict[str, Any], list[tuple[str, str]] | None]:
        if args.token == "gcp:storage/getProjectServiceAccount:getProjectServiceAccount":
            number = project_number(str(args.args["project"]))
            agent = f"service-{number}@gs-project-accounts.iam.gserviceaccount.com"
            return {"emailAddress": agent}, []
        return {}, []


def run(
    stack: str,
    config: dict[str, str] | None = None,
    platform_outputs: dict[str, Any] | None = None,
) -> list[Declared]:
    recorder = Recorder(platform_outputs)
    asyncio.set_event_loop(asyncio.new_event_loop())  # 3.14 makes no loop implicitly
    pulumi.runtime.set_mocks(recorder, project=naming.PROJECT, stack=stack, preview=False)
    runtime_config.set_all_config({f"{naming.PROJECT}:{k}": v for k, v in (config or {}).items()})

    @pulumi.runtime.test
    def program() -> None:
        if stack == naming.PLATFORM_STACK:
            platform.build()
        else:
            cell.build(stack)

    program()
    return recorder.declared


def one(declared: list[Declared], type_: str, name: str | None = None) -> Declared:
    found = [d for d in declared if d.type == type_ and (name is None or d.name == name)]
    assert len(found) == 1, f"{type_} {name}: {len(found)} declared"
    return found[0]


def as_export(
    declared: list[Declared],
    label: str,
    flags: dict[str, Any] | None = None,
    config: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The shape of ``pulumi stack export`` for ``cell_diff``."""
    stack = naming.cell_stack(label)
    project = naming.cell_project(label)
    outputs: dict[str, Any] = {
        "project_number": project_number(project),
        "entry_address": entry_address(project),
        "nat_ip": nat_address(project),
    }
    if flags is not None:
        outputs["flags"] = flags
    if config is not None:
        outputs["config"] = config
    resources: list[dict[str, Any]] = [
        {
            "type": "pulumi:pulumi:Stack",
            "urn": f"urn:pulumi:{stack}::{naming.PROJECT}::pulumi:pulumi:Stack::root",
            "outputs": outputs,
        }
    ]
    resources += [
        {
            "type": d.type,
            "urn": f"urn:pulumi:{stack}::{naming.PROJECT}::{d.type}::{d.name}",
            "inputs": d.inputs,
            "outputs": d.outputs,
        }
        for d in declared
    ]
    return {"deployment": {"resources": resources}}

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


@dataclass(frozen=True, slots=True)
class Declared:
    type: str
    name: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]


def project_number(project_id: str) -> str:
    return str(100_000_000_000 + zlib.crc32(project_id.encode()))


class Recorder(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.declared: list[Declared] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict[str, Any]]:
        if args.typ == "pulumi:pulumi:StackReference":
            outputs = {
                "stage_folder_ids": FOLDERS,
                "control_service_accounts": CONTROL,
                "nightly_service_account": NIGHTLY,
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
                agent = f"service-{project_number(project)}@{args.name}.iam.gserviceaccount.com"
                state |= {"email": agent, "member": f"serviceAccount:{agent}"}
            case "gcp:iam/workloadIdentityPool:WorkloadIdentityPool":
                number = project_number(project)
                state["name"] = (
                    f"projects/{number}/locations/global/workloadIdentityPools/"
                    f"{state['workloadIdentityPoolId']}"
                )
            case "gcp:projects/iAMCustomRole:IAMCustomRole":
                state["name"] = f"projects/{project}/roles/{state['roleId']}"
            case _:
                pass
        if args.typ != "pulumi:providers:gcp":
            self.declared.append(Declared(args.typ, args.name, dict(args.inputs), state))
        return f"{args.name}-id", state

    def call(
        self, args: pulumi.runtime.MockCallArgs
    ) -> tuple[dict[str, Any], list[tuple[str, str]] | None]:
        return {}, None


def run(stack: str, config: dict[str, str] | None = None) -> list[Declared]:
    recorder = Recorder()
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
    declared: list[Declared], label: str, flags: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The shape of ``pulumi stack export`` for ``cell_diff``."""
    stack = naming.cell_stack(label)
    outputs: dict[str, Any] = {"project_number": project_number(naming.cell_project(label))}
    if flags is not None:
        outputs["flags"] = flags
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

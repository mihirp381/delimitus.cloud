"""Make what Pulumi itself needs before it can run: the ``ssc-platform`` folder (logs in-region
first), the ``ssc-platform-0`` project, its APIs, the state bucket and the secrets key. Then the
``platform`` stack with the folder's ID in its config. Safe to run again, and on a fresh clone,
where it rewrites the stack files (they are not committed).

    uv run python -m ssc_infra.bootstrap
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from ssc_infra import naming as n
from ssc_infra.run import CommandError, gcloud, gcloud_json, pulumi

INFRA_DIR: Final = str(Path(__file__).resolve().parent.parent)
# Every API a stack calls: with ``user_project_override`` the quota project is this one.
PLATFORM_APIS: Final = (
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "orgpolicy.googleapis.com",
    "privilegedaccessmanager.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
)
KEY_ROTATION_DAYS: Final = 90


def say(msg: str) -> None:
    print(msg, file=sys.stderr)  # noqa: T201


def platform_folder() -> str:
    found: list[dict[str, object]] = (
        gcloud_json(
            "resource-manager",
            "folders",
            "list",
            f"--organization={n.ORG_ID}",
            f"--filter=displayName={n.PLATFORM_FOLDER}",
        )
        or []
    )
    names = [str(f["name"]) for f in found]
    if len(names) > 1:
        raise CommandError(f"{len(names)} folders named {n.PLATFORM_FOLDER}")
    if names:
        return names[0].removeprefix("folders/")
    say(f"creating folder {n.PLATFORM_FOLDER}")
    made = gcloud_json(
        "resource-manager",
        "folders",
        "create",
        f"--display-name={n.PLATFORM_FOLDER}",
        f"--organization={n.ORG_ID}",
    )
    return str(made["name"]).removeprefix("folders/")


def log_location(folder_id: str) -> None:
    gcloud(
        "logging", "settings", "update", f"--folder={folder_id}", f"--storage-location={n.REGION}"
    )


def project(folder_id: str) -> None:
    exists = (
        gcloud(
            "projects",
            "describe",
            n.BOOTSTRAP_PROJECT,
            "--format=value(projectId)",
            ok_codes=(0, 1),
        ).returncode
        == 0
    )
    if not exists:
        say(f"creating project {n.BOOTSTRAP_PROJECT}")
        gcloud(
            "projects",
            "create",
            n.BOOTSTRAP_PROJECT,
            f"--folder={folder_id}",
            "--labels=ssc-managed=bootstrap",
        )
    gcloud(
        "billing", "projects", "link", n.BOOTSTRAP_PROJECT, f"--billing-account={n.BILLING_ACCOUNT}"
    )
    gcloud("services", "enable", *PLATFORM_APIS, f"--project={n.BOOTSTRAP_PROJECT}")


def state_bucket() -> None:
    url = f"gs://{n.STATE_BUCKET}"
    if gcloud(
        "storage", "buckets", "describe", url, "--format=value(name)", ok_codes=(0, 1)
    ).returncode:
        say(f"creating {url}")
        gcloud(
            "storage",
            "buckets",
            "create",
            url,
            f"--project={n.BOOTSTRAP_PROJECT}",
            f"--location={n.REGION}",
            "--uniform-bucket-level-access",
            "--public-access-prevention",
        )
    gcloud("storage", "buckets", "update", url, "--versioning")


def secrets_key() -> None:
    where = (f"--location={n.REGION}", f"--project={n.BOOTSTRAP_PROJECT}")
    if gcloud("kms", "keyrings", "describe", n.KMS_RING, *where, ok_codes=(0, 1)).returncode:
        gcloud("kms", "keyrings", "create", n.KMS_RING, *where)
    if gcloud(
        "kms", "keys", "describe", n.KMS_KEY, f"--keyring={n.KMS_RING}", *where, ok_codes=(0, 1)
    ).returncode:
        first = datetime.now(UTC) + timedelta(days=KEY_ROTATION_DAYS)
        gcloud(
            "kms",
            "keys",
            "create",
            n.KMS_KEY,
            f"--keyring={n.KMS_RING}",
            *where,
            "--purpose=encryption",
            f"--rotation-period={KEY_ROTATION_DAYS}d",
            f"--next-rotation-time={first:%Y-%m-%dT%H:%M:%SZ}",
        )


def stack(name: str, config: dict[str, str]) -> None:
    """Stack files are not committed. A missing one gets the KMS secrets provider back, or
    Pulumi would assume a passphrase; Pulumi then wraps a new data key with it."""
    stacks = pulumi("stack", "ls", "--json", cwd=INFRA_DIR)
    stack_file = Path(INFRA_DIR) / f"Pulumi.{name}.yaml"
    if f'"name": "{name}"' not in stacks:
        pulumi("stack", "init", name, f"--secrets-provider={n.SECRETS_PROVIDER}", cwd=INFRA_DIR)
    elif not stack_file.exists():
        stack_file.write_text(f"secretsprovider: {n.SECRETS_PROVIDER}\n")
    for key, value in ({"gcp:disableGlobalProjectWarning": "true"} | config).items():
        pulumi("config", "set", "--stack", name, key, value, cwd=INFRA_DIR)


def platform_stack(folder_id: str) -> None:
    stack(n.PLATFORM_STACK, {"platform_folder_id": folder_id})


def cell_stack(label: str, *, probe: bool) -> None:
    stack(n.cell_stack(label), {"stage": "staging", "probe": "true" if probe else "false"})


def main(argv: list[str]) -> int:
    """No arguments: the platform bootstrap. ``cell <label> [--probe]``: a staging cell stack."""
    try:
        if argv[:1] == ["cell"] and len(argv) in (2, 3):
            cell_stack(argv[1], probe="--probe" in argv[2:])
            say(f"stack {n.cell_stack(argv[1])} ready")
            return 0
        if argv:
            say(__doc__ or "")
            return 2
        folder_id = platform_folder()
        log_location(folder_id)
        project(folder_id)
        state_bucket()
        secrets_key()
        platform_stack(folder_id)
    except CommandError as exc:
        say(str(exc))
        return 1
    say(f"ready: PULUMI_BACKEND_URL=gs://{n.STATE_BUCKET} pulumi up --stack {n.PLATFORM_STACK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

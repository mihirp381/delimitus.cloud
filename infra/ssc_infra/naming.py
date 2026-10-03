"""Every name SSC gives a Google Cloud resource, in one place (decision 021)."""

from typing import Final, Literal

from ssc_shared.hosts import check_cell_label

type Stage = Literal["prod", "staging"]

ORG_ID: Final = "878392300952"
BILLING_ACCOUNT: Final = "0103B6-DAEFDE-DA776C"
REGION: Final = "us-central1"
OPERATOR: Final = "user:mihirp381@gmail.com"

PLATFORM_FOLDER: Final = "ssc-platform"
CELLS_FOLDER: Final = "ssc-cells"
SANDBOX_FOLDER: Final = "ssc-sandbox"
STAGES: Final[tuple[Stage, ...]] = ("prod", "staging")

BOOTSTRAP_PROJECT: Final = "ssc-platform-0"
STATE_BUCKET: Final = f"{BOOTSTRAP_PROJECT}-pulumi"
KMS_RING: Final = "ssc-platform"
KMS_KEY: Final = "pulumi-secrets"
SECRETS_PROVIDER: Final = (
    f"gcpkms://projects/{BOOTSTRAP_PROJECT}/locations/{REGION}"
    f"/keyRings/{KMS_RING}/cryptoKeys/{KMS_KEY}"
)

PROJECT: Final = "ssc-infra"
PLATFORM_STACK: Final = "platform"
CELL_STACK_PREFIX: Final = "c-"

CONTROL_SA: Final = "ssc-control"
CELL_LABEL_KEY: Final = "ssc-cell"
APP_PREFIX: Final = "ssc-a-"
PROBE_SECRET: Final = f"{APP_PREFIX}probe"
PROBE_ALLOWED_SA: Final = f"{APP_PREFIX}probe"
PROBE_DENIED_SA: Final = "ssc-deny-probe"
# The two runtime-probe apps (SSC-017): ``a`` runs the probes, ``b`` is the peer it must not reach.
PROBE_ENVS: Final = ("env_probe00000000000000a", "env_probe00000000000000b")
PROBE_RUNNER: Final = "ssc-probe-runner"
NIGHTLY_SA: Final = "ssc-nightly"
GITHUB_REPOSITORY: Final = "mihirp381/delimitus.cloud"
NIGHTLY_WORKFLOW: Final = ".github/workflows/nightly.yml"
SECRET_READ: Final = "secretmanager.googleapis.com/versions.access"  # noqa: S105

GATEWAY: Final = "ssc-gateway"
DATA_GATEWAY: Final = "ssc-datagw"
FLAGS: Final = ("database", "egress", "connections", "gateway_min", "warm")
LAZY_RESOURCES: Final[dict[str, frozenset[str]]] = {
    "database": frozenset(
        {"gcp:sql/databaseInstance:DatabaseInstance::sql", "gcp:sql/user:User::sql-agent"}
    ),
    "egress": frozenset(
        {
            "gcp:compute/instanceTemplate:InstanceTemplate::proxy-template",
            "gcp:compute/instanceGroupManager:InstanceGroupManager::proxy",
        }
    ),
    "connections": frozenset({f"gcp:cloudrunv2/service:Service::{DATA_GATEWAY}"}),
}
GATEWAY_SERVICE: Final = f"gcp:cloudrunv2/service:Service::{GATEWAY}"
GATEWAY_MIN_PATH: Final = "template.scaling.minInstanceCount"


def control_project(stage: Stage) -> str:
    return f"ssc-control-{stage}"


def cell_project(label: str) -> str:
    return f"ssc-c-{check_cell_label(label)}"


def cell_bucket(label: str) -> str:
    return f"{cell_project(label)}-cell"


def cell_stack(label: str) -> str:
    return f"{CELL_STACK_PREFIX}{check_cell_label(label)}"


def label_of_stack(stack: str) -> str:
    if not stack.startswith(CELL_STACK_PREFIX):
        raise ValueError(f"a cell stack is {CELL_STACK_PREFIX}<cell label>, not {stack!r}")
    return check_cell_label(stack.removeprefix(CELL_STACK_PREFIX))


def sa_email(account: str, project: str) -> str:
    return f"{account}@{project}.iam.gserviceaccount.com"


def run_url(service: str, project_number: str) -> str:
    """A Cloud Run service's deterministic URL."""
    return f"https://{service}-{project_number}.{REGION}.run.app"


def platform_stack_ref() -> str:
    return f"organization/{PROJECT}/{PLATFORM_STACK}"

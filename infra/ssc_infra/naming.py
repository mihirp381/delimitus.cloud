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
CONTROL_WORKER_SA: Final = "ssc-control-worker"
AUTH_SA: Final = "ssc-auth"
MIGRATE_SA: Final = "ssc-control-migrate"
CELL_LABEL_KEY: Final = "ssc-cell"
APP_PREFIX: Final = "ssc-a-"
PROBE_SECRET: Final = f"{APP_PREFIX}probe"
PROBE_ALLOWED_SA: Final = f"{APP_PREFIX}probe"
PROBE_DENIED_SA: Final = "ssc-deny-probe"
# The two runtime-probe apps (SSC-017): ``a`` runs the probes, ``b`` is the peer it must not reach.
PROBE_ENVS: Final = ("env_probe00000000000000a", "env_probe00000000000000b")
PROBE_RUNNER: Final = "ssc-probe-runner"
NIGHTLY_SA: Final = "ssc-nightly"
DEPLOYER: Final = "ssc-cell-deployer"
PLATFORM_REPOSITORY: Final = "ssc-platform"
GITHUB_REPOSITORY: Final = "mihirp381/delimitus.cloud"
NIGHTLY_WORKFLOW: Final = ".github/workflows/nightly.yml"
SECRET_READ: Final = "secretmanager.googleapis.com/versions.access"  # noqa: S105

GATEWAY: Final = "ssc-gateway"
CELL_AGENT: Final = "ssc-cell-agent"
SECRET_INTAKE: Final = "ssc-secret-intake"  # noqa: S105
DATA_GATEWAY: Final = "ssc-datagw"
FLAGS: Final = ("database", "egress", "connections", "gateway_min", "warm")
LAZY_RESOURCES: Final[dict[str, frozenset[str]]] = {
    "database": frozenset(
        {
            "gcp:sql/databaseInstance:DatabaseInstance::sql",
            "gcp:sql/user:User::sql-agent",
            "gcp:dns/recordSet:RecordSet::sql-dns",
        }
    ),
    "egress": frozenset(
        {
            "gcp:compute/instanceTemplate:InstanceTemplate::proxy-template",
            "gcp:compute/instanceGroupManager:InstanceGroupManager::proxy",
        }
    ),
    "connections": frozenset({f"gcp:cloudrunv2/service:Service::{DATA_GATEWAY}"}),
}
LAZY_FLAGS: Final = tuple(LAZY_RESOURCES)
GATEWAY_SERVICE: Final = f"gcp:cloudrunv2/service:Service::{GATEWAY}"
GATEWAY_MIN_PATH: Final = "template.scaling.minInstanceCount"
AGENT_SERVICE: Final = f"gcp:cloudrunv2/service:Service::{CELL_AGENT}"
SQL_INSTANCE_ENV: Final = "SSC_SQL_INSTANCE"

APPS_DOMAIN: Final = "delimitusapps.com"
PLATFORM_DOMAIN: Final = "delimitus.com"
API_HOST: Final = f"api.{PLATFORM_DOMAIN}"
AUTH_HOST: Final = f"auth.{PLATFORM_DOMAIN}"
KEYS_HOST: Final = f"keys.{PLATFORM_DOMAIN}"
CONTROL_HOSTS: Final = (API_HOST, AUTH_HOST, KEYS_HOST)
GATEWAY_PLATFORM_HOSTS: Final = (AUTH_HOST, KEYS_HOST)
APPS_ZONE: Final = "delimitusapps"
PLATFORM_ZONE: Final = "delimitus"
AGENT_HOST_LABEL: Final = "ssc--agent"
INTAKE_HOST_LABEL: Final = "ssc--secrets"


def control_project(stage: Stage) -> str:
    return f"ssc-control-{stage}"


def cell_project(label: str) -> str:
    return f"ssc-c-{check_cell_label(label)}"


def cell_bucket(label: str) -> str:
    return f"{cell_project(label)}-cell"


CELL_BUCKET_TEMPLATE: Final = "ssc-c-{cell}-cell"


def control_bucket(stage: Stage, purpose: str) -> str:
    return f"{control_project(stage)}-{purpose}"


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


def host_suffix(label: str) -> str:
    """Every public host of a cell ends in ``.<this>``: ``<cell label>.<apps domain>``."""
    return f"{check_cell_label(label)}.{APPS_DOMAIN}"


def cell_wildcard(label: str) -> str:
    return f"*.{host_suffix(label)}"


def agent_host(label: str) -> str:
    """The cell agent's reserved host (SSC-095). A slug never holds ``--``, so no app is ever it."""
    return f"{AGENT_HOST_LABEL}.{host_suffix(label)}"


def agent_url(label: str) -> str:
    """The cell agent's URL through the cell's load balancer, and its ID token audience."""
    return f"https://{agent_host(label)}"


def intake_host(label: str) -> str:
    """The secret intake's reserved host (SSC-026), held off app slugs as the agent's is."""
    return f"{INTAKE_HOST_LABEL}.{host_suffix(label)}"


def intake_url(label: str) -> str:
    """The secret intake's origin through the cell's load balancer, the audience of each grant."""
    return f"https://{intake_host(label)}"


def origin(host: str) -> str:
    return f"https://{host}"


def identity_issuer(label: str) -> str:
    """The ``iss`` of a cell's app identity tokens; ``<this>/jwks.json`` serves its public keys."""
    return f"https://{KEYS_HOST}/{check_cell_label(label)}"


def deployer_job() -> str:
    """The cell deployer job's full name, as the worker's ``SSC_CELL_DEPLOYER_JOB``."""
    return f"projects/{BOOTSTRAP_PROJECT}/locations/{REGION}/jobs/{DEPLOYER}"


def platform_registry() -> str:
    """The platform's own images in ``ssc-platform-0``: the cell deployer, the build tools image
    and the Railpack frontend mirror."""
    return f"{REGION}-docker.pkg.dev/{BOOTSTRAP_PROJECT}/{PLATFORM_REPOSITORY}"


def platform_stack_ref() -> str:
    return f"organization/{PROJECT}/{PLATFORM_STACK}"

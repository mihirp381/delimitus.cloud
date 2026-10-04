"""The control plane in each ``ssc-control-<stage>`` project (SSC-064): the API, the worker and
the auth host on Cloud Run, each as its own account; the control database and its migration
job; the secrets, each readable only by the processes that use it; and, in the public stage
only, the load balancer behind ``api``, ``auth`` and ``keys.delimitus.com``.

Until ``control_image`` names a build of ``packages/ssc_control/Dockerfile`` in the platform
registry, every service runs the placeholder image with no settings, the worker pool runs no
instance and there is no migration job. Secret values never pass through this program: each
secret is an empty container that the operator adds versions to.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

import pulumi
import pulumi_gcp as gcp

from ssc_infra import naming as n
from ssc_shared.hosts import check_cell_label

PLACEHOLDER_IMAGE: Final = "us-docker.pkg.dev/cloudrun/container/hello"
PINNED_IMAGE: Final = re.compile(
    rf"{re.escape(n.platform_registry())}(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{{64}}"
)
PRIVATE_JWK_MEMBERS: Final = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})

API: Final = "api"
WORKER: Final = "worker"
AUTH: Final = "auth"
MIGRATE: Final = "migrate"
ACCOUNTS: Final = {
    API: n.CONTROL_SA,
    WORKER: n.CONTROL_WORKER_SA,
    AUTH: n.AUTH_SA,
    MIGRATE: n.MIGRATE_SA,
}
DISPLAY: Final = {
    API: "SSC control plane",
    WORKER: "SSC control-plane worker",
    AUTH: "SSC auth host",
    MIGRATE: "SSC control-plane migrations",
}
SIGNERS: Final = (API, WORKER)
APIS: Final = (
    "compute.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
)
TIMER_KEY: Final = "SSC_TIMER_SIGNING_KEY"  # noqa: S105  (a secret's name)
"""Made, and given to the worker, only once ``timer_key_id`` is set (SSC-041)."""
TIMER_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SECRET_READERS: Final[Mapping[str, tuple[str, ...]]] = {
    "SSC_DATABASE_DSN": (API, WORKER, AUTH),
    "SSC_MIGRATE_DSN": (MIGRATE,),
    "SSC_METRICS_KEY": (API, WORKER),
    "SSC_WORKOS_API_KEY": (WORKER, AUTH),
    "SSC_WORKOS_CLIENT_ID": (WORKER, AUTH),
    "SSC_AUTH_SIGNING_KEY": (AUTH,),
    "SSC_AUTH_STATE_KEY": (AUTH,),
    TIMER_KEY: (WORKER,),
}
SECRET_ACCESSOR: Final = "roles/secretmanager.secretAccessor"  # noqa: S105
SQL_INSTANCE: Final = "ssc-control"
SQL_DATABASE: Final = "ssc"
SQL_TIER: Final = "db-f1-micro"
SQL_MAX_CONNECTIONS: Final = "50"
SQL_VOLUME: Final = "cloudsql"
SQL_MOUNT: Final = "/cloudsql"
API_SERVICE: Final = "ssc-api"
AUTH_SERVICE: Final = "ssc-auth"
WORKER_POOL: Final = "ssc-worker"
MIGRATE_JOB: Final = "ssc-control-migrate"
API_COMMAND: Final = ("python", "-m", "ssc_control.api")
AUTH_COMMAND: Final = ("python", "-m", "ssc_control.identity", "serve")
WORKER_COMMAND: Final = ("python", "-m", "ssc_control.worker")
MIGRATE_COMMAND: Final = (
    "python",
    "-c",
    "import os; from ssc_control.db.migrate import upgrade; upgrade(os.environ['SSC_MIGRATE_DSN'])",
)
API_MIN: Final[Mapping[n.Stage, int]] = {"staging": 0, "prod": 1}
API_MAX: Final = 3
AUTH_MAX: Final = 2
LIMITS: Final = {"cpu": "1", "memory": "512Mi"}
MIGRATE_TIMEOUT: Final = "600s"
ENTRY: Final = "ssc-control-entry"
LB_SCHEME: Final = "EXTERNAL_MANAGED"
TLS_MIN: Final = "TLS_1_2"
TLS_PROFILE: Final = "MODERN"
DNS_TTL: Final = 300
KEYS_CACHE: Final = "public, max-age=300"
RELEASE_SETTINGS: Final = ("control_image", "auth_jwks", "auth_signing_kid")
CELL_SETTINGS: Final = ("cell_label", "cell_jwks")


def public_jwks_kids(jwks: str) -> set[str] | None:
    """The key IDs of a JWKS of named keys with no private member; None for anything else."""
    try:
        doc: object = json.loads(jwks)
    except ValueError:
        return None
    keys = cast(dict[str, object], doc).get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list) or not keys:
        return None
    kids: set[str] = set()
    for key in cast(list[object], keys):
        members = cast(dict[str, object], key) if isinstance(key, dict) else {}
        kid = members.get("kid")
        if not isinstance(kid, str) or PRIVATE_JWK_MEMBERS & set(members):
            return None
        kids.add(kid)
    return kids


@dataclass(frozen=True, slots=True)
class ControlConfig:
    """``control_stages`` run the control plane; ``public_stage`` holds the public hosts.
    ``control_image``, ``auth_jwks`` and ``auth_signing_kid`` are the release, all or none;
    ``cell_label`` and ``cell_jwks`` the one cell, both or none. ``timer_key_id`` names the
    worker's timer key, whose PEM is the ``SSC_TIMER_SIGNING_KEY`` secret (SSC-041)."""

    stages: tuple[n.Stage, ...] = ()
    public: n.Stage | None = None
    image: str | None = None
    auth_jwks: str | None = None
    auth_kid: str | None = None
    cell_label: str | None = None
    cell_jwks: str | None = None
    worker_instances: int = 1
    deployer: bool = False
    timer_key_id: str | None = None

    @property
    def released(self) -> bool:
        return self.image is not None

    def serves_cells(self, stage: n.Stage) -> bool:
        """Whether ``stage``'s control plane is the one the cells trust (``cell.control_for``):
        the public stage, or every stage while there is none."""
        return self.public is None or self.public == stage


def release_settings(
    image: str | None, jwks: str | None, kid: str | None
) -> tuple[str | None, str | None, str | None]:
    """The image pinned in the platform registry, the auth host's public JWKS and the key ID it
    signs with, which that JWKS must hold."""
    if not (image or jwks or kid):
        return None, None, None
    if not (image and jwks and kid):
        raise ValueError(f"set all of {', '.join(RELEASE_SETTINGS)} or none")
    if not PINNED_IMAGE.fullmatch(image):
        raise ValueError(f"control_image must be {n.platform_registry()}/<image>@sha256:<digest>")
    kids = public_jwks_kids(jwks)
    if kids is None:
        raise ValueError("auth_jwks must be the auth host's public JWKS")
    if kid not in kids:
        raise ValueError("auth_jwks must hold the key auth_signing_kid names")
    return image, jwks, kid


def cell_settings(label: str | None, jwks: str | None) -> tuple[str | None, str | None]:
    """The one cell until placement: its label and its public identity JWKS (the cell stack's
    ``identity_jwks`` output)."""
    if not (label or jwks):
        return None, None
    if not (label and jwks):
        raise ValueError(f"set both of {', '.join(CELL_SETTINGS)} or neither")
    check_cell_label(label)
    if public_jwks_kids(jwks) is None:
        raise ValueError("cell_jwks must be the cell's public JWKS (its identity_jwks output)")
    return label, jwks


def read_config(config: pulumi.Config, *, deployer: bool) -> ControlConfig:
    raw = cast(object, config.get_object("control_stages") or [])
    if not isinstance(raw, list):
        raise ValueError(f"control_stages must be a list of {', '.join(n.STAGES)}")
    named = [str(s) for s in cast(list[object], raw)]
    if len(set(named)) != len(named) or not set(named) <= set(n.STAGES):
        raise ValueError(f"control_stages must name each of {', '.join(n.STAGES)} at most once")
    stages: tuple[n.Stage, ...] = tuple(s for s in n.STAGES if s in named)
    public = config.get("public_stage")
    if public is None:
        public = stages[0] if stages else None
    elif public not in stages:
        raise ValueError("public_stage must be one of control_stages")
    image, auth_jwks, auth_kid = release_settings(
        config.get("control_image"), config.get("auth_jwks"), config.get("auth_signing_kid")
    )
    label, cell_jwks = cell_settings(config.get("cell_label"), config.get("cell_jwks"))
    timer_key_id = config.get("timer_key_id") or None
    if timer_key_id is not None and not TIMER_KEY_ID.fullmatch(timer_key_id):
        raise ValueError("timer_key_id must be 1 to 64 letters, digits, '.', '_' or '-'")
    instances = config.get_int("worker_instances")
    if instances is not None and instances < 0:
        raise ValueError("worker_instances must be 0 or more")
    return ControlConfig(
        stages=stages,
        public=public,
        image=image,
        auth_jwks=auth_jwks,
        auth_kid=auth_kid,
        cell_label=label,
        cell_jwks=cell_jwks,
        worker_instances=1 if instances is None else instances,
        deployer=deployer,
        timer_key_id=timer_key_id,
    )


def api_env(
    cfg: ControlConfig, stage: n.Stage, signer: pulumi.Input[str]
) -> dict[str, pulumi.Input[str]]:
    """What ``api.settings.Settings.from_env`` reads, beside its secrets."""
    env: dict[str, pulumi.Input[str]] = {
        "SSC_ENV": stage,
        "SSC_API_ISSUER": n.origin(n.AUTH_HOST),
        "SSC_API_JWKS": cfg.auth_jwks or "",
        "SSC_API_USER_AUDIENCE": n.origin(n.API_HOST),
        "SSC_API_PUBLIC_URL": n.origin(n.API_HOST),
        "SSC_APPS_DOMAIN": n.APPS_DOMAIN,
        **_blob_env(stage, signer),
    }
    if cfg.cell_label and cfg.serves_cells(stage):
        env["SSC_CELL_AGENT_URL"] = n.agent_url(cfg.cell_label)
        env["SSC_SECRET_INTAKE_URL"] = n.intake_url(cfg.cell_label)
    return env


def worker_env(
    cfg: ControlConfig, stage: n.Stage, signer: pulumi.Input[str]
) -> dict[str, pulumi.Input[str]]:
    """What ``worker.compose_ports`` reads, beside its secrets."""
    env: dict[str, pulumi.Input[str]] = {
        "SSC_ENV": stage,
        "SSC_APPS_DOMAIN": n.APPS_DOMAIN,
        "SSC_API_PUBLIC_URL": n.origin(n.API_HOST),
        "SSC_CELL_BUCKET_TEMPLATE": n.CELL_BUCKET_TEMPLATE,
        **_blob_env(stage, signer),
    }
    if cfg.deployer:
        env["SSC_CELL_DEPLOYER"] = "cloud_run"
        env["SSC_CELL_DEPLOYER_JOB"] = n.deployer_job()
    if cfg.cell_label and cfg.cell_jwks and cfg.serves_cells(stage):
        env["SSC_RUNTIME_DRIVER"] = "cell_agent"
        env["SSC_BUILD_DRIVER"] = "cell_agent"
        env["SSC_CELL_AGENT_URL"] = n.agent_url(cfg.cell_label)
        env["SSC_IDENTITY_JWKS"] = cfg.cell_jwks
        env["SSC_IDENTITY_ISSUER"] = n.identity_issuer(cfg.cell_label)
    if cfg.timer_key_id:
        env["SSC_TIMER_DISPATCHER"] = "https"
        env["SSC_TIMER_KEY_ID"] = cfg.timer_key_id
    return env


def auth_env(cfg: ControlConfig, stage: n.Stage) -> dict[str, pulumi.Input[str]]:
    """What ``identity.settings.AuthSettings.from_env`` reads, beside its secrets."""
    return {
        "SSC_ENV": stage,
        "SSC_AUTH_URL": n.origin(n.AUTH_HOST),
        "SSC_API_USER_AUDIENCE": n.origin(n.API_HOST),
        "SSC_APPS_DOMAIN": n.APPS_DOMAIN,
        "SSC_AUTH_SIGNING_KID": cfg.auth_kid or "",
    }


def _blob_env(stage: n.Stage, signer: pulumi.Input[str]) -> dict[str, pulumi.Input[str]]:
    return {
        "SSC_BLOB_BACKEND": "gcs",
        "SSC_BLOB_BUCKET": n.control_bucket(stage, "blobs"),
        "SSC_BLOB_SIGNER": signer,
    }


def secrets_in(cfg: ControlConfig) -> dict[str, tuple[str, ...]]:
    """``SECRET_READERS`` as this config uses it: the timer key only with ``timer_key_id``."""
    return {
        secret: readers
        for secret, readers in SECRET_READERS.items()
        if secret != TIMER_KEY or cfg.timer_key_id
    }


def secrets_of(role: str, cfg: ControlConfig) -> list[str]:
    return [secret for secret, readers in secrets_in(cfg).items() if role in readers]


def _slug(secret: str) -> str:
    return secret.lower().replace("_", "-")


class ControlProject:
    """One ``ssc-control-<stage>`` project and its four accounts. The API and the worker may
    each sign as themselves (bundle URLs, decision 015)."""

    def __init__(
        self,
        stage: n.Stage,
        folder: pulumi.Input[str],
        opts: pulumi.ResourceOptions,
        *,
        billed: bool = True,
    ) -> None:
        self.stage: n.Stage = stage
        self.project = gcp.organizations.Project(
            f"control-{stage}",
            project_id=n.control_project(stage),
            name=n.control_project(stage),
            folder_id=folder,
            billing_account=n.BILLING_ACCOUNT if billed else None,
            auto_create_network=False,
            deletion_policy="PREVENT",
            opts=opts,
        )
        iam_api = gcp.projects.Service(
            f"control-{stage}-iam",
            project=self.project.project_id,
            service="iam.googleapis.com",
            disable_on_destroy=False,
            opts=opts,
        )
        after = pulumi.ResourceOptions.merge(opts, pulumi.ResourceOptions(depends_on=[iam_api]))
        self.accounts = {
            role: gcp.serviceaccount.Account(
                f"control-{stage}-sa" if role == API else f"control-{stage}-{role}-sa",
                project=self.project.project_id,
                account_id=account,
                display_name=f"{DISPLAY[role]} ({stage})",
                opts=after,
            )
            for role, account in ACCOUNTS.items()
        }
        for role in SIGNERS:
            account = self.accounts[role]
            gcp.serviceaccount.IAMMember(
                f"control-{stage}-signs-urls"
                if role == API
                else f"control-{stage}-{role}-signs-urls",
                service_account_id=account.name,
                role="roles/iam.serviceAccountTokenCreator",
                member=account.member,
                opts=opts,
            )

    @property
    def api(self) -> gcp.serviceaccount.Account:
        return self.accounts[API]

    @property
    def worker(self) -> gcp.serviceaccount.Account:
        return self.accounts[WORKER]


class ControlPlane:
    """The running control plane in one control project. The services are reached only through
    the public stage's load balancer (internal and load-balancer ingress); the worker pool takes
    no requests."""

    def __init__(
        self,
        cp: ControlProject,
        cfg: ControlConfig,
        opts: pulumi.ResourceOptions,
        *,
        enabled: Sequence[str] = (),
    ) -> None:
        self.cp = cp
        self.cfg = cfg
        self.stage: n.Stage = cp.stage
        self.pid = cp.project.project_id
        self.public = cfg.public == cp.stage
        self.opts = opts
        self.apis = [
            gcp.projects.Service(
                f"control-{self.stage}-{api.split('.')[0]}",
                project=self.pid,
                service=api,
                disable_on_destroy=False,
                opts=opts,
            )
            for api in APIS
            if api not in enabled
        ]

    def _o(self, *depends_on: pulumi.Resource) -> pulumi.ResourceOptions:
        return pulumi.ResourceOptions.merge(
            self.opts, pulumi.ResourceOptions(depends_on=[*self.apis, *depends_on])
        )

    def _name(self, part: str) -> str:
        return f"control-{self.stage}-{part}"

    def build(self) -> None:
        self.registry()
        self.database()
        self.secrets()
        self.blobs()
        self.services()
        self.migrate_job()
        if self.public:
            self.entry()

    def registry(self) -> None:
        """Cloud Run pulls the control image from the platform registry as this project's
        service agent."""
        run_agent = gcp.projects.ServiceIdentity(
            self._name("run-agent"), project=self.pid, service="run.googleapis.com", opts=self._o()
        )
        gcp.artifactregistry.RepositoryIamMember(
            self._name("registry"),
            project=n.BOOTSTRAP_PROJECT,
            location=n.REGION,
            repository=n.PLATFORM_REPOSITORY,
            role="roles/artifactregistry.reader",
            member=run_agent.member,
            opts=self._o(),
        )

    def database(self) -> None:
        """The control database: reached only through the Cloud SQL connector (no authorised
        network, client certificates required), as ``ssc_migrate`` by the migration job and
        ``ssc_app`` by everything else. The operator makes both roles (the runbook), so no
        password is in this program's state."""
        prod = self.stage == "prod"
        self.sql = gcp.sql.DatabaseInstance(
            self._name("sql"),
            project=self.pid,
            name=SQL_INSTANCE,
            region=n.REGION,
            database_version="POSTGRES_18",
            deletion_protection=True,
            settings=gcp.sql.DatabaseInstanceSettingsArgs(
                tier=SQL_TIER,
                edition="ENTERPRISE",
                availability_type="ZONAL",
                deletion_protection_enabled=True,
                connector_enforcement="REQUIRED",
                ip_configuration=gcp.sql.DatabaseInstanceSettingsIpConfigurationArgs(
                    ipv4_enabled=True,
                    ssl_mode="TRUSTED_CLIENT_CERTIFICATE_REQUIRED",
                ),
                backup_configuration=gcp.sql.DatabaseInstanceSettingsBackupConfigurationArgs(
                    enabled=True,
                    point_in_time_recovery_enabled=prod,
                    start_time="08:00",
                    transaction_log_retention_days=7 if prod else None,
                ),
                database_flags=[
                    gcp.sql.DatabaseInstanceSettingsDatabaseFlagArgs(
                        name="max_connections", value=SQL_MAX_CONNECTIONS
                    )
                ],
                user_labels={"ssc-control": self.stage},
            ),
            opts=self._o(),
        )
        gcp.sql.Database(
            self._name("sql-db"),
            project=self.pid,
            instance=self.sql.name,
            name=SQL_DATABASE,
            opts=self._o(),
        )
        for role in (API, WORKER, AUTH, MIGRATE):
            gcp.projects.IAMMember(
                self._name(f"{role}-sql-client"),
                project=self.pid,
                role="roles/cloudsql.client",
                member=self.cp.accounts[role].member,
                opts=self._o(),
            )

    def secrets(self) -> None:
        """One secret per setting, in the region, readable only by the accounts in
        ``SECRET_READERS``: a grant on the secret, never on the project."""
        self.secret_grants: dict[str, list[pulumi.Resource]] = {role: [] for role in ACCOUNTS}
        for secret_id, readers in secrets_in(self.cfg).items():
            secret = gcp.secretmanager.Secret(
                self._name(_slug(secret_id)),
                project=self.pid,
                secret_id=secret_id,
                replication=gcp.secretmanager.SecretReplicationArgs(
                    user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                        replicas=[
                            gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(
                                location=n.REGION
                            )
                        ]
                    )
                ),
                opts=self._o(),
            )
            for role in readers:
                self.secret_grants[role].append(
                    gcp.secretmanager.SecretIamMember(
                        self._name(f"{_slug(secret_id)}-{role}"),
                        project=self.pid,
                        secret_id=secret.secret_id,
                        role=SECRET_ACCESSOR,
                        member=self.cp.accounts[role].member,
                        opts=self._o(),
                    )
                )

    def blobs(self) -> None:
        """Bundles and build inputs (decision 015): private, signed URLs only."""
        bucket = gcp.storage.Bucket(
            self._name("blobs"),
            project=self.pid,
            name=n.control_bucket(self.stage, "blobs"),
            location=n.REGION.upper(),
            uniform_bucket_level_access=True,
            public_access_prevention="enforced",
            opts=self._o(),
        )
        for role in SIGNERS:
            gcp.storage.BucketIAMMember(
                self._name(f"blobs-{role}"),
                bucket=bucket.name,
                role="roles/storage.objectUser",
                member=self.cp.accounts[role].member,
                opts=self._o(),
            )

    def services(self) -> None:
        cfg, stage = self.cfg, self.stage
        self.api_ = self._service(
            API_SERVICE,
            API,
            command=API_COMMAND,
            env=api_env(cfg, stage, self.cp.api.email),
            instances=(API_MIN[stage], API_MAX),
        )
        self.auth_ = self._service(
            AUTH_SERVICE,
            AUTH,
            command=AUTH_COMMAND,
            env=auth_env(cfg, stage),
            instances=(0, AUTH_MAX),
        )
        self.worker_pool()

    def _service(
        self,
        name: str,
        role: str,
        *,
        command: Sequence[str],
        env: dict[str, pulumi.Input[str]],
        instances: tuple[int, int],
    ) -> gcp.cloudrunv2.Service:
        """A request-billed service: CPU only while a request is open."""
        released = self.cfg.released
        low, high = instances
        service = gcp.cloudrunv2.Service(
            self._name(name),
            project=self.pid,
            name=name,
            location=n.REGION,
            ingress="INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
            deletion_protection=self.stage == "prod",
            scaling=gcp.cloudrunv2.ServiceScalingArgs(max_instance_count=high),
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=self.cp.accounts[role].email,
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(
                    min_instance_count=low if released else 0
                ),
                volumes=[
                    gcp.cloudrunv2.ServiceTemplateVolumeArgs(
                        name=SQL_VOLUME,
                        cloud_sql_instance=gcp.cloudrunv2.ServiceTemplateVolumeCloudSqlInstanceArgs(
                            instances=[self.sql.connection_name]
                        ),
                    )
                ]
                if released
                else None,
                containers=[
                    gcp.cloudrunv2.ServiceTemplateContainerArgs(
                        image=self.cfg.image or PLACEHOLDER_IMAGE,
                        commands=list(command) if released else None,
                        envs=[
                            *(
                                gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=k, value=v)
                                for k, v in sorted(env.items())
                            ),
                            *(
                                gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                                    name=s,
                                    value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
                                        secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                                            secret=s, version="latest"
                                        )
                                    ),
                                )
                                for s in secrets_of(role, self.cfg)
                            ),
                        ]
                        if released
                        else None,
                        volume_mounts=[
                            gcp.cloudrunv2.ServiceTemplateContainerVolumeMountArgs(
                                name=SQL_VOLUME, mount_path=SQL_MOUNT
                            )
                        ]
                        if released
                        else None,
                        resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                            cpu_idle=True, limits=LIMITS
                        ),
                    )
                ],
            ),
            opts=self._o(*self.secret_grants[role]),
        )
        if self.public:
            gcp.cloudrunv2.ServiceIamMember(
                self._name(f"{name}-invoker"),
                project=self.pid,
                location=n.REGION,
                name=service.name,
                role="roles/run.invoker",
                member="allUsers",
                opts=self._o(),
            )
        return service

    def worker_pool(self) -> None:
        """The worker polls the database for jobs, so it runs always-on in a worker pool:
        ``worker_instances`` instances (1 by default), none until the release is set."""
        released = self.cfg.released
        self.worker_ = gcp.cloudrunv2.WorkerPool(
            self._name(WORKER_POOL),
            project=self.pid,
            name=WORKER_POOL,
            location=n.REGION,
            launch_stage="BETA",
            deletion_protection=self.stage == "prod",
            scaling=gcp.cloudrunv2.WorkerPoolScalingArgs(
                scaling_mode="MANUAL",
                manual_instance_count=self.cfg.worker_instances if released else 0,
            ),
            template=gcp.cloudrunv2.WorkerPoolTemplateArgs(
                service_account=self.cp.worker.email,
                volumes=[
                    gcp.cloudrunv2.WorkerPoolTemplateVolumeArgs(
                        name=SQL_VOLUME,
                        cloud_sql_instance=gcp.cloudrunv2.WorkerPoolTemplateVolumeCloudSqlInstanceArgs(
                            instances=[self.sql.connection_name]
                        ),
                    )
                ]
                if released
                else None,
                containers=[
                    gcp.cloudrunv2.WorkerPoolTemplateContainerArgs(
                        image=self.cfg.image or PLACEHOLDER_IMAGE,
                        commands=list(WORKER_COMMAND) if released else None,
                        envs=[
                            *(
                                gcp.cloudrunv2.WorkerPoolTemplateContainerEnvArgs(name=k, value=v)
                                for k, v in sorted(
                                    worker_env(self.cfg, self.stage, self.cp.worker.email).items()
                                )
                            ),
                            *(
                                gcp.cloudrunv2.WorkerPoolTemplateContainerEnvArgs(
                                    name=s,
                                    value_source=gcp.cloudrunv2.WorkerPoolTemplateContainerEnvValueSourceArgs(
                                        secret_key_ref=gcp.cloudrunv2.WorkerPoolTemplateContainerEnvValueSourceSecretKeyRefArgs(
                                            secret=s, version="latest"
                                        )
                                    ),
                                )
                                for s in secrets_of(WORKER, self.cfg)
                            ),
                        ]
                        if released
                        else None,
                        volume_mounts=[
                            gcp.cloudrunv2.WorkerPoolTemplateContainerVolumeMountArgs(
                                name=SQL_VOLUME, mount_path=SQL_MOUNT
                            )
                        ]
                        if released
                        else None,
                        resources=gcp.cloudrunv2.WorkerPoolTemplateContainerResourcesArgs(
                            limits=LIMITS
                        ),
                    )
                ],
            ),
            opts=self._o(*self.secret_grants[WORKER]),
        )

    def migrate_job(self) -> None:
        """``ssc_control.db.migrate.upgrade`` as ``ssc_migrate``, run by the operator before each
        release (the runbook). Only once the release is set."""
        image = self.cfg.image
        if image is None:
            return
        gcp.cloudrunv2.Job(
            self._name(MIGRATE_JOB),
            project=self.pid,
            name=MIGRATE_JOB,
            location=n.REGION,
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                task_count=1,
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=self.cp.accounts[MIGRATE].email,
                    max_retries=0,
                    timeout=MIGRATE_TIMEOUT,
                    volumes=[
                        gcp.cloudrunv2.JobTemplateTemplateVolumeArgs(
                            name=SQL_VOLUME,
                            cloud_sql_instance=gcp.cloudrunv2.JobTemplateTemplateVolumeCloudSqlInstanceArgs(
                                instances=[self.sql.connection_name]
                            ),
                        )
                    ],
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            image=image,
                            commands=list(MIGRATE_COMMAND),
                            envs=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name=s,
                                    value_source=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceArgs(
                                        secret_key_ref=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceSecretKeyRefArgs(
                                            secret=s, version="latest"
                                        )
                                    ),
                                )
                                for s in secrets_of(MIGRATE, self.cfg)
                            ],
                            volume_mounts=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerVolumeMountArgs(
                                    name=SQL_VOLUME, mount_path=SQL_MOUNT
                                )
                            ],
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits=LIMITS
                            ),
                        )
                    ],
                ),
            ),
            opts=self._o(*self.secret_grants[MIGRATE]),
        )

    def entry(self) -> None:
        """The public door, as a cell's (SSC-088): one global external Application Load
        Balancer on one address with TLS 1.2 or later and HTTP redirected. ``api`` goes to the
        API, ``auth`` to the auth host and ``keys`` to a public bucket holding
        ``<cell label>/jwks.json``. One Google-managed certificate names the three hosts; it
        issues once their A records, written here into the ``delimitus`` zone, resolve."""
        self.entry_ip = gcp.compute.GlobalAddress(
            self._name("entry-ip"),
            project=self.pid,
            name=ENTRY,
            address_type="EXTERNAL",
            ip_version="IPV4",
            opts=self._o(),
        )
        api = self._backend("api", API_SERVICE, self.api_)
        auth = self._backend("auth", AUTH_SERVICE, self.auth_)
        keys = self._keys()
        url_map = gcp.compute.URLMap(
            self._name("entry-map"),
            project=self.pid,
            name=ENTRY,
            default_service=api.id,
            host_rules=[
                gcp.compute.URLMapHostRuleArgs(hosts=[host], path_matcher=matcher)
                for host, matcher in ((n.API_HOST, API), (n.AUTH_HOST, AUTH), (n.KEYS_HOST, "keys"))
            ],
            path_matchers=[
                gcp.compute.URLMapPathMatcherArgs(name=API, default_service=api.id),
                gcp.compute.URLMapPathMatcherArgs(name=AUTH, default_service=auth.id),
                gcp.compute.URLMapPathMatcherArgs(name="keys", default_service=keys.id),
            ],
            opts=self._o(),
        )
        certificate = gcp.compute.ManagedSslCertificate(
            self._name("cert"),
            project=self.pid,
            name=ENTRY,
            managed=gcp.compute.ManagedSslCertificateManagedArgs(domains=list(n.CONTROL_HOSTS)),
            opts=self._o(),
        )
        tls = gcp.compute.SSLPolicy(
            self._name("entry-tls"),
            project=self.pid,
            name=ENTRY,
            min_tls_version=TLS_MIN,
            profile=TLS_PROFILE,
            opts=self._o(),
        )
        https = gcp.compute.TargetHttpsProxy(
            self._name("entry-https"),
            project=self.pid,
            name=ENTRY,
            url_map=url_map.id,
            ssl_policy=tls.id,
            ssl_certificates=[certificate.id],
            opts=self._o(),
        )
        redirect = gcp.compute.URLMap(
            self._name("entry-redirect"),
            project=self.pid,
            name=f"{ENTRY}-redirect",
            default_url_redirect=gcp.compute.URLMapDefaultUrlRedirectArgs(
                https_redirect=True,
                strip_query=False,
                redirect_response_code="MOVED_PERMANENTLY_DEFAULT",
            ),
            opts=self._o(),
        )
        http = gcp.compute.TargetHttpProxy(
            self._name("entry-http"),
            project=self.pid,
            name=f"{ENTRY}-redirect",
            url_map=redirect.id,
            opts=self._o(),
        )
        for scheme, target, port in (("https", https.id, "443"), ("http", http.id, "80")):
            gcp.compute.GlobalForwardingRule(
                self._name(f"entry-{scheme}"),
                project=self.pid,
                name=f"{ENTRY}-{scheme}",
                target=target,
                ip_address=self.entry_ip.address,
                ip_protocol="TCP",
                port_range=port,
                load_balancing_scheme=LB_SCHEME,
                opts=self._o(),
            )
        for host in n.CONTROL_HOSTS:
            gcp.dns.RecordSet(
                self._name(f"dns-{host.split('.')[0]}"),
                project=n.BOOTSTRAP_PROJECT,
                managed_zone=n.PLATFORM_ZONE,
                name=f"{host}.",
                type="A",
                ttl=DNS_TTL,
                rrdatas=[self.entry_ip.address],
                opts=self._o(),
            )

    def _backend(
        self, resource: str, name: str, service: gcp.cloudrunv2.Service
    ) -> gcp.compute.BackendService:
        """A serverless NEG on one Cloud Run service and the backend service in front of it."""
        neg = gcp.compute.RegionNetworkEndpointGroup(
            self._name(f"{resource}-neg"),
            project=self.pid,
            name=name,
            region=n.REGION,
            network_endpoint_type="SERVERLESS",
            cloud_run=gcp.compute.RegionNetworkEndpointGroupCloudRunArgs(service=service.name),
            opts=self._o(),
        )
        return gcp.compute.BackendService(
            self._name(f"{resource}-backend"),
            project=self.pid,
            name=name,
            load_balancing_scheme=LB_SCHEME,
            protocol="HTTPS",
            backends=[gcp.compute.BackendServiceBackendArgs(group=neg.id)],
            opts=self._o(),
        )

    def _keys(self) -> gcp.compute.BackendBucket:
        """``keys.delimitus.com/<cell label>/jwks.json``: each cell's public JWKS as an object in
        a public bucket. Only public keys ever go in it (``cell_settings`` refuses others)."""
        bucket = gcp.storage.Bucket(
            self._name("keys"),
            project=self.pid,
            name=n.control_bucket(self.stage, "keys"),
            location=n.REGION.upper(),
            uniform_bucket_level_access=True,
            public_access_prevention="inherited",
            opts=self._o(),
        )
        gcp.storage.BucketIAMMember(
            self._name("keys-public"),
            bucket=bucket.name,
            role="roles/storage.objectViewer",
            member="allUsers",
            opts=self._o(),
        )
        label, jwks = self.cfg.cell_label, self.cfg.cell_jwks
        if label and jwks:
            gcp.storage.BucketObject(
                self._name(f"keys-{label}"),
                bucket=bucket.name,
                name=f"{label}/jwks.json",
                content=jwks,
                content_type="application/json",
                cache_control=KEYS_CACHE,
                opts=self._o(),
            )
        return gcp.compute.BackendBucket(
            self._name("keys-backend"),
            project=self.pid,
            name="ssc-keys",
            bucket_name=bucket.name,
            opts=self._o(),
        )

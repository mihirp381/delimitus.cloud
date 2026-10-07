"""The control plane in the platform program (SSC-064), run against mocks.

Ticket "done when" checks:
  * API, worker and auth host per control project, each its own account
        -> test_each_process_runs_as_its_own_account
  * prod's project only once configured -> test_prod_is_made_only_once_configured
  * images digest-pinned, all or none, placeholder otherwise
        -> test_the_release_is_all_or_none, test_the_placeholder_runs_until_the_release_is_set
  * API request-billed, min 0 in staging and 1 in prod -> test_the_api_is_request_billed
  * how the worker runs -> test_the_worker_runs_always_on_in_a_worker_pool
  * the control database and its migrations -> test_the_database_is_reached_only_by_connector,
        test_the_migration_job_runs_as_the_migrator
  * no service account can read a secret it does not use
        -> test_no_account_can_read_a_secret_it_does_not_use
  * the worker's timer key only once named (SSC-041)
        -> test_without_a_timer_key_id_the_worker_keeps_its_dispatcher,
        test_a_timer_key_id_gives_the_worker_its_timer_key_alone
  * every setting of the three processes -> test_every_setting_of_the_api_is_wired,
        test_every_setting_of_the_worker_is_wired, test_every_setting_of_the_auth_host_is_wired
  * api, auth and keys hosts with certificate and records
        -> test_the_public_hosts_are_on_one_load_balancer, test_keys_serves_each_cell_jwks
  * every cell the control plane serves, as SSC_CELLS (decision 030)
        -> test_the_cells_are_one_compact_setting, test_the_legacy_cell_is_a_one_cell_list,
        test_the_cells_are_validated, test_the_control_plane_names_cell_hosts_as_the_cells_do
"""

import json
from typing import Any

import pulumi
import pytest

from mockcloud import Declared, entry_address, one, run
from ssc_infra import control, naming
from ssc_shared import hosts

PLATFORM_FOLDER = "333333333333"
IMAGE = f"{naming.platform_registry()}/control@sha256:{'1' * 64}"
DEPLOYER_IMAGE = f"{naming.platform_registry()}/deployer@sha256:{'0' * 64}"
KID = "auth-2026-10"
AUTH_JWKS = json.dumps(
    {"keys": [{"kty": "EC", "crv": "P-256", "x": "AA", "y": "BB", "kid": KID, "alg": "ES256"}]}
)
LABEL = "testcell09"
CELL_JWKS = json.dumps(
    {"keys": [{"kty": "EC", "crv": "P-256", "x": "CC", "y": "DD", "kid": "gw-1", "alg": "ES256"}]}
)
LABEL2 = "othercell7"
CELL2_JWKS = json.dumps(
    {"keys": [{"kty": "EC", "crv": "P-256", "x": "EE", "y": "FF", "kid": "gw-2"}]}
)
CELLS = [{"label": LABEL, "jwks": CELL_JWKS}, {"label": LABEL2, "jwks": CELL2_JWKS}]
SSC_CELLS = (
    '{"othercell7":{"identity_jwks":{"keys":[{"crv":"P-256","kid":"gw-2","kty":"EC","x":"EE",'
    '"y":"FF"}]}},"testcell09":{"identity_jwks":{"keys":[{"alg":"ES256","crv":"P-256",'
    '"kid":"gw-1","kty":"EC","x":"CC","y":"DD"}]}}}'
)
RELEASE = {
    "platform_folder_id": PLATFORM_FOLDER,
    "control_stages": json.dumps(["staging", "prod"]),
    "control_image": IMAGE,
    "auth_jwks": AUTH_JWKS,
    "auth_signing_kid": KID,
    "cells": json.dumps(CELLS),
    "deployer_image": DEPLOYER_IMAGE,
}
WORKER_CELL = {
    "SSC_RUNTIME_DRIVER": "cell_agent",
    "SSC_BUILD_DRIVER": "cell_agent",
    "SSC_CELLS": SSC_CELLS,
}
API_CELL = {"SSC_CELLS": SSC_CELLS}
RETIRED = (
    "SSC_CELL_AGENT_URL",
    "SSC_SECRET_INTAKE_URL",
    "SSC_IDENTITY_JWKS",
    "SSC_IDENTITY_ISSUER",
)
"""The one cell's settings before ``SSC_CELLS`` (decision 030), set on no process now."""
TIMER_KID = "timer-202610"
SERVICE = "gcp:cloudrunv2/service:Service"
POOL = "gcp:cloudrunv2/workerPool:WorkerPool"
JOB = "gcp:cloudrunv2/job:Job"
SECRET_GRANT = "gcp:secretmanager/secretIamMember:SecretIamMember"


def email(account: str, stage: naming.Stage) -> str:
    return naming.sa_email(account, naming.control_project(stage))


@pytest.fixture(scope="module")
def released() -> list[Declared]:
    return run(naming.PLATFORM_STACK, RELEASE)


@pytest.fixture(scope="module")
def timed() -> list[Declared]:
    return run(naming.PLATFORM_STACK, RELEASE | {"timer_key_id": TIMER_KID})


@pytest.fixture(scope="module")
def bare() -> list[Declared]:
    return run(
        naming.PLATFORM_STACK,
        {"platform_folder_id": PLATFORM_FOLDER, "control_stages": json.dumps(["staging"])},
    )


def _of(declared: list[Declared], type_: str, stage: naming.Stage) -> list[Declared]:
    return [
        d
        for d in declared
        if d.type == type_ and d.inputs.get("project") == naming.control_project(stage)
    ]


def _workload(declared: list[Declared], type_: str, stage: naming.Stage, name: str) -> Declared:
    (found,) = [d for d in _of(declared, type_, stage) if d.inputs["name"] == name]
    return found


def _template(d: Declared) -> dict[str, Any]:
    return d.inputs["template"]["template"] if d.type == JOB else d.inputs["template"]


def _env(d: Declared) -> tuple[dict[str, str], set[str]]:
    """A workload's plain settings and the secrets it takes."""
    (container,) = _template(d)["containers"]
    plain = {e["name"]: e["value"] for e in container.get("envs", []) if "value" in e}
    secrets: set[str] = set()
    for e in container.get("envs", []):
        if "valueSource" in e:
            ref = e["valueSource"]["secretKeyRef"]
            assert ref["secret"] == e["name"]
            assert ref["version"] == "latest"
            secrets.add(ref["secret"])
    return plain, secrets


def test_no_control_plane_until_a_stage_is_named() -> None:
    """Default config: staging's project and accounts only, as before SSC-064."""
    declared = run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER})
    for type_ in (SERVICE, POOL, JOB, "gcp:sql/databaseInstance:DatabaseInstance"):
        assert not [d for d in declared if d.type == type_]
    accounts = {
        d.outputs["email"] for d in declared if d.type == "gcp:serviceaccount/account:Account"
    }
    assert {email(a, "staging") for a in control.ACCOUNTS.values()} <= accounts
    assert not [a for a in accounts if naming.control_project("prod") in a]


def test_prod_is_made_only_once_configured(released: list[Declared]) -> None:
    projects = {
        d.inputs["projectId"]: d.inputs
        for d in released
        if d.type == "gcp:organizations/project:Project"
    }
    assert sorted(projects) == ["ssc-control-prod", "ssc-control-staging"]
    for project in projects.values():
        assert project["folderId"] == PLATFORM_FOLDER
        assert project["deletionPolicy"] == "PREVENT"
        assert project["billingAccount"] == naming.BILLING_ACCOUNT
    staging = one(released, "gcp:organizations/project:Project", "control-staging")
    assert staging.inputs["projectId"] == "ssc-control-staging"


def test_each_process_runs_as_its_own_account(released: list[Declared]) -> None:
    for stage in naming.STAGES:
        runs_as = {
            d.inputs["name"]: _template(d)["serviceAccount"]
            for type_ in (SERVICE, POOL, JOB)
            for d in _of(released, type_, stage)
        }
        assert runs_as == {
            "ssc-api": email(naming.CONTROL_SA, stage),
            "ssc-worker": email(naming.CONTROL_WORKER_SA, stage),
            "ssc-auth": email(naming.AUTH_SA, stage),
            "ssc-control-migrate": email(naming.MIGRATE_SA, stage),
        }
        assert len(set(runs_as.values())) == len(runs_as)


@pytest.mark.parametrize("stack", ["released", "timed"])
def test_no_account_can_read_a_secret_it_does_not_use(
    stack: str, request: pytest.FixtureRequest
) -> None:
    """Use is what the declared containers take by ``secretKeyRef``. Every grant is the
    accessor role on one secret to an account whose workload takes it, every secret a
    workload takes is granted to its account, and no account holds a secret role on a
    project. Also with the worker's timer key (``timed``)."""
    released: list[Declared] = request.getfixturevalue(stack)
    for stage in naming.STAGES:
        uses: set[tuple[str, str]] = set()
        for type_ in (SERVICE, POOL, JOB):
            for d in _of(released, type_, stage):
                member = f"serviceAccount:{_template(d)['serviceAccount']}"
                uses |= {(member, secret) for secret in _env(d)[1]}
        grants = {
            (g.inputs["member"], g.inputs["secretId"]) for g in _of(released, SECRET_GRANT, stage)
        }
        assert grants == uses
        assert {g.inputs["role"] for g in _of(released, SECRET_GRANT, stage)} == {
            "roles/secretmanager.secretAccessor"
        }
        secrets = {
            d.inputs["secretId"]: d.inputs
            for d in _of(released, "gcp:secretmanager/secret:Secret", stage)
        }
        assert {s for _, s in uses} == set(secrets)
        assert {
            "SSC_WORKOS_API_KEY",
            "SSC_WORKOS_CLIENT_ID",
            "SSC_AUTH_SIGNING_KEY",
            "SSC_AUTH_STATE_KEY",
        } <= set(secrets)
        for secret in secrets.values():
            assert secret["replication"] == {
                "userManaged": {"replicas": [{"location": naming.REGION}]}
            }
    assert not [
        d
        for d in released
        if d.type.endswith(("iAMMember:IAMMember", "IAMBinding", "IAMPolicy"))
        and "secretmanager" in str(d.inputs.get("role"))
    ]
    assert not [d for d in released if d.type == "gcp:secretmanager/secretVersion:SecretVersion"]


def test_the_ticket_secrets_go_only_where_they_are_read(released: list[Declared]) -> None:
    readers: dict[str, set[str]] = {}
    for g in _of(released, SECRET_GRANT, "prod"):
        readers.setdefault(g.inputs["secretId"], set()).add(
            g.inputs["member"].removeprefix("serviceAccount:").split("@")[0]
        )
    assert readers == {
        "SSC_DATABASE_DSN": {"ssc-control", "ssc-control-worker", "ssc-auth"},
        "SSC_MIGRATE_DSN": {"ssc-control-migrate"},
        "SSC_METRICS_KEY": {"ssc-control", "ssc-control-worker"},
        "SSC_WORKOS_API_KEY": {"ssc-control-worker", "ssc-auth"},
        "SSC_WORKOS_CLIENT_ID": {"ssc-control-worker", "ssc-auth"},
        "SSC_AUTH_SIGNING_KEY": {"ssc-auth"},
        "SSC_AUTH_STATE_KEY": {"ssc-auth"},
    }


def test_every_setting_of_the_api_is_wired(released: list[Declared]) -> None:
    """The cell's settings go to the public stage alone, ``prod`` here (test below)."""
    for stage in naming.STAGES:
        plain, secrets = _env(_workload(released, SERVICE, stage, "ssc-api"))
        cell = API_CELL if stage == "prod" else {}
        assert plain == {
            "SSC_ENV": stage,
            "SSC_API_ISSUER": "https://auth.delimitus.com",
            "SSC_API_JWKS": AUTH_JWKS,
            "SSC_API_USER_AUDIENCE": "https://api.delimitus.com",
            "SSC_API_PUBLIC_URL": "https://api.delimitus.com",
            "SSC_APPS_DOMAIN": "delimitusapps.com",
            "SSC_BLOB_BACKEND": "gcs",
            "SSC_BLOB_BUCKET": f"ssc-control-{stage}-blobs",
            "SSC_BLOB_SIGNER": email(naming.CONTROL_SA, stage),
            **cell,
        }
        assert secrets == {"SSC_DATABASE_DSN", "SSC_METRICS_KEY"}


def test_every_setting_of_the_worker_is_wired(released: list[Declared]) -> None:
    """The cell's settings go to the public stage alone, ``prod`` here (test below)."""
    for stage in naming.STAGES:
        plain, secrets = _env(_workload(released, POOL, stage, "ssc-worker"))
        assert plain == {
            "SSC_ENV": stage,
            "SSC_APPS_DOMAIN": "delimitusapps.com",
            "SSC_API_PUBLIC_URL": "https://api.delimitus.com",
            "SSC_BLOB_BACKEND": "gcs",
            "SSC_BLOB_BUCKET": f"ssc-control-{stage}-blobs",
            "SSC_BLOB_SIGNER": email(naming.CONTROL_WORKER_SA, stage),
            "SSC_CELL_BUCKET_TEMPLATE": "ssc-c-{cell}-cell",
            "SSC_CELL_DEPLOYER": "cloud_run",
            "SSC_CELL_DEPLOYER_JOB": naming.deployer_job(),
            **(WORKER_CELL if stage == "prod" else {}),
        }
        assert secrets == {
            "SSC_DATABASE_DSN",
            "SSC_METRICS_KEY",
            "SSC_WORKOS_API_KEY",
            "SSC_WORKOS_CLIENT_ID",
        }
    assert naming.CELL_BUCKET_TEMPLATE.format(cell=LABEL) == naming.cell_bucket(LABEL)


def test_every_setting_of_the_auth_host_is_wired(released: list[Declared]) -> None:
    """The gateway's ``/internal/redeem`` token names ``SSC_AUTH_URL`` as its audience."""
    for stage in naming.STAGES:
        plain, secrets = _env(_workload(released, SERVICE, stage, "ssc-auth"))
        assert plain == {
            "SSC_ENV": stage,
            "SSC_AUTH_URL": "https://auth.delimitus.com",
            "SSC_API_USER_AUDIENCE": "https://api.delimitus.com",
            "SSC_APPS_DOMAIN": "delimitusapps.com",
            "SSC_AUTH_SIGNING_KID": KID,
        }
        assert secrets == {
            "SSC_DATABASE_DSN",
            "SSC_WORKOS_API_KEY",
            "SSC_WORKOS_CLIENT_ID",
            "SSC_AUTH_SIGNING_KEY",
            "SSC_AUTH_STATE_KEY",
        }


@pytest.mark.parametrize(("public", "other"), [("staging", "prod"), ("prod", "staging")])
def test_only_the_stage_the_cells_trust_is_given_the_cell(
    public: naming.Stage, other: naming.Stage
) -> None:
    """A cell grants its agent and bucket to ``control_public_stage``'s accounts alone
    (``cell.control_for``), so another stage's API and worker would only be refused."""
    declared = run(naming.PLATFORM_STACK, RELEASE | {"public_stage": public})
    api, _ = _env(_workload(declared, SERVICE, public, "ssc-api"))
    worker, _ = _env(_workload(declared, POOL, public, "ssc-worker"))
    assert API_CELL.items() <= api.items()
    assert WORKER_CELL.items() <= worker.items()
    api, _ = _env(_workload(declared, SERVICE, other, "ssc-api"))
    worker, _ = _env(_workload(declared, POOL, other, "ssc-worker"))
    assert not set(API_CELL) & set(api)
    assert not set(WORKER_CELL) & set(worker)
    assert worker["SSC_CELL_DEPLOYER"] == "cloud_run"


def test_without_a_timer_key_id_the_worker_keeps_its_dispatcher(
    released: list[Declared],
) -> None:
    """Nothing waits for a secret version: no timer key secret, grant or setting."""
    for stage in naming.STAGES:
        plain, secrets = _env(_workload(released, POOL, stage, "ssc-worker"))
        assert not {"SSC_TIMER_DISPATCHER", "SSC_TIMER_KEY_ID"} & set(plain)
        assert control.TIMER_KEY not in secrets
        made = {
            d.inputs["secretId"] for d in _of(released, "gcp:secretmanager/secret:Secret", stage)
        }
        assert control.TIMER_KEY not in made


def test_a_timer_key_id_gives_the_worker_its_timer_key_alone(timed: list[Declared]) -> None:
    """The worker sends timer calls through each app's public host, signed with the key in
    ``SSC_TIMER_SIGNING_KEY`` (``worker.timer_dispatcher_from_env``); no other account reads
    it."""
    for stage in naming.STAGES:
        plain, secrets = _env(_workload(timed, POOL, stage, "ssc-worker"))
        assert {
            "SSC_TIMER_DISPATCHER": "https",
            "SSC_TIMER_KEY_ID": TIMER_KID,
            "SSC_APPS_DOMAIN": "delimitusapps.com",
        }.items() <= plain.items()
        assert control.TIMER_KEY in secrets
        readers = {
            g.inputs["member"]
            for g in _of(timed, SECRET_GRANT, stage)
            if g.inputs["secretId"] == control.TIMER_KEY
        }
        assert readers == {f"serviceAccount:{email(naming.CONTROL_WORKER_SA, stage)}"}
        for name in ("ssc-api", "ssc-auth"):
            assert control.TIMER_KEY not in _env(_workload(timed, SERVICE, stage, name))[1]


@pytest.mark.parametrize("kid", ["a b", "-lead", "x" * 65, "kid/1"])
def test_the_timer_key_id_is_a_plain_name(kid: str) -> None:
    with pytest.raises(Exception, match="timer_key_id"):
        run(naming.PLATFORM_STACK, RELEASE | {"timer_key_id": kid})


def test_with_no_public_stage_each_stage_is_given_the_cell() -> None:
    cells = control.cell_settings(CELLS, None, None)
    cfg = control.ControlConfig(stages=naming.STAGES, cells=cells, public=None)
    for stage in naming.STAGES:
        assert API_CELL.items() <= control.api_env(cfg, stage, "signer").items()
        assert WORKER_CELL.items() <= control.worker_env(cfg, stage, "signer").items()


def test_without_a_cell_or_the_deployer_the_worker_runs_none_of_them() -> None:
    config = {k: v for k, v in RELEASE.items() if k != control.CELLS}
    del config["deployer_image"]
    declared = run(naming.PLATFORM_STACK, config)
    plain, _ = _env(_workload(declared, POOL, "prod", "ssc-worker"))
    for unset in ("SSC_CELL_DEPLOYER", "SSC_RUNTIME_DRIVER", "SSC_BUILD_DRIVER", "SSC_CELLS"):
        assert unset not in plain
    api, _ = _env(_workload(declared, SERVICE, "prod", "ssc-api"))
    assert "SSC_CELLS" not in api


def test_the_cells_are_one_compact_setting(released: list[Declared]) -> None:
    """Each cell's label and identity JWKS, and nothing the label alone names: the control
    plane derives the agent, intake and issuer from it (``ssc_shared.hosts``)."""
    for name, type_ in (("ssc-api", SERVICE), ("ssc-worker", POOL)):
        plain, _ = _env(_workload(released, type_, "prod", name))
        assert plain["SSC_CELLS"] == SSC_CELLS
        assert json.loads(SSC_CELLS) == {
            LABEL: {"identity_jwks": json.loads(CELL_JWKS)},
            LABEL2: {"identity_jwks": json.loads(CELL2_JWKS)},
        }
        assert not set(RETIRED) & set(plain)
    reordered = run(naming.PLATFORM_STACK, RELEASE | {"cells": json.dumps(CELLS[::-1])})
    plain, _ = _env(_workload(reordered, POOL, "prod", "ssc-worker"))
    assert plain["SSC_CELLS"] == SSC_CELLS


def test_the_legacy_cell_is_a_one_cell_list() -> None:
    legacy = {k: v for k, v in RELEASE.items() if k != control.CELLS}
    declared = run(naming.PLATFORM_STACK, legacy | {"cell_label": LABEL, "cell_jwks": CELL_JWKS})
    plain, _ = _env(_workload(declared, POOL, "prod", "ssc-worker"))
    one = control.cells_env([control.Cell(label=LABEL, jwks=CELL_JWKS)])
    assert plain["SSC_CELLS"] == one
    assert json.loads(one) == {LABEL: {"identity_jwks": json.loads(CELL_JWKS)}}
    assert not set(RETIRED) & set(plain)
    (obj,) = [d for d in declared if d.type == "gcp:storage/bucketObject:BucketObject"]
    assert obj.name == f"control-prod-keys-{LABEL}"


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        (json.dumps({"label": LABEL, "jwks": CELL_JWKS}), "must be a list"),
        (json.dumps([{"label": LABEL}]), "label, jwks"),
        (json.dumps([{"label": LABEL, "jwks": CELL_JWKS, "url": "x"}]), "label, jwks"),
        (json.dumps([{"label": LABEL, "jwks": json.loads(CELL_JWKS)}]), "identity_jwks output"),
        (json.dumps([{"label": "Not A Label", "jwks": CELL_JWKS}]), "label"),
        (json.dumps([{"label": LABEL, "jwks": json.dumps({"keys": [{"d": "x"}]})}]), "public"),
        (json.dumps([CELLS[0], CELLS[0]]), "each label once"),
    ],
)
def test_the_cells_are_validated(cells: str, message: str) -> None:
    with pytest.raises(Exception, match=message):
        run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER, "cells": cells})


def test_the_cells_replace_the_legacy_cell() -> None:
    with pytest.raises(Exception, match="not both"):
        run(naming.PLATFORM_STACK, RELEASE | {"cell_label": LABEL, "cell_jwks": CELL_JWKS})


@pytest.mark.parametrize("label", [LABEL, LABEL2, "a1234567", "z" * 16])
def test_the_control_plane_names_cell_hosts_as_the_cells_do(label: str) -> None:
    """The control plane reads only a label from ``SSC_CELLS`` and names the agent, the intake
    and the issuer with ``ssc_shared.hosts``; the cell stack names them with ``naming``."""
    assert naming.agent_url(label) == hosts.agent_url(label, naming.APPS_DOMAIN)
    assert naming.agent_host(label) == hosts.agent_host(label, naming.APPS_DOMAIN)
    assert naming.intake_url(label) == hosts.intake_url(label, naming.APPS_DOMAIN)
    assert naming.intake_host(label) == hosts.intake_host(label, naming.APPS_DOMAIN)
    assert naming.identity_issuer(label) == hosts.identity_issuer(label)
    assert hosts.label_of_issuer(naming.identity_issuer(label)) == label
    assert naming.agent_host(label) == f"ssc--agent.{naming.host_suffix(label)}"
    assert naming.intake_host(label) == f"ssc--secrets.{naming.host_suffix(label)}"
    assert naming.identity_issuer(label) == f"https://{naming.KEYS_HOST}/{label}"


def test_the_placeholder_runs_until_the_release_is_set(bare: list[Declared]) -> None:
    for type_, name in ((SERVICE, "ssc-api"), (SERVICE, "ssc-auth"), (POOL, "ssc-worker")):
        d = _workload(bare, type_, "staging", name)
        (container,) = d.inputs["template"]["containers"]
        assert container["image"] == control.PLACEHOLDER_IMAGE
        assert not {"envs", "commands", "volumeMounts"} & set(container)
        assert "volumes" not in d.inputs["template"]
    pool = _workload(bare, POOL, "staging", "ssc-worker")
    assert pool.inputs["scaling"]["manualInstanceCount"] == 0
    assert not [d for d in bare if d.type == JOB]


def test_every_process_runs_the_pinned_image(released: list[Declared]) -> None:
    commands = {}
    for type_ in (SERVICE, POOL, JOB):
        for d in _of(released, type_, "prod"):
            (container,) = _template(d)["containers"]
            assert container["image"] == IMAGE
            commands[d.inputs["name"]] = tuple(container["commands"])
            assert container["volumeMounts"] == [{"name": "cloudsql", "mountPath": "/cloudsql"}]
            (volume,) = _template(d)["volumes"]
            assert volume["cloudSqlInstance"]["instances"] == [
                "ssc-control-prod:us-central1:ssc-control"
            ]
    assert commands == {
        "ssc-api": ("python", "-m", "ssc_control.api"),
        "ssc-auth": ("python", "-m", "ssc_control.identity", "serve"),
        "ssc-worker": ("python", "-m", "ssc_control.worker"),
        "ssc-control-migrate": control.MIGRATE_COMMAND,
    }


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"control_image": IMAGE}, "set all of"),
        ({"control_image": IMAGE, "auth_jwks": AUTH_JWKS}, "set all of"),
        (
            {
                "control_image": "docker.io/library/python:3.14",
                "auth_jwks": AUTH_JWKS,
                "auth_signing_kid": KID,
            },
            "control_image must be",
        ),
        (
            {
                "control_image": IMAGE.split("@")[0] + ":latest",
                "auth_jwks": AUTH_JWKS,
                "auth_signing_kid": KID,
            },
            "control_image must be",
        ),
        (
            {
                "control_image": IMAGE,
                "auth_jwks": json.dumps({"keys": [{"kid": KID, "d": "secret-part"}]}),
                "auth_signing_kid": KID,
            },
            "public JWKS",
        ),
        (
            {"control_image": IMAGE, "auth_jwks": AUTH_JWKS, "auth_signing_kid": "other"},
            "auth_signing_kid",
        ),
        ({"cell_label": LABEL}, "set both"),
        ({"cell_jwks": CELL_JWKS}, "set both"),
        ({"cell_label": "Not A Label", "cell_jwks": CELL_JWKS}, "label"),
        ({"cell_label": LABEL, "cell_jwks": json.dumps({"keys": [{"d": "x"}]})}, "cell_jwks"),
        ({"control_stages": json.dumps(["dev"])}, "control_stages"),
        ({"control_stages": json.dumps(["prod", "prod"])}, "control_stages"),
        ({"control_stages": json.dumps(["staging"]), "public_stage": "prod"}, "public_stage"),
        ({"worker_instances": "-1"}, "worker_instances"),
    ],
)
def test_the_release_is_all_or_none(settings: dict[str, str], message: str) -> None:
    with pytest.raises(Exception, match=message):
        run(naming.PLATFORM_STACK, {"platform_folder_id": PLATFORM_FOLDER, **settings})


def test_the_api_is_request_billed(released: list[Declared]) -> None:
    for stage in naming.STAGES:
        floor = 1 if stage == "prod" else 0
        for name, low, high in (("ssc-api", floor, 3), ("ssc-auth", 0, 2)):
            service = _workload(released, SERVICE, stage, name).inputs
            assert service["template"]["scaling"]["minInstanceCount"] == low
            assert service["scaling"]["maxInstanceCount"] == high
            (container,) = service["template"]["containers"]
            assert container["resources"]["cpuIdle"] is True
            assert service["ingress"] == "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"


def test_the_worker_runs_always_on_in_a_worker_pool(released: list[Declared]) -> None:
    for stage in naming.STAGES:
        pool = _workload(released, POOL, stage, "ssc-worker").inputs
        assert pool["scaling"] == {"scalingMode": "MANUAL", "manualInstanceCount": 1}
        (container,) = pool["template"]["containers"]
        assert container["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}
    two = run(naming.PLATFORM_STACK, {**RELEASE, "worker_instances": "2"})
    assert _workload(two, POOL, "prod", "ssc-worker").inputs["scaling"]["manualInstanceCount"] == 2


def test_the_database_is_reached_only_by_connector(released: list[Declared]) -> None:
    for stage in naming.STAGES:
        (sql,) = _of(released, "gcp:sql/databaseInstance:DatabaseInstance", stage)
        settings = sql.inputs["settings"]
        assert (sql.inputs["name"], sql.inputs["databaseVersion"]) == ("ssc-control", "POSTGRES_18")
        assert sql.inputs["region"] == naming.REGION
        assert settings["connectorEnforcement"] == "REQUIRED"
        assert settings["ipConfiguration"] == {
            "ipv4Enabled": True,
            "sslMode": "TRUSTED_CLIENT_CERTIFICATE_REQUIRED",
        }
        assert settings["backupConfiguration"]["enabled"] is True
        assert settings["backupConfiguration"]["pointInTimeRecoveryEnabled"] is (stage == "prod")
        assert sql.inputs["deletionProtection"] is True
        (db,) = _of(released, "gcp:sql/database:Database", stage)
        assert db.inputs["name"] == "ssc"
        clients = {
            d.inputs["member"]
            for d in _of(released, "gcp:projects/iAMMember:IAMMember", stage)
            if d.inputs["role"] == "roles/cloudsql.client"
        }
        assert clients == {f"serviceAccount:{email(a, stage)}" for a in control.ACCOUNTS.values()}
    assert not [d for d in released if d.type == "gcp:sql/user:User"]


def test_the_migration_job_runs_as_the_migrator(released: list[Declared]) -> None:
    job = _workload(released, JOB, "prod", "ssc-control-migrate")
    template = _template(job)
    assert template["maxRetries"] == 0
    assert _env(job) == ({}, {"SSC_MIGRATE_DSN"})
    assert "upgrade(os.environ['SSC_MIGRATE_DSN'])" in control.MIGRATE_COMMAND[-1]


def test_the_public_hosts_are_on_one_load_balancer(released: list[Declared]) -> None:
    """Prod holds them when it runs; staging has no load balancer then."""
    assert not _of(released, "gcp:compute/globalAddress:GlobalAddress", "staging")
    assert not _of(released, "gcp:cloudrunv2/serviceIamMember:ServiceIamMember", "staging")
    (url_map,) = [
        d
        for d in _of(released, "gcp:compute/uRLMap:URLMap", "prod")
        if d.inputs["name"] == control.ENTRY
    ]
    hosts = {r["hosts"][0]: r["pathMatcher"] for r in url_map.inputs["hostRules"]}
    assert hosts == {
        "api.delimitus.com": "api",
        "auth.delimitus.com": "auth",
        "keys.delimitus.com": "keys",
    }
    backends = {m["name"]: m["defaultService"] for m in url_map.inputs["pathMatchers"]}
    assert backends["api"] == url_map.inputs["defaultService"]
    (cert,) = _of(released, "gcp:compute/managedSslCertificate:ManagedSslCertificate", "prod")
    assert cert.inputs["managed"]["domains"] == [
        "api.delimitus.com",
        "auth.delimitus.com",
        "keys.delimitus.com",
    ]
    (https,) = _of(released, "gcp:compute/targetHttpsProxy:TargetHttpsProxy", "prod")
    assert https.inputs["sslCertificates"] == [f"{cert.name}-id"]
    (tls,) = _of(released, "gcp:compute/sSLPolicy:SSLPolicy", "prod")
    assert (tls.inputs["minTlsVersion"], tls.inputs["profile"]) == ("TLS_1_2", "MODERN")
    records = {
        d.inputs["name"]: d.inputs for d in released if d.type == "gcp:dns/recordSet:RecordSet"
    }
    address = entry_address(naming.control_project("prod"))
    assert set(records) == {"api.delimitus.com.", "auth.delimitus.com.", "keys.delimitus.com."}
    for record in records.values():
        assert (record["project"], record["managedZone"]) == (naming.BOOTSTRAP_PROJECT, "delimitus")
        assert (record["type"], record["rrdatas"]) == ("A", [address])
    invokers = {
        d.inputs["name"]: d.inputs["member"]
        for d in _of(released, "gcp:cloudrunv2/serviceIamMember:ServiceIamMember", "prod")
    }
    assert invokers == {"ssc-api": "allUsers", "ssc-auth": "allUsers"}


def test_keys_serves_each_cell_jwks(released: list[Declared]) -> None:
    objects = {
        d.name: d.inputs for d in released if d.type == "gcp:storage/bucketObject:BucketObject"
    }
    assert set(objects) == {f"control-prod-keys-{LABEL}", f"control-prod-keys-{LABEL2}"}
    for label, jwks in ((LABEL, CELL_JWKS), (LABEL2, CELL2_JWKS)):
        obj = objects[f"control-prod-keys-{label}"]
        assert obj["bucket"] == "ssc-control-prod-keys"
        assert obj["name"] == f"{label}/jwks.json"
        assert obj["content"]["value"] == jwks
        assert obj["contentType"] == "application/json"
    (backend,) = _of(released, "gcp:compute/backendBucket:BackendBucket", "prod")
    assert backend.inputs["bucketName"] == "ssc-control-prod-keys"
    buckets = {
        d.inputs["name"]: d.inputs
        for d in released
        if d.type == "gcp:storage/bucket:Bucket" and d.inputs["name"].startswith("ssc-control")
    }
    assert buckets["ssc-control-prod-keys"]["publicAccessPrevention"] == "inherited"
    for stage in naming.STAGES:
        assert buckets[f"ssc-control-{stage}-blobs"]["publicAccessPrevention"] == "enforced"
    public = {
        d.inputs["bucket"]
        for d in released
        if d.type == "gcp:storage/bucketIAMMember:BucketIAMMember"
        and d.inputs["member"] == "allUsers"
    }
    assert public == {"ssc-control-prod-keys"}


def test_staging_holds_the_public_hosts_while_prod_does_not_run(bare: list[Declared]) -> None:
    (address,) = _of(bare, "gcp:compute/globalAddress:GlobalAddress", "staging")
    assert address.inputs["name"] == control.ENTRY
    records = [d for d in bare if d.type == "gcp:dns/recordSet:RecordSet"]
    assert {r.inputs["rrdatas"][0] for r in records} == {
        entry_address(naming.control_project("staging"))
    }
    assert not [d for d in bare if d.type == "gcp:storage/bucketObject:BucketObject"]


def test_cloud_run_pulls_the_image_from_the_platform_registry(released: list[Declared]) -> None:
    readers = {
        d.name: d.inputs
        for d in released
        if d.type == "gcp:artifactregistry/repositoryIamMember:RepositoryIamMember"
    }
    for stage in naming.STAGES:
        grant = readers[f"control-{stage}-registry"]
        assert (grant["project"], grant["repository"], grant["role"]) == (
            naming.BOOTSTRAP_PROJECT,
            naming.PLATFORM_REPOSITORY,
            "roles/artifactregistry.reader",
        )
        assert "@gcp-sa-control-" in grant["member"]


def test_the_api_and_the_worker_sign_as_themselves(released: list[Declared]) -> None:
    signing = {
        d.inputs["member"]
        for d in released
        if d.type == "gcp:serviceaccount/iAMMember:IAMMember"
        and d.inputs["role"] == "roles/iam.serviceAccountTokenCreator"
    }
    expected = {
        f"serviceAccount:{email(a, s)}"
        for a in (naming.CONTROL_SA, naming.CONTROL_WORKER_SA)
        for s in naming.STAGES
    }
    assert signing == expected


def test_the_control_accounts_are_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    exported: dict[str, Any] = {}
    monkeypatch.setattr(pulumi, "export", lambda name, value: exported.__setitem__(name, value))
    run(naming.PLATFORM_STACK, RELEASE)
    assert exported["control_public_stage"] == "prod"
    assert set(exported["control_workers"]) == {"staging", "prod"}
    assert set(exported["control_service_accounts"]) == {"staging", "prod"}
    assert set(exported["control_accounts"]["prod"]) == set(control.ACCOUNTS)
    assert set(exported["control_sql_instances"]) == {"staging", "prod"}
    assert "control_entry_address" in exported


def test_names_for_the_control_plane() -> None:
    assert naming.identity_issuer(LABEL) == f"https://keys.delimitus.com/{LABEL}"
    assert naming.deployer_job() == (
        "projects/ssc-platform-0/locations/us-central1/jobs/ssc-cell-deployer"
    )
    assert naming.CONTROL_HOSTS == ("api.delimitus.com", "auth.delimitus.com", "keys.delimitus.com")
    assert all(len(a) <= 30 for a in control.ACCOUNTS.values())
    assert control.public_jwks_kids(AUTH_JWKS) == {KID}
    assert control.public_jwks_kids("[]") is None

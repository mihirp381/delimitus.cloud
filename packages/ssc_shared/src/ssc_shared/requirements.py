"""The platform's runtime rules as data (SSC-093): one source for ``get_platform_requirements``
and ``ssc requirements``, and for ``ssc doctor``.

Each rule and fact names the ``ssc doctor`` codes and the build codes (refusals and notices,
``ssc_contracts.build``) that check it. Every doctor code belongs to exactly one item, and each
finding carries that item's id as its ``requirement``; a test fails when a code is added to the
doctor or the build without a rule here, or a rule names a code that no longer exists. The texts
are written for a coding agent and a person alike: what to do, never how SSC is built.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from ssc_contracts import app_database, app_env
from ssc_contracts.manifest import RESOURCE_CLASSES, SESSION_FRAMEWORKS
from ssc_contracts.packages import APPROVED_PACKAGES, HOW_TO_ASK
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS, SESSION_TIMEOUT_SECONDS


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlatformRule(_Data):
    """One rule or fact: ``doctor`` and ``build`` are the codes that check it, if any."""

    id: str
    text: str
    doctor: tuple[str, ...] = ()
    build: tuple[str, ...] = ()


class ResourceSize(_Data):
    name: str
    vcpu: int
    memory_mib: int
    max_instances: int


class PlatformRequirements(_Data):
    rules: tuple[PlatformRule, ...]
    facts: tuple[PlatformRule, ...]
    resource_classes: tuple[ResourceSize, ...]
    approved_packages: tuple[str, ...]
    how_to_ask_for_a_package: str
    next: str


_SESSIONS = ", ".join(sorted(f.capitalize() for f in SESSION_FRAMEWORKS))

RULES: Final = (
    PlatformRule(
        id="port",
        text=f"Listen on 0.0.0.0 and take the port from the {app_env.PORT} environment variable "
        "(8080 unless port under [runtime] in ssc.toml says otherwise).",
        doctor=("PORT_BINDING",),
        build=("LOCALHOST_BIND",),
    ),
    PlatformRule(
        id="health",
        text="Answer the health path (health_path under [runtime], default /) with a success "
        "within 2 minutes of starting, or the deploy fails with HEALTH_CHECK_FAILED.",
    ),
    PlatformRule(
        id="start",
        text="Say how to start: a start script in package.json, a web: line in a Procfile, or "
        "start under [runtime] in ssc.toml.",
        doctor=("NO_START_COMMAND",),
        build=("BUILD_NO_ENTRYPOINT",),
    ),
    PlatformRule(
        id="one-app",
        text="One app per folder, built from its source; a folder of prebuilt files has nothing "
        "to build.",
        doctor=("NOT_SINGLE_APP",),
    ),
    PlatformRule(
        id="web-app",
        text="A web app in Python or Node. No chat bots and no Java.",
        build=("BUILD_UNSUPPORTED_RUNTIME",),
    ),
    PlatformRule(
        id="non-root",
        text="The app runs as a non-root user and cannot install anything or change the system "
        "while it runs.",
    ),
    PlatformRule(
        id="memory-only",
        text="Write only to memory: the disk is memory, lost on every restart or deploy. "
        f"{app_env.HOME} is {app_env.HOME_VALUE}. Write scratch files under /tmp and keep "
        "lasting data in Postgres.",
        doctor=("WRITES_HOME",),
    ),
    PlatformRule(
        id="postgres",
        text="No SQLite on disk. For lasting data set postgres = true under [state] in ssc.toml "
        f"and connect with {app_env.DATABASE_URL} exactly as given. There is no key-value store "
        "such as Redis: keep that data in a Postgres table.",
        doctor=("STATE_SQLITE_EPHEMERAL",),
        build=("STATE_SQLITE_EPHEMERAL", "KV_STORE"),
    ),
    PlatformRule(
        id="sign-in",
        text="SSC signs people in before a request reaches the app: read who it is from the "
        "X-SSC-Identity note and add no login screen or sign-in vendor. SSC runs no other outside "
        "service for the app.",
        doctor=("EXTERNAL_SERVICE",),
        build=("SUPABASE_AUTH",),
    ),
    PlatformRule(
        id="timers",
        text="No scheduler inside the process: instances stop when idle, so its jobs would not "
        "run. Declare timed jobs under [[schedules]] in ssc.toml.",
        build=("IN_PROCESS_SCHEDULER",),
    ),
    PlatformRule(
        id="egress",
        text="Outbound calls reach only the hosts listed under [egress] hosts in ssc.toml once "
        "they are approved, through the cell's egress proxy; every other address is unreachable. "
        "get_org_deployment_policy lists the hosts approved so far.",
    ),
    PlatformRule(
        id="no-dockerfile",
        text="No Dockerfile: Railpack builds every image from the source and a Dockerfile is "
        "ignored. The install and build steps must succeed.",
        build=("DOCKERFILE_IGNORED", "BUILD_EXITED_NONZERO"),
    ),
    PlatformRule(
        id="dependencies",
        text="Dependencies come from the public registries (PyPI, npm) and install with the lock "
        "file frozen: keep the lock file in step with the dependency list, and use no private "
        "registry.",
        doctor=("LOCKFILE_STALE",),
        build=("BUILD_DEPENDENCY_UNRESOLVED", "BUILD_PRIVATE_REGISTRY"),
    ),
    PlatformRule(
        id="system-packages",
        text="System packages (native libraries, fonts, PDF tools) come only from the platform "
        "package list (approved_packages); a dependency that needs another stops the build with "
        "ADD_APPROVED_PACKAGE. Prefer dependencies that ship prebuilt wheels or binaries.",
        doctor=("ADD_APPROVED_PACKAGE", "NATIVE_LIBRARY"),
        build=("ADD_APPROVED_PACKAGE",),
    ),
    PlatformRule(
        id="no-secrets",
        text="No secret in the code, in ssc.toml or anywhere in the folder: the deploy refuses "
        "it. Read each secret from the environment variable of its name; a person sets it with "
        "ssc secret set, and an agent never asks for or passes on its value.",
        doctor=("SECRET_IN_BUNDLE",),
        build=("SECRET_IN_BUNDLE",),
    ),
    PlatformRule(
        id="manifest",
        text='ssc.toml starts with schema = "ssc/v1" and is strict: an unknown key or a value of '
        "the wrong type is refused. Without one the app gets the defaults, with no database.",
        doctor=("MANIFEST_MISSING", "MANIFEST_INVALID"),
    ),
    PlatformRule(
        id="public-build-values",
        text="Values the browser needs at build time go under [build.public_env.preview] and "
        "[build.public_env.prod] in ssc.toml; anyone who can open the app can read them, so never "
        "a secret.",
        doctor=("PUBLIC_ENV_AT_BUILD",),
    ),
)
"""What an app must do to deploy and run."""

FACTS: Final = (
    PlatformRule(
        id="cold-start",
        text="An app sleeps at zero when nobody uses it and its first request is slow: waking "
        'takes a few seconds, and a browser sees a "waking up" page meanwhile. Do not ping it to '
        "keep it awake; every request is billed.",
    ),
    PlatformRule(
        id="session",
        text=f"{_SESSIONS} apps, and any app with sessions = true under [runtime], are session "
        "apps: one instance, billed while it runs, and each connection drops at "
        f"{SESSION_TIMEOUT_SECONDS // 60} minutes. Any other app's request ends after "
        f"{REQUEST_TIMEOUT_SECONDS // 60} minutes.",
        doctor=("SESSION_FRAMEWORK",),
    ),
    PlatformRule(
        id="classes",
        text="Each app runs in one resource class, small (the default), medium or large, set with "
        "class under [runtime] in ssc.toml; resource_classes gives what each buys.",
    ),
    PlatformRule(
        id="database",
        text=f"An app's database refuses more than {app_database.CONNECTION_LIMIT} connections "
        f"and a stateful app runs {app_database.MAX_INSTANCES} instance, so set every connection "
        f"pool to {app_database.POOL_SIZE}. The first app to ask for a database creates the "
        "company's, which takes about ten minutes once.",
    ),
)
"""How an app behaves once it runs: nothing to fix, but code should expect it."""

NEXT: Final = (
    "Run preflight on the folder (ssc doctor on the command line) and fix every finding marked "
    "block before deploying."
)

REQUIREMENT_OF: Final[Mapping[str, str]] = MappingProxyType(
    {code: item.id for item in (*RULES, *FACTS) for code in item.doctor}
)
"""The rule or fact each ``ssc doctor`` code checks."""


def platform_requirements() -> PlatformRequirements:
    """The rules, the facts, the resource classes and the platform package list."""
    return PlatformRequirements(
        rules=RULES,
        facts=FACTS,
        resource_classes=tuple(
            ResourceSize(
                name=n, vcpu=c.vcpu, memory_mib=c.memory_mib, max_instances=c.max_instances
            )
            for n, c in RESOURCE_CLASSES.items()
        ),
        approved_packages=tuple(sorted(APPROVED_PACKAGES)),
        how_to_ask_for_a_package=HOW_TO_ASK,
        next=NEXT,
    )

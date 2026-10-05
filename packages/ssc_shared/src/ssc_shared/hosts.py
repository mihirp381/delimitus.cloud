"""App hosts (decision 004): one address per app environment on the apps domain.

    prod     <slug>.<cell label>.<apps domain>
    preview  <slug>--preview.<cell label>.<apps domain>

The cell label is the org's opaque ``ssc.org.cell_label``, so one wildcard certificate per org
covers all its hosts and certificate logs never name a customer. A slug never contains ``--``
(nor, therefore, starts with ``xn--``), so ``<slug>--preview`` is never another app's slug and no
two (slug, environment) pairs share a host. A slug has at least 3 characters, so no host label
has ``--`` in positions 3-4 (reserved by IDNA2008) except through ``--preview``. Reserved words
are never slugs.

Pure and strict: nothing here accepts what the rule does not produce. :func:`parse_app_host`
takes the host after the caller has lower-cased it and dropped any port.
"""

import re
from dataclasses import dataclass
from typing import Final, Literal, get_args

type Environment = Literal["prod", "preview"]
type SlugProblem = Literal["pattern", "short", "punycode", "double_dash", "reserved"]

SLUG_PATTERN: Final = r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$"
LABEL_PATTERN: Final = r"^[a-z][a-z0-9]{7,15}$"
MIN_SLUG: Final = 3
CELL_PROJECT_PREFIX: Final = "ssc-c-"
RESERVED_SLUGS: Final = frozenset(
    {"www", "api", "auth", "login", "console", "admin", "status", "static", "keys", "ssc", "mail"}
)
PREVIEW_SUFFIX: Final = "--preview"
MAX_APPS_DOMAIN: Final = 186
"""The longest apps domain whose hosts all fit in 253 characters: the longest first label
(40 + 9), the longest cell label (16) and two dots take the other 67."""

_ENVIRONMENTS: Final[tuple[str, ...]] = get_args(Environment.__value__)
_SLUG: Final = re.compile(SLUG_PATTERN)
_LABEL: Final = re.compile(LABEL_PATTERN)
_DNS_LABEL: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SLUG_MESSAGES: Final[dict[SlugProblem, str]] = {
    "pattern": "an app slug is 3 to 40 lower-case letters, digits and hyphens, starting with a "
    "letter and not ending with a hyphen",
    "short": "an app slug has at least 3 characters",
    "punycode": "an app slug never starts with 'xn--'",
    "double_dash": "an app slug never contains '--', which preview hosts use",
    "reserved": "that app slug is reserved",
}


@dataclass(frozen=True, slots=True)
class AppHost:
    slug: str
    environment: Environment
    cell_label: str


def slug_problem(slug: str) -> SlugProblem | None:
    """Why ``slug`` cannot name an app, or ``None`` when it can."""
    if _SLUG.fullmatch(slug) is None:
        return "pattern"
    if len(slug) < MIN_SLUG:
        return "short"
    if slug.startswith("xn--"):
        return "punycode"
    if "--" in slug:
        return "double_dash"
    if slug in RESERVED_SLUGS:
        return "reserved"
    return None


def check_slug(slug: str) -> str:
    """``slug`` unchanged, or ``ValueError`` saying which rule it breaks."""
    problem = slug_problem(slug)
    if problem is not None:
        raise ValueError(_SLUG_MESSAGES[problem])
    return slug


def check_cell_label(label: str) -> str:
    if _LABEL.fullmatch(label) is None:
        raise ValueError("a cell label is 8 to 16 lower-case letters and digits, letter first")
    return label


def cell_project(label: str) -> str:
    """The GCP project of the cell with this label (decision 021)."""
    return f"{CELL_PROJECT_PREFIX}{check_cell_label(label)}"


def check_apps_domain(domain: str) -> str:
    """A lower-case DNS name of at least two labels and at most :data:`MAX_APPS_DOMAIN`."""
    labels = domain.split(".")
    if (
        len(domain) > MAX_APPS_DOMAIN
        or len(labels) < 2  # noqa: PLR2004  (a registrable name has a label and a suffix)
        or not all(_DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError(f"not a usable apps domain: {domain!r}")
    return domain


def host_label(slug: str, environment: Environment) -> str:
    """The first label of an app environment's host: the key of the snapshot's ``hosts``."""
    if environment not in _ENVIRONMENTS:
        raise ValueError(f"not an environment: {environment!r}")
    return check_slug(slug) + (PREVIEW_SUFFIX if environment == "preview" else "")


def app_host(slug: str, environment: Environment, cell_label: str, apps_domain: str) -> str:
    """The host of one app environment. ``ValueError`` for anything the rule refuses."""
    first = host_label(slug, environment)
    return f"{first}.{check_cell_label(cell_label)}.{check_apps_domain(apps_domain)}"


def app_origin(slug: str, environment: Environment, cell_label: str, apps_domain: str) -> str:
    """``https://`` plus :func:`app_host`: the environment's URL and the identity note audience."""
    return "https://" + app_host(slug, environment, cell_label, apps_domain)


def parse_app_host(host: str, apps_domain: str) -> AppHost | None:
    """The app environment ``host`` names, or ``None`` when :func:`app_host` never makes it."""
    suffix = "." + check_apps_domain(apps_domain)
    if not host.endswith(suffix):
        return None
    parts = host.removesuffix(suffix).split(".")
    if len(parts) != 2:  # noqa: PLR2004  (first label and cell label)
        return None
    first, label = parts
    environment: Environment = "preview" if first.endswith(PREVIEW_SUFFIX) else "prod"
    slug = first.removesuffix(PREVIEW_SUFFIX)
    if slug_problem(slug) is not None or _LABEL.fullmatch(label) is None:
        return None
    return AppHost(slug, environment, label)

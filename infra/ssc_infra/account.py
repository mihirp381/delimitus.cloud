"""The Google Cloud account SSC runs in, read from stack config and never kept in source."""

import re
from dataclasses import dataclass
from typing import Final

import pulumi

EMAIL: Final = r"[^@\s:]+@[^@\s]+\.[^@\s]+"
SHAPES: Final = {
    "org_id": (re.compile(r"[0-9]+"), "the organisation's numeric id"),
    "billing_account": (
        re.compile(r"[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}"),
        "a billing account id, XXXXXX-XXXXXX-XXXXXX",
    ),
    "operator": (
        re.compile(rf"(user|group|serviceAccount):{EMAIL}"),
        "user:<email>, group:<email> or serviceAccount:<email>",
    ),
    "github_repository": (re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"), "<owner>/<name>"),
}


@dataclass(frozen=True, slots=True)
class Account:
    org_id: str
    billing_account: str
    operator: str
    github_repository: str

    @property
    def org(self) -> str:
        return f"organizations/{self.org_id}"


def check(key: str, value: str) -> str:
    shape, hint = SHAPES[key]
    if not shape.fullmatch(value):
        raise ValueError(f"{key} must be {hint}")
    return value


def require(config: pulumi.Config, key: str) -> str:
    return check(key, config.require(key))


def from_config(config: pulumi.Config) -> Account:
    return Account(
        org_id=require(config, "org_id"),
        billing_account=require(config, "billing_account"),
        operator=require(config, "operator"),
        github_repository=require(config, "github_repository"),
    )


def operator_email(operator: str) -> str:
    """The operator's address when the operator is one person, else empty."""
    return operator.removeprefix("user:") if operator.startswith("user:") else ""


def org_principals(org_id: str) -> str:
    return f"//cloudresourcemanager.googleapis.com/organizations/{org_id}"


def pam_agent(org_id: str) -> str:
    return f"serviceAccount:service-org-{org_id}@gcp-sa-pam.iam.gserviceaccount.com"

"""The account SSC runs in comes from stack config, checked for shape."""

import pulumi
import pytest
from pulumi.runtime import config as runtime_config

from mockcloud import ACCOUNT, PLATFORM_CONFIG
from ssc_infra import account, naming


def _config(values: dict[str, str]) -> pulumi.Config:
    runtime_config.set_all_config({f"{naming.PROJECT}:{k}": v for k, v in values.items()})
    return pulumi.Config(naming.PROJECT)


def test_the_account_is_read_from_config() -> None:
    assert account.from_config(_config(PLATFORM_CONFIG)) == ACCOUNT
    assert ACCOUNT.org_id == "000000000000"
    assert ACCOUNT.billing_account == "AAAAAA-BBBBBB-CCCCCC"
    assert ACCOUNT.operator == "user:operator@example.com"
    assert ACCOUNT.github_repository == "example-org/example-repo"
    assert ACCOUNT.github_repository_id == "100000001"
    assert ACCOUNT.github_owner_id == "200000002"


@pytest.mark.parametrize("key", sorted(PLATFORM_CONFIG))
def test_every_key_is_required(key: str) -> None:
    values = {k: v for k, v in PLATFORM_CONFIG.items() if k != key}
    with pytest.raises(pulumi.ConfigMissingError, match=key):
        account.from_config(_config(values))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("org_id", "org_000000000000"),
        ("org_id", ""),
        ("billing_account", "AAAAAA-BBBBBB"),
        ("billing_account", "aaaaaa-bbbbbb-cccccc"),
        ("operator", "operator@example.com"),
        ("operator", "user:not-an-email"),
        ("operator", "domain:example.com"),
        ("github_repository", "example-repo"),
        ("github_repository", "example-org/example-repo/extra"),
        ("github_repository_id", "example-repo"),
        ("github_repository_id", "-1"),
        ("github_owner_id", "example-org"),
    ],
)
def test_a_wrong_shape_names_the_key_and_not_the_value(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=f"^{key} must be") as caught:
        account.from_config(_config(PLATFORM_CONFIG | {key: value}))
    if value:
        assert value not in str(caught.value)


@pytest.mark.parametrize(
    "operator",
    [
        "user:operator@example.com",
        "group:platform-admins@example.com",
        "serviceAccount:ops@example-project.iam.gserviceaccount.com",
    ],
)
def test_the_operator_may_be_a_user_a_group_or_a_service_account(operator: str) -> None:
    assert account.from_config(_config(PLATFORM_CONFIG | {"operator": operator})).operator == (
        operator
    )


def test_the_organisation_s_principals_and_pam_agent_follow_the_org_id() -> None:
    assert ACCOUNT.org == "organizations/000000000000"
    assert account.org_principals("000000000000") == (
        "//cloudresourcemanager.googleapis.com/organizations/000000000000"
    )
    assert account.pam_agent("000000000000") == (
        "serviceAccount:service-org-000000000000@gcp-sa-pam.iam.gserviceaccount.com"
    )


def test_the_operator_s_address_is_known_only_for_a_user() -> None:
    assert account.operator_email("user:operator@example.com") == "operator@example.com"
    assert account.operator_email("group:platform-admins@example.com") == ""

"""Stack files are local: bootstrap recreates one with the KMS secrets provider."""

from pathlib import Path

import pytest

from ssc_infra import bootstrap, naming


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    made: list[tuple[str, ...]] = []

    def fake(*args: str, cwd: str) -> str:
        made.append(args)
        return '[{"name": "platform"}]' if args[:2] == ("stack", "ls") else ""

    monkeypatch.setattr(bootstrap, "INFRA_DIR", str(tmp_path))
    monkeypatch.setattr(bootstrap, "pulumi", fake)
    return made


def test_a_missing_stack_file_gets_the_kms_provider_back(
    tmp_path: Path, calls: list[tuple[str, ...]]
) -> None:
    bootstrap.platform_stack("370253497254")
    assert (tmp_path / "Pulumi.platform.yaml").read_text() == (
        f"secretsprovider: {naming.SECRETS_PROVIDER}\n"
    )
    assert ("config", "set", "--stack", "platform", "platform_folder_id", "370253497254") in calls
    assert (
        "config",
        "set",
        "--stack",
        "platform",
        "gcp:disableGlobalProjectWarning",
        "true",
    ) in calls


def test_an_existing_stack_file_is_left_alone(tmp_path: Path, calls: list[tuple[str, ...]]) -> None:
    existing = tmp_path / "Pulumi.platform.yaml"
    existing.write_text("secretsprovider: kept\nencryptedkey: kept\n")
    bootstrap.platform_stack("370253497254")
    assert existing.read_text() == "secretsprovider: kept\nencryptedkey: kept\n"


def test_a_new_stack_is_made_with_the_kms_provider(calls: list[tuple[str, ...]]) -> None:
    bootstrap.cell_stack("testcell03", probe=False)
    assert (
        "stack",
        "init",
        "c-testcell03",
        f"--secrets-provider={naming.SECRETS_PROVIDER}",
    ) in calls
    assert ("config", "set", "--stack", "c-testcell03", "probe", "false") in calls


ACCOUNT_ENV = {"SSC_ORG_ID": "000000000000", "SSC_BILLING_ACCOUNT": "AAAAAA-BBBBBB-CCCCCC"}


def test_the_org_and_billing_account_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert bootstrap.account_env(ACCOUNT_ENV) == ("000000000000", "AAAAAA-BBBBBB-CCCCCC")
    seen: list[tuple[str, ...]] = []

    def fake_json(*args: str) -> object:
        seen.append(args)
        return [{"name": "folders/444"}]

    monkeypatch.setattr(bootstrap, "gcloud_json", fake_json)
    assert bootstrap.platform_folder("000000000000") == "444"
    assert "--organization=000000000000" in seen[0]


@pytest.mark.parametrize(
    ("environ", "named"),
    [
        ({"SSC_BILLING_ACCOUNT": "AAAAAA-BBBBBB-CCCCCC"}, "SSC_ORG_ID"),
        ({"SSC_ORG_ID": "000000000000"}, "SSC_BILLING_ACCOUNT"),
        (ACCOUNT_ENV | {"SSC_ORG_ID": "org-1"}, "SSC_ORG_ID"),
        (ACCOUNT_ENV | {"SSC_BILLING_ACCOUNT": "AAAAAA"}, "SSC_BILLING_ACCOUNT"),
    ],
)
def test_a_missing_or_malformed_variable_stops_the_bootstrap_with_one_line(
    environ: dict[str, str],
    named: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key in ACCOUNT_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(bootstrap, "gcloud_json", pytest.fail)
    assert bootstrap.main([]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert named in err

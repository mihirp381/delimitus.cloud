"""Step 9 of ``ssc_infra.onboard`` against the real ``pulumi`` CLI, on two local file backends: one
stands for the onboarding folder and the other for the state bucket (SSC-091 phase 2b).

Nothing here reaches a network or the cloud. The project is a ``runtime: yaml`` one with outputs
only (the language host ships inside the CLI, so no plugin is downloaded), the secrets provider is
a passphrase one (a KMS key needs the network), and the Pulumi home is a folder of the test's own.
What it shows is what the real step relies on: ``stack init`` on the second backend leaves the key
lines of ``Pulumi.<stack>.yaml`` alone, a secret config value and a secret output survive the
export and the import, and the preview of the moved stack has no changes. Skipped without
``pulumi`` on the path.
"""

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from ssc_infra import naming as n
from ssc_infra import onboard
from ssc_infra.onboard import Onboarding, Options, Tools
from ssc_infra.run import CommandError, pulumi

pytestmark = pytest.mark.skipif(shutil.which("pulumi") is None, reason="pulumi is not installed")

LABEL = "movecell01"
STACK = n.cell_stack(LABEL)
PASSPHRASE = "test-passphrase-not-a-secret"
PROBE_IMAGE = f"{n.platform_registry()}/ssc-probe@sha256:" + "b" * 64
PROGRAM = """name: movecheck
runtime: yaml
config:
  token:
    type: string
    secret: true
outputs:
  plain: hello
  sealed:
    fn::secret: shh-output
"""


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.infra = tmp_path / "infra"
        self.infra.mkdir()
        (self.infra / "Pulumi.yaml").write_text(PROGRAM)
        self.state_root = tmp_path / "state"
        self.bucket = tmp_path / "bucket"
        self.bucket.mkdir()
        self.folder = self.state_root / LABEL
        self.env: dict[str, str] = {
            **os.environ,
            "PULUMI_HOME": str(tmp_path / "home"),
            "PULUMI_SKIP_UPDATE_CHECK": "true",
            "PULUMI_CONFIG_PASSPHRASE": PASSPHRASE,
            "PULUMI_BACKEND_URL": f"file://{self.folder}",
        }
        self.stack_file = self.infra / f"Pulumi.{STACK}.yaml"
        self.fail_import = False

    def tools(self) -> Tools:
        def run(*args: str, cwd: str | None = None, env: Mapping[str, str] | None = None) -> str:
            if self.fail_import and args[:2] == ("stack", "import"):
                raise CommandError("pulumi stack import failed: stopped for the test")
            return pulumi(*args, cwd=cwd, env=env)

        return Tools(
            pulumi=run,
            env=self.env,
            infra_dir=str(self.infra),
            state_root=self.state_root,
            bucket_url=f"file://{self.bucket}",
            secrets_provider="passphrase",
            out=lambda _text: None,
            err=lambda _text: None,
        )

    def onboarding(self) -> Onboarding:
        opts = Options(label=LABEL, settings={}, probe_image=PROBE_IMAGE, from_step=9)
        return Onboarding(opts, self.tools())

    def on(self, url: str, *args: str) -> str:
        return pulumi(*args, cwd=str(self.infra), env={**self.env, "PULUMI_BACKEND_URL": url})

    def local_stack(self) -> None:
        """What steps 1 to 3 leave: a stack with a secret config value and one apply."""
        self.folder.mkdir(parents=True, mode=0o700)
        self.on(f"file://{self.folder}", "stack", "init", STACK, "--secrets-provider=passphrase")
        self.on(f"file://{self.folder}", "config", "set", "--secret", "token", "s3cret-value")
        self.on(f"file://{self.folder}", "up", "--stack", STACK, "--yes", "--skip-preview")


def key_text(stack_file: Path) -> list[str]:
    return [
        line
        for line in stack_file.read_text().splitlines()
        if line.split(":")[0] in onboard.KEY_LINES
    ]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_the_state_moves_with_its_key_its_secret_config_and_its_secret_output(rig: Rig) -> None:
    rig.local_stack()
    keys = key_text(rig.stack_file)
    assert keys  # the passphrase provider's salt: what a KMS provider's key is, offline
    before = rig.stack_file.read_text()

    rig.onboarding().move_state()

    assert rig.stack_file.read_text() == before  # init on the second backend left the file alone
    assert key_text(rig.stack_file) == keys
    bucket = f"file://{rig.bucket}"
    assert rig.on(bucket, "config", "get", "token").strip() == "s3cret-value"
    outputs = json.loads(rig.on(bucket, "stack", "output", "--json", "--show-secrets"))
    assert outputs == {"plain": "hello", "sealed": "shh-output"}
    rig.on(bucket, "preview", "--stack", STACK, "--expect-no-changes")
    assert not rig.folder.exists()
    (moved,) = rig.state_root.glob(f"{LABEL}.moved-*")
    assert (moved / "export.json").exists()
    assert (moved / f"Pulumi.{STACK}.yaml.before-init").read_text() == before
    assert "s3cret-value" not in (moved / "export.json").read_text()


def test_a_move_that_stopped_after_the_init_is_finished_by_running_it_again(rig: Rig) -> None:
    rig.local_stack()
    rig.fail_import = True
    with pytest.raises(CommandError, match="stopped for the test"):
        rig.onboarding().move_state()
    assert rig.folder.exists()
    assert STACK in rig.on(f"file://{rig.bucket}", "stack", "ls", "--json")  # init was done

    rig.fail_import = False
    rig.onboarding().move_state()  # init is not repeated: pulumi would say "already exists"

    bucket = f"file://{rig.bucket}"
    assert rig.on(bucket, "config", "get", "token").strip() == "s3cret-value"
    rig.on(bucket, "preview", "--stack", STACK, "--expect-no-changes")
    assert len(list(rig.state_root.glob(f"{LABEL}.moved-*"))) == 1


def test_a_move_that_stopped_after_the_import_is_finished_by_running_it_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.local_stack()
    onboarding = rig.onboarding()
    original = onboarding.t.pulumi

    def stop_at_the_preview(
        *args: str, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        if args[0] == "preview":
            raise CommandError("pulumi preview failed: stopped for the test")
        return original(*args, cwd=cwd, env=env)

    monkeypatch.setattr(onboarding.t, "pulumi", stop_at_the_preview)
    with pytest.raises(CommandError, match="stopped for the test"):
        onboarding.move_state()
    assert rig.folder.exists()

    rig.onboarding().move_state()  # neither init nor import is repeated

    assert len(list(rig.state_root.glob(f"{LABEL}.moved-*"))) == 1
    rig.on(f"file://{rig.bucket}", "preview", "--stack", STACK, "--expect-no-changes")

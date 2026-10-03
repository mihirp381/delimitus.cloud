"""One org with two apps, used by the gateway tests (SSC-018). Imported as ``edge_world``.
Payroll is a session app: its environment carries ``timeout_seconds``; ledger's do not."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from ssc_contracts.snapshot import FORMAT_V1
from ssc_edge.gate import Gate, GateConfig
from ssc_edge.keys import Keyring, new_keyring, parse_keyring
from ssc_edge.server import gate_for
from ssc_edge.session import Session, SessionCodec, new_sid
from ssc_shared.access import AccessView
from ssc_shared.runtime import SESSION_TIMEOUT_SECONDS

ORG = "org_" + "a" * 20
OTHER_ORG = "org_" + "b" * 20
LEDGER, PAYROLL = "app_" + "l" * 20, "app_" + "p" * 20
PROD, PREVIEW, PAY = "env_" + "p" * 20, "env_" + "v" * 20, "env_" + "y" * 20
ADA, BEN, CY = ("usr_" + c * 20 for c in "abc")
FIN, OPS = "grp_" + "f" * 20, "grp_" + "o" * 20
LABEL = "bcdfghjklmnp"
DOMAIN = "apps.test"
NOW = 1_790_000_000
HOST = f"ledger.{LABEL}.{DOMAIN}"
PREVIEW_HOST = f"ledger--preview.{LABEL}.{DOMAIN}"
PAY_HOST = f"payroll.{LABEL}.{DOMAIN}"
NOWHERE_HOST = f"nothere.{LABEL}.{DOMAIN}"
NONCE = "n" * 43
LOGIN = f"__Host-ssc-login={NONCE}"


def gnt(n: int) -> str:
    return f"gnt_{n:020d}"


def snapshot(version: int = 1, **changes: Any) -> dict[str, Any]:
    env = {"status": "active"}
    base: dict[str, Any] = {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": version,
        "compiled_at": "2026-10-01T12:00:00Z",
        "environments": {
            PROD: {"app_id": LEDGER, "name": "prod", "floor": "user", **env},
            PREVIEW: {"app_id": LEDGER, "name": "preview", "floor": "builder", **env},
            PAY: {
                "app_id": PAYROLL,
                "name": "prod",
                "floor": "user",
                "timeout_seconds": SESSION_TIMEOUT_SECONDS,
                **env,
            },
        },
        "hosts": {"ledger": PROD, "ledger--preview": PREVIEW, "payroll": PAY},
        "grants": {
            PROD: [
                {"grant_id": gnt(1), "role": "user", "subject_kind": "org", "subject_id": None},
                {"grant_id": gnt(2), "role": "builder", "subject_kind": "group", "subject_id": FIN},
            ],
            PREVIEW: [
                {"grant_id": gnt(3), "role": "user", "subject_kind": "user", "subject_id": BEN},
                {"grant_id": gnt(4), "role": "builder", "subject_kind": "user", "subject_id": ADA},
            ],
            PAY: [{"grant_id": gnt(5), "role": "user", "subject_kind": "user", "subject_id": ADA}],
        },
        "groups_by_user": {ADA: [FIN, OPS]},
        "users": {
            ADA: {"status": "active"},
            BEN: {"status": "active"},
            CY: {"status": "deactivated"},
        },
        "ceiling": None,
    }
    base.update(changes)
    return base


def config(**changes: Any) -> GateConfig:
    base: dict[str, Any] = {
        "org_id": ORG,
        "cell_label": LABEL,
        "apps_domain": DOMAIN,
        "auth_url": "https://auth.example.test",
        "issuer": f"https://keys.example.test/{LABEL}",
        "project_number": "123456789012",
        "region": "us-central1",
        "max_body_bytes": 1024,
    }
    base.update(changes)
    return GateConfig(**base)


def session(user: str = ADA, *, org: str = ORG, iat: int = NOW - 60, life: int = 3600) -> Session:
    return Session(
        sid=new_sid(),
        sub=user,
        org=org,
        name="Ada L",
        email="ada@example.test",
        iat=iat,
        exp=iat + life,
    )


@dataclass
class FakeRedeemer:
    sessions: dict[str, Session] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    nonce: str = NONCE

    async def redeem(self, code: str, host: str, nonce: str) -> Session | None:
        self.calls.append((code, host, nonce))
        return self.sessions.get(code) if nonce == self.nonce else None


@dataclass
class World:
    keyring: Keyring
    view: AccessView | None
    redeemer: FakeRedeemer
    now: int = NOW

    @property
    def codec(self) -> SessionCodec:
        return SessionCodec(self.keyring.session, active=self.keyring.session_kid)

    def gate(self, **changes: Any) -> Gate:
        return gate_for(
            config(**changes),
            self.keyring,
            view=lambda: self.view,
            clock=lambda: self.now,
            redeemer=self.redeemer,
            nonce=lambda: NONCE,
        )

    def cookie(self, s: Session | None = None, host: str = HOST) -> str:
        return f"__Host-ssc-session={self.codec.seal(s or session(iat=self.now - 60), host)}"


@pytest.fixture
def world() -> World:
    return World(
        keyring=parse_keyring(new_keyring()),
        view=AccessView.from_document(snapshot()),
        redeemer=FakeRedeemer(),
    )

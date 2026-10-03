"""The workload token (SSC-050): only a Google-signed ID token for this gateway, from an app
account of this cell's project, names an environment. Any service account anywhere can mint a
token naming the gateway, so a forged or foreign token is refused here, not by Cloud Run."""

import time

import jwt
import pytest
from datagw_world import (
    AUDIENCE,
    CERTS,
    FORGED_KEY,
    GOOGLE_KEY,
    PREVIEW,
    PROD,
    PROJECT,
    account,
    certs_transport,
    google_token,
)

from ssc_datagw.workload import (
    REFETCH_SECONDS,
    GoogleWorkloads,
    WorkloadKeysUnavailableError,
    WorkloadRefusedError,
)


def workloads(
    fetches: list[str] | None = None, *, fail: bool = False, clock: list[float] | None = None
) -> GoogleWorkloads:
    now = clock or [1000.0]
    return GoogleWorkloads(
        audience=AUDIENCE,
        project_id=PROJECT,
        transport=certs_transport(fetches, fail=fail),
        certs_url=CERTS,
        clock=lambda: now[0],
    )


async def test_an_app_token_names_its_environment() -> None:
    w = await workloads().verify(f"Bearer {google_token(PREVIEW)}")
    assert w.env_id == PREVIEW
    assert w.account == account(PREVIEW)


async def test_the_short_issuer_is_google_too() -> None:
    w = await workloads().verify(f"Bearer {google_token(iss='accounts.google.com')}")
    assert w.env_id == PROD


def _hs256() -> str:
    claims = jwt.decode(google_token(), options={"verify_signature": False})
    return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": "google-1"})


def _alg_none() -> str:
    claims = jwt.decode(google_token(), options={"verify_signature": False})
    return jwt.encode(claims, None, algorithm="none", headers={"kid": "google-1"})


FORGED = {
    "no token": lambda: "",
    "not bearer": lambda: f"Basic {google_token()}",
    "not a jwt": lambda: "Bearer not.a.token",
    "signed by another key under Google's kid": lambda: f"Bearer {google_token(key=FORGED_KEY)}",
    "unknown kid": lambda: f"Bearer {google_token(kid='made-up')}",
    "alg none": lambda: f"Bearer {_alg_none()}",
    "HS256": lambda: f"Bearer {_hs256()}",
    "another audience": lambda: f"Bearer {google_token(aud='https://elsewhere.run.app')}",
    "another issuer": lambda: f"Bearer {google_token(iss='https://evil.example')}",
    "email not verified": lambda: f"Bearer {google_token(email_verified=False)}",
    "no email_verified": lambda: f"Bearer {google_token(email_verified=None)}",
    "no email": lambda: f"Bearer {google_token(email=None)}",
    "an app of another cell": lambda: (
        f"Bearer {google_token(email=account(PROD, 'ssc-c-other1234'))}"
    ),
    "the gateway's own account": lambda: (
        f"Bearer {google_token(email=f'ssc-gateway@{PROJECT}.iam.gserviceaccount.com')}"
    ),
    "a user, not a service account": lambda: f"Bearer {google_token(email='ada@example.com')}",
    "a lookalike project": lambda: f"Bearer {google_token(email=account(PROD, PROJECT + 'x'))}",
    "expired": lambda: (
        f"Bearer {google_token(iat=int(time.time()) - 7200, exp=int(time.time()) - 60)}"
    ),
    "no exp": lambda: f"Bearer {google_token(exp=None)}",
}


@pytest.mark.parametrize("case", sorted(FORGED))
async def test_a_forged_or_foreign_token_is_refused(case: str) -> None:
    with pytest.raises(WorkloadRefusedError):
        await workloads().verify(FORGED[case]())


async def test_an_unknown_kid_refetches_google_keys_at_most_every_thirty_seconds() -> None:
    fetches: list[str] = []
    clock = [1000.0]
    w = workloads(fetches, clock=clock)
    await w.verify(f"Bearer {google_token()}")
    assert fetches == [CERTS]
    for _ in range(3):
        with pytest.raises(WorkloadRefusedError):
            await w.verify(f"Bearer {google_token(kid='rotated')}")
    assert len(fetches) == 1
    clock[0] += REFETCH_SECONDS + 1
    with pytest.raises(WorkloadRefusedError):
        await w.verify(f"Bearer {google_token(kid='rotated')}")
    assert len(fetches) == 2


async def test_no_google_keys_is_unavailable_not_refused() -> None:
    with pytest.raises(WorkloadKeysUnavailableError):
        await workloads(fail=True).verify(f"Bearer {google_token(key=GOOGLE_KEY)}")

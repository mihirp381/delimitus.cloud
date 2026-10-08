"""The workload token (SSC-050): only a Google-signed ID token for this gateway, from an app
account of this cell's project, names an environment. Any service account anywhere can mint a
token naming the gateway, so a forged or foreign token is refused here, not by Cloud Run. The
cell agent's account names no environment of its own: it says which in a header, and only when
the caller asks for an agent (the schema route, GA-5.8)."""

import time

import jwt
import pytest
from datagw_world import (
    AGENT,
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
    fetches: list[str] | None = None,
    *,
    fail: bool = False,
    clock: list[float] | None = None,
    agent_account: str | None = AGENT,
) -> GoogleWorkloads:
    now = clock or [1000.0]
    return GoogleWorkloads(
        audience=AUDIENCE,
        project_id=PROJECT,
        agent_account=agent_account,
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


AGENT_TOKEN = f"Bearer {google_token(email=AGENT)}"


async def test_the_cell_agent_names_the_environment_it_asks_for() -> None:
    w = await workloads().verify(AGENT_TOKEN, agent=True, environment=PREVIEW)
    assert (w.env_id, w.account, w.agent) == (PREVIEW, AGENT, True)


async def test_an_app_asked_as_an_agent_is_still_its_own_environment() -> None:
    w = await workloads().verify(f"Bearer {google_token(PROD)}", agent=True, environment=PREVIEW)
    assert (w.env_id, w.agent) == (PROD, False)


@pytest.mark.parametrize(
    ("environment", "reason"),
    [
        (None, "the cell agent sent no X-SSC-Environment"),
        ("", "the cell agent sent no X-SSC-Environment"),
        ("env_short", "the cell agent's X-SSC-Environment is malformed"),
        ("ENV_" + "P" * 20, "the cell agent's X-SSC-Environment is malformed"),
        ("app_" + "l" * 20, "the cell agent's X-SSC-Environment is malformed"),
    ],
)
async def test_the_cell_agent_without_a_well_formed_environment_is_refused(
    environment: str | None, reason: str
) -> None:
    with pytest.raises(WorkloadRefusedError, match=reason):
        await workloads().verify(AGENT_TOKEN, agent=True, environment=environment)


async def test_the_cell_agent_is_refused_where_no_agent_is_asked_for() -> None:
    with pytest.raises(WorkloadRefusedError):
        await workloads().verify(AGENT_TOKEN, environment=PREVIEW)


async def test_no_agent_account_or_another_one_is_refused() -> None:
    other = f"Bearer {google_token(email=f'ssc-cell-agent@{PROJECT}x.iam.gserviceaccount.com')}"
    with pytest.raises(WorkloadRefusedError):
        await workloads(agent_account=None).verify(AGENT_TOKEN, agent=True, environment=PREVIEW)
    with pytest.raises(WorkloadRefusedError):
        await workloads().verify(other, agent=True, environment=PREVIEW)

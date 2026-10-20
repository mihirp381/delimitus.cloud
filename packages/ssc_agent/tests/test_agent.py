"""The agent's configuration, metadata tokens, and the pure parts of the Cloud Run driver."""

import base64
import json

import httpx2
import pytest

from ssc_agent.__main__ import (
    CONNECTION_TAG_ENV,
    DATA_SA_ENV,
    ENV,
    LOG_VIEW_ENV,
    ConfigError,
    cell_from_env,
    connections_from_env,
    log_views_from_env,
)
from ssc_agent.cloud_run import (  # pyright: ignore[reportPrivateUsage]
    _bare,
    _cpu,
    _memory_mib,
    _readiness,
    _traffic,
)
from ssc_agent.metadata import (
    EARLY_SECONDS,
    MIN_CACHE_SECONDS,
    MetadataAccessTokens,
    MetadataError,
    MetadataIdTokens,
)

FULL = {name: f"value-{field}" for field, name in ENV.items()}


def test_cell_from_env_needs_every_name() -> None:
    cell = cell_from_env(FULL)
    assert cell.project == "value-project"
    assert cell.invoker == "value-invoker"
    for name in ENV.values():
        with pytest.raises(ConfigError, match=name):
            cell_from_env({k: v for k, v in FULL.items() if k != name})


def test_log_views_from_env_takes_one_or_more_views() -> None:
    app = "projects/cell-project/locations/us-central1/buckets/_Default/views/ssc-app-logs"
    builds = app.replace("ssc-app-logs", "ssc-build-logs")
    assert log_views_from_env({}) is None
    assert log_views_from_env({LOG_VIEW_ENV: app}) == (app,)
    assert log_views_from_env({LOG_VIEW_ENV: f"{app}, {builds}"}) == (app, builds)
    for bad in ("projects/cell-project/logs/run", f"{app},", f"{app},logs/x", "a b"):
        with pytest.raises(ConfigError, match=LOG_VIEW_ENV):
            log_views_from_env({LOG_VIEW_ENV: bad})


def test_connection_secrets_need_the_data_account_and_the_tag() -> None:
    data = "ssc-data@cell-project.iam.gserviceaccount.com"
    tag = "tagKeys/1001=tagValues/2002"
    assert connections_from_env({}) is None
    found = connections_from_env({DATA_SA_ENV: data, CONNECTION_TAG_ENV: tag})
    assert found is not None
    assert (found.reader, found.tag_key, found.tag_value) == (
        data,
        "tagKeys/1001",
        "tagValues/2002",
    )
    for half in ({DATA_SA_ENV: data}, {CONNECTION_TAG_ENV: tag}):
        with pytest.raises(ConfigError, match="both"):
            connections_from_env(half)
    for bad in ("tagKeys/1", "tagValues/2=tagKeys/1", "proj/ssc-secret-kind=connection", f"{tag},"):
        with pytest.raises(ConfigError, match=CONNECTION_TAG_ENV):
            connections_from_env({DATA_SA_ENV: data, CONNECTION_TAG_ENV: bad})


async def test_access_token_is_cached() -> None:
    calls: list[httpx2.Request] = []

    def metadata(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"access_token": "tok", "expires_in": 3599})

    tokens = MetadataAccessTokens(httpx2.AsyncClient(transport=httpx2.MockTransport(metadata)))
    assert await tokens() == "tok"
    assert await tokens() == "tok"
    assert len(calls) == 1
    assert calls[0].headers["Metadata-Flavor"] == "Google"


async def test_access_token_failure_names_no_token() -> None:
    tokens = MetadataAccessTokens(
        httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _: httpx2.Response(403)))
    )
    with pytest.raises(MetadataError, match="HTTPStatusError"):
        await tokens()


def _id_token(exp: float | None) -> str:
    def part(doc: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b"=").decode()

    claims: dict[str, object] = {"aud": "https://gw.test"}
    if exp is not None:
        claims["exp"] = exp
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"


@pytest.mark.parametrize(
    ("exp_in", "refresh_after"),
    [
        (600.0, 600 - EARLY_SECONDS),  # a token that lives ten minutes is kept five
        (7200.0, 3600 - EARLY_SECONDS),  # never longer than an ID token lives
        (60.0, MIN_CACHE_SECONDS),  # one nearly expired is kept only the floor
        (None, MIN_CACHE_SECONDS),  # and so is one whose exp cannot be read
    ],
)
async def test_an_id_token_is_cached_until_its_own_exp_less_five_minutes(
    exp_in: float | None, refresh_after: float
) -> None:
    now = [5000.0]
    wall = 1_800_000_000.0
    calls: list[httpx2.Request] = []

    def metadata(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, text=_id_token(None if exp_in is None else wall + exp_in))

    tokens = MetadataIdTokens(
        "https://gw.test",
        httpx2.AsyncClient(transport=httpx2.MockTransport(metadata)),
        monotonic=lambda: now[0],
        wall=lambda: wall,
    )
    await tokens()
    now[0] += refresh_after - 1
    await tokens()
    assert len(calls) == 1
    now[0] += 2
    await tokens()
    assert len(calls) == 2  # noqa: PLR2004


def test_proto3_zeros_compare_equal() -> None:
    assert _bare({"scaling": {"minInstanceCount": 0, "maxInstanceCount": 2}}) == _bare(
        {"scaling": {"maxInstanceCount": 2}}
    )
    assert _bare({"invokerIamDisabled": False}) == _bare({})


@pytest.mark.parametrize(("value", "cpu"), [("1", 1.0), ("2", 2.0), ("1000m", 1.0), ("0.5", 0.5)])
def test_cpu(value: str, cpu: float) -> None:
    assert _cpu(value) == cpu


@pytest.mark.parametrize(("value", "mib"), [("512Mi", 512), ("2Gi", 2048), ("4096Mi", 4096)])
def test_memory(value: str, mib: int) -> None:
    assert _memory_mib(value) == mib


def test_latest_traffic_resolves_to_a_revision() -> None:
    svc = {
        "latestReadyRevision": "projects/p/locations/r/services/s/revisions/s-00002-abc",
        "trafficStatuses": [
            {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 90},
            {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": "s-1", "percent": 10},
        ],
    }
    assert _traffic(svc) == {"s-00002-abc": 90, "s-1": 10}


def _conditions(**states: str) -> list[dict[str, str]]:
    return [{"type": k, "state": f"CONDITION_{v}"} for k, v in states.items()]


@pytest.mark.parametrize(
    ("revision", "seen"),
    [
        (
            {"conditions": _conditions(Ready="SUCCEEDED", ContainerHealthy="SUCCEEDED")},
            (True, False),
        ),
        ({"conditions": _conditions(Ready="FAILED", ContainerHealthy="FAILED")}, (False, True)),
        (
            {"conditions": _conditions(Ready="RECONCILING", ContainerHealthy="FAILED")},
            (False, True),
        ),
        (
            {"conditions": _conditions(Ready="RECONCILING", ContainerHealthy="RECONCILING")},
            (None, False),
        ),
        # Cell 1, 2026-10-07: a revision that never listens, a second after it was made.
        (
            {
                "conditions": _conditions(
                    Ready="SUCCEEDED", Retry="SUCCEEDED", ContainerReady="SUCCEEDED"
                )
            },
            (None, False),
        ),
        (
            {
                "reconciling": True,
                "conditions": _conditions(Ready="SUCCEEDED", ContainerHealthy="SUCCEEDED"),
            },
            (None, False),
        ),
    ],
)
def test_readiness(revision: dict[str, object], seen: tuple[bool | None, bool]) -> None:
    assert _readiness(revision) == seen

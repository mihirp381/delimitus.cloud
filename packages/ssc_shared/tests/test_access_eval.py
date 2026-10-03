"""The one access evaluator (SSC-021, decision 019), on hand-built ``ssc-snapshot/v1`` documents."""

import json
from typing import Any

import pytest

from ssc_contracts.snapshot import FORMAT_V1, SnapshotDoc
from ssc_shared.access import AccessView, SnapshotInvalidError, ViewHolder, decide
from ssc_shared.canonical import canonical_bytes
from ssc_shared.runtime import REQUEST_TIMEOUT_SECONDS, SESSION_TIMEOUT_SECONDS

ORG = "org_" + "a" * 20
OTHER_ORG = "org_" + "b" * 20
APP = "app_" + "a" * 20
PROD = "env_" + "p" * 20
PREVIEW = "env_" + "v" * 20
ADA, BEN, CY, DEE = ("usr_" + c * 20 for c in "abcd")
FIN = "grp_" + "f" * 20


def gnt(n: int) -> str:
    return f"gnt_{n:020d}"


def doc(version: int = 1, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "format": FORMAT_V1,
        "org_id": ORG,
        "version": version,
        "compiled_at": "2026-09-29T12:00:00Z",
        "environments": {
            PROD: {"app_id": APP, "name": "prod", "status": "active", "floor": "user"},
            PREVIEW: {"app_id": APP, "name": "preview", "status": "active", "floor": "builder"},
        },
        "hosts": {},
        "grants": {
            PROD: [
                {"grant_id": gnt(3), "role": "user", "subject_kind": "org", "subject_id": None},
                {"grant_id": gnt(2), "role": "builder", "subject_kind": "group", "subject_id": FIN},
            ],
            PREVIEW: [
                {"grant_id": gnt(4), "role": "user", "subject_kind": "user", "subject_id": BEN},
                {"grant_id": gnt(1), "role": "builder", "subject_kind": "user", "subject_id": ADA},
            ],
        },
        "groups_by_user": {ADA: [FIN]},
        "users": {
            ADA: {"status": "active"},
            BEN: {"status": "active"},
            CY: {"status": "deactivated"},
            DEE: {"status": "active"},
        },
        "ceiling": None,
    }
    base.update(changes)
    return base


def view(**changes: Any) -> AccessView:
    return AccessView.from_document(doc(**changes))


def test_no_view_refuses_everyone() -> None:
    d = decide(None, PROD, ADA)
    assert (d.allowed, d.role, d.via, d.reason) == (False, None, (), "no_view")


def test_refusals_come_before_grants() -> None:
    v = view()
    assert decide(v, "env_" + "x" * 20, ADA).reason == "unknown_environment"
    assert decide(v, PROD, CY).reason == "user_not_active"
    assert decide(v, PROD, "usr_" + "z" * 20).reason == "user_not_active"
    assert decide(v, "not an id", "nor this").reason == "unknown_environment"
    envs = doc()["environments"]
    for status in ("disabled", "quarantined"):
        stopped = {**envs, PROD: {**envs[PROD], "status": status}}
        assert decide(view(environments=stopped), PROD, ADA).reason == "app_not_active"


def test_the_best_counted_role_wins_and_every_counted_grant_is_named() -> None:
    d = decide(view(), PROD, ADA)
    assert (d.allowed, d.role, d.reason) == (True, "builder", "granted")
    assert [g.grant_id for g in d.via] == [gnt(2), gnt(3)]
    org_only = decide(view(), PROD, DEE)
    assert (org_only.role, [g.subject_kind for g in org_only.via]) == ("user", ["org"])


def test_a_grant_below_the_floor_counts_for_nothing() -> None:
    d = decide(view(), PREVIEW, BEN)
    assert (d.allowed, d.role, d.reason) == (False, None, "below_floor")
    assert [g.grant_id for g in d.via] == [gnt(4)]
    assert decide(view(), PREVIEW, DEE).reason == "no_grant"
    assert decide(view(), PREVIEW, ADA).allowed is True


def test_an_environment_with_no_grants_refuses() -> None:
    grants = {PROD: [], PREVIEW: []}
    assert decide(view(grants=grants), PROD, ADA).reason == "no_grant"
    assert decide(view(grants={}), PROD, ADA).reason == "no_grant"


@pytest.mark.parametrize(
    "changes",
    [
        {"format": "ssc-snapshot/v2"},
        {"org_id": "acme"},
        {"version": -1},
        {"version": 2**53},
        {"compiled_at": "2026-09-29T12:00:00"},
        {"ceiling": {"max": "org"}},
        {"extra": 1},
        {"grants": {"env_" + "x" * 20: []}},
        {"hosts": {"ledger": "env_" + "x" * 20}},
        {"groups_by_user": {"usr_" + "z" * 20: [FIN]}},
        {
            "grants": {
                PROD: [
                    {
                        "grant_id": gnt(9),
                        "role": "user",
                        "subject_kind": "user",
                        "subject_id": "usr_" + "z" * 20,
                    }
                ]
            }
        },
        {
            "grants": {
                PROD: [
                    {"grant_id": gnt(9), "role": "user", "subject_kind": "org", "subject_id": ADA}
                ]
            }
        },
        {
            "grants": {
                PROD: [
                    {"grant_id": gnt(9), "role": "user", "subject_kind": "group", "subject_id": ADA}
                ]
            }
        },
        {
            "grants": {
                PROD: [
                    {"grant_id": gnt(9), "role": "admin", "subject_kind": "org", "subject_id": None}
                ]
            }
        },
    ],
)
def test_an_invalid_document_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(SnapshotInvalidError):
        AccessView.from_document(doc(**changes))


def test_documents_arrive_as_json_bytes_text_or_models() -> None:
    raw = json.dumps(doc())
    for form in (raw, raw.encode(), SnapshotDoc.model_validate_json(raw), doc()):
        assert decide(AccessView.from_document(form), PROD, ADA).allowed is True
    for junk in (b"", b"{", b"[]", "null", b"\xff\xfe", "[" * 10000):
        with pytest.raises(SnapshotInvalidError):
            AccessView.from_document(junk)


def test_the_holder_swaps_only_to_a_valid_newer_document_of_its_org() -> None:
    holder = ViewHolder(ORG)
    assert holder.view is None
    assert holder.apply(json.dumps(doc(1)).encode()) is True
    first = holder.view
    grants = {PROD: [], PREVIEW: []}
    for bad in (
        json.dumps(doc(2)).encode()[:-3],
        doc(2, grants={"env_" + "x" * 20: []}),
        doc(2, org_id=OTHER_ORG),
        doc(0),
    ):
        with pytest.raises(SnapshotInvalidError):
            holder.apply(bad)
        assert holder.view is first
    assert holder.apply(doc(1, grants=grants)) is False
    assert holder.view is first
    assert holder.apply(doc(2, grants=grants)) is True
    assert holder.view is not None
    assert holder.view.version == 2
    assert decide(holder.view, PROD, ADA).reason == "no_grant"
    assert holder.apply(doc(1)) is False


def test_an_environment_timeout_is_optional_and_missing_means_the_lower_figure() -> None:
    plain = doc()
    assert view().environments[PROD].timeout_seconds == REQUEST_TIMEOUT_SECONDS
    parsed = SnapshotDoc.model_validate(plain)
    assert canonical_bytes(parsed.model_dump(mode="json")) == canonical_bytes(plain)
    envs = {**plain["environments"]}
    envs[PROD] = {**envs[PROD], "timeout_seconds": SESSION_TIMEOUT_SECONDS}
    session = AccessView.from_document(doc(environments=envs))
    assert session.environments[PROD].timeout_seconds == SESSION_TIMEOUT_SECONDS
    assert session.environments[PREVIEW].timeout_seconds == REQUEST_TIMEOUT_SECONDS
    assert decide(session, PROD, ADA) == decide(view(), PROD, ADA)


@pytest.mark.parametrize("seconds", [0, -300, 1.5, "soon"])
def test_a_bad_environment_timeout_is_refused(seconds: object) -> None:
    envs = {**doc()["environments"]}
    envs[PROD] = {**envs[PROD], "timeout_seconds": seconds}
    with pytest.raises(SnapshotInvalidError):
        AccessView.from_document(json.dumps(doc(environments=envs)))


def test_the_view_is_read_only() -> None:
    v = view()
    with pytest.raises(TypeError):
        v.environments[PROD] = v.environments[PREVIEW]  # type: ignore[index]
    with pytest.raises(AttributeError):
        v.extra = 1  # type: ignore[attr-defined]
    assert v.floor_of(PREVIEW) == "builder"
    assert v.floor_of("env_" + "x" * 20) is None

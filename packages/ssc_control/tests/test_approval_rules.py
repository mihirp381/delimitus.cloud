"""SSC-045: the typed approval rules, without a database."""

from __future__ import annotations

import pytest

from ssc_control.domain.approval_rules import (
    PROFILES,
    GrantKey,
    NoRuleError,
    RequestedCapabilities,
    Requirement,
    RequirementKind,
    agent_share_needs_approval,
    agent_share_subject_key,
    check_decider,
    gate_outcome,
    required_for_deploy,
    share_subject_key,
    widening_needs_approval,
    widens,
)

ADA: GrantKey = ("builder", "user", "usr_ada")
BOB: GrantKey = ("user", "user", "usr_bob")
BOB_BUILDER: GrantKey = ("builder", "user", "usr_bob")
FINANCE: GrantKey = ("user", "group", "grp_finance")
ORG_USER: GrantKey = ("user", "org", None)
ORG_BUILDER: GrantKey = ("builder", "org", None)
CONNECT = RequirementKind.CONNECT_DATA_SOURCE
HOSTS = RequirementKind.ENABLE_INTERNET_HOSTS


def test_profiles_are_exactly_internal() -> None:
    assert frozenset({"internal"}) == PROFILES


@pytest.mark.parametrize(
    ("before", "after", "widened"),
    [
        pytest.param({ADA}, {ADA}, False, id="no change"),
        pytest.param({ADA, BOB}, {ADA}, False, id="removing someone"),
        pytest.param({ADA}, {ADA, BOB}, True, id="a new user"),
        pytest.param({ADA}, {ADA, FINANCE}, True, id="a new group"),
        pytest.param({ADA}, {ADA, ORG_USER}, True, id="the whole org"),
        pytest.param({ORG_USER}, {ORG_USER, ORG_BUILDER}, True, id="an added org grant"),
        pytest.param({ADA, BOB}, {ADA, BOB_BUILDER}, False, id="a role change, same people"),
        pytest.param({ORG_USER, FINANCE}, {ORG_USER}, False, id="narrowing"),
        pytest.param(set[GrantKey](), set[GrantKey](), False, id="nothing to nothing"),
    ],
)
def test_widening_truth_table(before: set[GrantKey], after: set[GrantKey], widened: bool) -> None:
    assert widens(before, after) is widened
    need = widening_needs_approval("internal", True, before, after)
    assert (need is not None) is widened
    if need is not None:
        assert need == Requirement(RequirementKind.WIDEN_AUDIENCE, share_subject_key(after))
    # Not data-connected: never needs an approval, however wide.
    assert widening_needs_approval("internal", False, before, after) is None


def test_unknown_profile_has_no_rule() -> None:
    with pytest.raises(NoRuleError) as e:
        widening_needs_approval("public", False, {ADA}, {ADA})
    assert e.value.reason == "unknown_profile"
    with pytest.raises(NoRuleError) as e:
        required_for_deploy("public", RequestedCapabilities())
    assert e.value.reason == "unknown_profile"


def test_deploy_needs_one_approval_per_connection_and_host() -> None:
    caps = RequestedCapabilities(
        connections=frozenset({"finance", "hr"}), egress_hosts=frozenset({"api.example.com"})
    )
    assert required_for_deploy("internal", caps) == {
        Requirement(CONNECT, "finance"),
        Requirement(CONNECT, "hr"),
        Requirement(HOSTS, "api.example.com"),
    }
    assert required_for_deploy("internal", RequestedCapabilities()) == frozenset()


def test_an_unknown_capability_has_no_rule() -> None:
    with pytest.raises(NoRuleError) as e:
        required_for_deploy("internal", RequestedCapabilities(unknown=frozenset({"smtp"})))
    assert e.value.reason == "unknown_capability"


def test_share_keys_ignore_order_and_bind_the_version() -> None:
    assert share_subject_key([ADA, FINANCE]) == share_subject_key([FINANCE, ADA])
    assert share_subject_key([ADA]) != share_subject_key([ADA, FINANCE])
    assert share_subject_key([ADA]).startswith("sha256:")
    assert len(share_subject_key([ADA])) == len("sha256:") + 64
    assert agent_share_subject_key(3, [ADA, BOB]) == agent_share_subject_key(3, [BOB, ADA])
    assert agent_share_subject_key(3, [ADA]) != agent_share_subject_key(4, [ADA])
    assert agent_share_subject_key(3, [ADA]) != share_subject_key([ADA])
    # The org grant's missing subject id sorts and digests without error.
    assert share_subject_key([ORG_USER, ADA]) == share_subject_key([ADA, ORG_USER])


def test_every_agent_change_needs_approval_and_a_person_s_does_not() -> None:
    need = agent_share_needs_approval(True, 7, {ADA, BOB}, {ADA})
    assert need == Requirement(RequirementKind.AGENT_SHARE, agent_share_subject_key(7, {ADA}))
    assert agent_share_needs_approval(True, 7, {ADA}, {ADA}) is None  # no change
    assert agent_share_needs_approval(False, 7, {ADA}, {ADA, ORG_USER}) is None


@pytest.mark.parametrize(
    ("decider", "role", "active", "via_agent", "refusal"),
    [
        pytest.param("usr_b", "admin", True, False, None, id="another active admin"),
        pytest.param("usr_a", "admin", True, True, "agent_session", id="agent first"),
        pytest.param("usr_b", None, False, True, "agent_session", id="agent before eligibility"),
        pytest.param("usr_a", "admin", True, False, "self_approval", id="self"),
        pytest.param(
            "usr_a", "member", False, False, "self_approval", id="self before eligibility"
        ),
        pytest.param("usr_b", "member", True, False, "not_eligible", id="a member"),
        pytest.param("usr_b", "admin", False, False, "not_eligible", id="a deactivated admin"),
        pytest.param("usr_b", None, False, False, "not_eligible", id="nobody in this org"),
    ],
)
def test_check_decider_order(
    decider: str, role: str | None, active: bool, via_agent: bool, refusal: str | None
) -> None:
    assert check_decider("usr_a", decider, role, active, via_agent) == refusal


def test_gate_outcome() -> None:
    a, b = Requirement(CONNECT, "finance"), Requirement(HOSTS, "api.example.com")
    assert gate_outcome({}) == "clear"
    assert gate_outcome({a: "approved", b: "approved"}) == "clear"
    assert gate_outcome({a: "approved", b: "pending"}) == "waiting"
    assert gate_outcome({a: "approved", b: None}) == "waiting"
    assert gate_outcome({a: "cancelled"}) == "waiting"
    assert gate_outcome({a: "pending", b: "denied"}) == "refused"
    assert gate_outcome({a: "approved", b: "revoked"}) == "refused"

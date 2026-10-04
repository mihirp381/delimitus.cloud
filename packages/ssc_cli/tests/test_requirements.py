"""SSC-093: the platform requirements, ``ssc doctor`` and the build come from one source."""

from collections import Counter
from typing import get_args

from ssc_cli.doctor import run_doctor
from ssc_cli.doctor.finding import FIX, DoctorCode
from ssc_contracts.build import FIX_ITS, NOTICES
from ssc_contracts.manifest import RESOURCE_CLASSES
from ssc_contracts.packages import APPROVED_PACKAGES
from ssc_shared.requirements import FACTS, REQUIREMENT_OF, RULES, platform_requirements


def test_requirements_and_doctor_come_from_one_source() -> None:
    items = (*RULES, *FACTS)
    doctor = Counter(code for item in items for code in item.doctor)
    assert set(doctor) == set(get_args(DoctorCode)) == set(FIX)
    assert max(doctor.values()) == 1
    assert {code for item in items for code in item.build} == set(FIX_ITS) | set(NOTICES)
    assert set(REQUIREMENT_OF.values()) <= {item.id for item in items}
    assert len({item.id for item in items}) == len(items)


def test_each_finding_names_the_rule_it_checks(tmp_path) -> None:
    (tmp_path / "ssc.toml").write_text('schema = "ssc/v1"\n')
    (tmp_path / "requirements.txt").write_text("flask\npytesseract\n")
    (tmp_path / "app.py").write_text("app.run(host='127.0.0.1', port=5000)\n")
    found = {(f.code, f.requirement) for f in run_doctor(tmp_path)}
    assert {("ADD_APPROVED_PACKAGE", "system-packages"), ("PORT_BINDING", "port")} <= found


def test_the_requirements_carry_the_ticket_rules_and_facts() -> None:
    req = platform_requirements()
    ids = [r.id for r in req.rules]
    for rule in (
        "port",
        "health",
        "non-root",
        "memory-only",
        "postgres",
        "timers",
        "egress",
        "no-dockerfile",
        "web-app",
        "system-packages",
    ):
        assert rule in ids
    facts = {f.id: f.text for f in req.facts}
    assert "first request is slow" in facts["cold-start"]
    assert "one instance" in facts["session"]
    assert "60 minutes" in facts["session"]
    assert (
        [s.name for s in req.resource_classes]
        == list(RESOURCE_CLASSES)
        == [
            "small",
            "medium",
            "large",
        ]
    )
    assert set(req.approved_packages) == APPROVED_PACKAGES
    assert "preflight" in req.next

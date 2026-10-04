"""``ssc requirements``: the platform's runtime rules, from the source ``ssc doctor`` and the
``get_platform_requirements`` agent tool read (``ssc_shared.requirements``). Offline."""

from ssc_cli.commands._common import JsonOpt
from ssc_cli.output import print_json, say, table
from ssc_shared.requirements import platform_requirements


def requirements(json_mode: JsonOpt = False) -> None:
    """Show the rules every app must follow to run on SSC, and how it runs once deployed."""
    req = platform_requirements()
    if json_mode:
        print_json(req)
        return
    say("Rules:")
    for rule in req.rules:
        say(f"- {rule.text}")
    say("\nHow an app runs:")
    for fact in req.facts:
        say(f"- {fact.text}")
    say()
    say(
        table(
            ("CLASS", "VCPU", "MEMORY", "MAX INSTANCES"),
            [
                (s.name, str(s.vcpu), f"{s.memory_mib} MiB", str(s.max_instances))
                for s in req.resource_classes
            ],
        )
    )
    say(f"\nSystem packages a build may install: {', '.join(req.approved_packages)}.")
    say(req.how_to_ask_for_a_package)
    say(f"\n{req.next}")

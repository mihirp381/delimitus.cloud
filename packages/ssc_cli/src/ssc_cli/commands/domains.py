"""``ssc domains``: the org's own address for its apps (``/v1/domain``). An org admin asks for a
subdomain, has IT add its NS records, then moves every app to it. An agent may read it, never
change it."""

from collections.abc import Callable
from typing import Annotated, Final

import typer

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.commands._common import JsonOpt, handled, session
from ssc_cli.errors import WAIT_TIMED_OUT, CliError, ErrorBody, ExitCode
from ssc_cli.models import DomainOut
from ssc_cli.output import dash, print_json, say, table
from ssc_cli.shapes import DomainAppRow, DomainPreviousRow, DomainResult
from ssc_cli.wait import Budget
from ssc_contracts.errors import ErrorCode

WAIT_SECONDS: Final = 72 * 3600
POLL_SECONDS: Final = 30.0
ZONE_SECONDS: Final = 180
ZONE_POLL_SECONDS: Final = 5.0
ENDED: Final = frozenset({"active", "failed"})
DOMAIN_FAILED: Final = "DOMAIN_FAILED"
ADMISSION_RULES: Final = (
    "Use a name at least three labels long under a domain your company owns, such as "
    "tools.example.com, that no other organisation uses."
)
FIXES: Final = {
    ErrorCode.DOMAIN_NOT_READY.value: "Some apps are still being prepared for the new address. "
    "Run `ssc domains status` until ready_to_flip is yes, then run the same command again.",
    ErrorCode.DOMAIN_STATE.value: "The address is not where this step needs it. Run "
    "`ssc domains show` to see its state.",
    ErrorCode.VALIDATION_FAILED.value: ADMISSION_RULES,
}

domains_app = typer.Typer(
    name="domains",
    help="Serve the org's apps under your company's own domain.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)

SuffixArg = Annotated[
    str, typer.Argument(metavar="SUFFIX", help="The subdomain to delegate, like tools.acme.com.")
]
WaitOpt = Annotated[bool, typer.Option("--wait", help="Wait until it is active or failed.")]
TimeoutOpt = Annotated[
    int, typer.Option("--timeout", min=1, metavar="SECONDS", help="Give up waiting after this.")
]
BackOpt = Annotated[bool, typer.Option("--back", help="Move the apps back to the default suffix.")]
ConfirmOpt = Annotated[
    bool, typer.Option("--confirm", help="Required: every app's address changes.")
]


def zone_of(suffix: str) -> str:
    return suffix.split(".", 1)[1] if "." in suffix else suffix


def record_lines(suffix: str, name_servers: list[str]) -> list[str]:
    return [f"{suffix} NS {ns}" for ns in name_servers]


def it_ticket(suffix: str, name_servers: list[str]) -> str:
    zone = zone_of(suffix)
    return "\n".join(
        [
            f"Please add these NS records in the DNS zone {zone}:",
            "",
            *record_lines(suffix, name_servers),
            "",
            f"Why: this hands {suffix} to Delimitus, which serves our internal apps under it and "
            "issues their certificates.",
            f"Nothing else changes: no other record in {zone} is touched.",
        ]
    )


def result_of(api_url: str, d: DomainOut) -> DomainResult:
    g = d.delegation
    names = list(g.name_servers) if g else []
    seen = sorted({ns for answer in (g.observed_ns or {}).values() for ns in answer}) if g else []
    return DomainResult(
        api_url=api_url,
        default_suffix=d.default_suffix,
        current_suffix=d.current_suffix,
        moved=d.current_suffix != d.default_suffix,
        previous=[DomainPreviousRow(suffix=p.suffix, until=p.until) for p in d.previous],
        suffix=g.suffix if g else None,
        state=g.state if g else None,
        zone=zone_of(g.suffix) if g else None,
        name_servers=names,
        records=record_lines(g.suffix, names) if g else [],
        observed=seen,
        failure=g.failure if g else None,
        provider=g.provider if g else None,
        apps_that_move=[DomainAppRow(slug=a.slug, hosts=list(a.hosts)) for a in d.apps_that_move],
        ready_to_flip=d.ready_to_flip,
        rolling=d.rolling,
        it_ticket=it_ticket(g.suffix, names) if g else None,
    )


def _explained[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except CliError as e:
        e.fix = e.fix or FIXES.get(e.body.code)
        raise


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _say_records(r: DomainResult) -> None:
    say(f"NS records to add in {r.zone}:")
    for line in r.records:
        say(f"  {line}")


def _say_observed(r: DomainResult) -> None:
    seen = ", ".join(r.observed) if r.observed else "nothing yet"
    say(f"What we see from the internet right now: {seen}")


def _say_moves(r: DomainResult) -> None:
    rows = [(a.slug, *(a.hosts + ["", ""])[:2]) for a in r.apps_that_move]
    say(table(("APP", "PRODUCTION", "PREVIEW"), rows) if rows else "No apps to move.")


@domains_app.command("show")
def show(ctx: typer.Context, json_mode: JsonOpt = False) -> None:
    """Show where the apps live, the own address's state, its records and the apps that move."""
    with handled(json_mode), session(ctx).client() as client:
        r = result_of(client.api_url, client.get_domain())
    if json_mode:
        print_json(r)
        return
    say(f"Apps live under: {r.current_suffix}")
    for p in r.previous:
        say(f"Old addresses under {p.suffix} redirect until {p.until}")
    if r.state is None:
        say("No own address yet. Ask for one with `ssc domains add <suffix>`.")
        return
    say(f"Own address: {r.suffix} ({r.state})")
    if r.failure:
        say(("Certificate authority says: " if r.state == "issuing" else "Reason: ") + r.failure)
    if r.state == "pending_dns":
        _say_records(r)
        _say_observed(r)
    if not r.moved:
        if r.rolling:
            say(f"{r.rolling} apps are still being prepared.")
        say("Apps that move:")
        _say_moves(r)


@domains_app.command("add")
def add(ctx: typer.Context, suffix: SuffixArg, json_mode: JsonOpt = False) -> None:
    """Ask for an own address; prints the NS records and a ticket for your IT team."""
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        d = _explained(lambda: client.request_domain(suffix))
        r = result_of(client.api_url, _zone_made(client, s.sleep, d))
    if json_mode:
        print_json(r)
        return
    say(f"Asked for {r.suffix}. One DNS change by your IT team; we do the rest.")
    if not r.name_servers:
        say(
            "The cell is still making its zone. Run `ssc domains show` in a minute for the NS "
            "records and the ticket for your IT team."
        )
        return
    _say_records(r)
    say("\nTicket for your IT team:\n")
    say(r.it_ticket or "")
    say("\nFollow it with `ssc domains status --wait`.")


def _zone_made(client: ApiClient, sleep: Sleep, d: DomainOut) -> DomainOut:
    """The answer once the cell has made the zone and named its servers, or after a few minutes."""
    budget = Budget(ZONE_SECONDS)
    while (
        d.delegation is not None
        and d.delegation.state == "pending_dns"
        and not d.delegation.name_servers
        and not budget.used_up
    ):
        budget.wait(sleep, ZONE_POLL_SECONDS)
        d = client.get_domain()
    return d


def _wait(client: ApiClient, sleep: Sleep, seconds: int) -> DomainOut:
    budget = Budget(seconds)
    while True:
        d = client.get_domain()
        if d.delegation is None or d.delegation.state in ENDED:
            return d
        if budget.used_up:
            body = ErrorBody(
                code=WAIT_TIMED_OUT,
                title="Stopped waiting for the own address.",
                detail=f"It was still {d.delegation.state} after {seconds:g} seconds and carries "
                "on without ssc. Follow it with `ssc domains status`.",
                instance="/v1/domain",
            )
            raise CliError(body, ExitCode.FAILED)
        budget.wait(sleep, POLL_SECONDS)


@domains_app.command("status")
def status(
    ctx: typer.Context,
    wait: WaitOpt = False,
    timeout: TimeoutOpt = WAIT_SECONDS,
    json_mode: JsonOpt = False,
) -> None:
    """One line per field of the own address; --wait polls until it is active or failed."""
    s = session(ctx)
    with handled(json_mode), s.client() as client:
        d = _wait(client, s.sleep, timeout) if wait else client.get_domain()
        r = result_of(client.api_url, d)
        if wait and r.state == "failed":
            body = ErrorBody(
                code=DOMAIN_FAILED,
                title="The own address failed.",
                detail=f"{r.suffix}: {r.failure or 'no reason given'}",
                instance="/v1/domain",
            )
            raise CliError(body, ExitCode.FAILED, fix=f"Run `ssc domains add {r.suffix}` again.")
    if json_mode:
        print_json(r)
        return
    for name, value in (
        ("state", dash(r.state)),
        ("suffix", dash(r.suffix)),
        ("current_suffix", r.current_suffix),
        ("default_suffix", r.default_suffix),
        ("observed", ", ".join(r.observed) or "-"),
        ("failure", dash(r.failure)),
        ("rolling", str(r.rolling)),
        ("ready_to_flip", _yes(r.ready_to_flip)),
        ("moved", _yes(r.moved)),
    ):
        say(f"{name}: {value}")


@domains_app.command("flip")
def flip(
    ctx: typer.Context,
    back: BackOpt = False,
    confirm: ConfirmOpt = False,
    json_mode: JsonOpt = False,
) -> None:
    """Move every app to the own address, or back to the default suffix with --back."""
    if not confirm:
        raise typer.BadParameter(
            "every app's address changes; add --confirm.", param_hint="--confirm"
        )
    to = "default" if back else "customer"
    with handled(json_mode), session(ctx).client() as client:
        r = result_of(client.api_url, _explained(lambda: client.flip_domain(to)))
    if json_mode:
        print_json(r)
        return
    say(f"Your apps now live under {r.current_suffix}.")
    for p in r.previous:
        say(f"Old addresses under {p.suffix} redirect until {p.until}")

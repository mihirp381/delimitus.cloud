"""The worker's mail tasks (SSC-049). Registered by ``worker.build_app`` under the ``notify``
namespace.

``send`` is deferred with the request that needs it and sends that org's due mail, so a request
reaches an approver's inbox within moments. ``tick`` runs every minute for every org: the
three-day reminders, the retries of mail that failed, and the pruning of old rows. ``digest``
runs daily at 07:00 UTC. With no mailer in the ports they do nothing and the rows wait.

``blueprint()`` builds fresh tasks on each call: ``App.add_tasks_from`` renames what it copies.
"""

import logging
from datetime import UTC, datetime
from typing import Final

from procrastinate import Blueprint, JobContext

from ssc_control.db.orgs import all_org_ids
from ssc_control.notifications import delivery
from ssc_control.worker_ports import ports_of

log = logging.getLogger(__name__)

TICK_CRON: Final = "* * * * *"
DIGEST_CRON: Final = "0 7 * * *"


def blueprint(*, tick_cron: str = TICK_CRON, digest_cron: str = DIGEST_CRON) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="send", pass_context=True)
    async def send(context: JobContext, org_id: str) -> int:  # pyright: ignore[reportUnusedFunction]
        """Send one org's due mail; returns how many went out."""
        ports = ports_of(context)
        if ports.mailer is None:
            log.info("mail not sent: no mailer configured", extra={"org_id": org_id})
            return 0
        return await delivery.flush(
            ports.engine, ports.mailer, org_id=org_id, console_url=ports.console_url
        )

    @bp.periodic(cron=tick_cron, periodic_id="notify_tick", queueing_lock="notify_tick")
    @bp.task(name="tick", pass_context=True)
    async def tick(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Remind, send what is due and prune, for every org; returns how many were sent."""
        del timestamp
        ports = ports_of(context)
        sent = 0
        for org_id in await all_org_ids(ports.engine):
            try:
                await delivery.remind(ports.engine, org_id=org_id)
                if ports.mailer is not None:
                    sent += await delivery.flush(
                        ports.engine, ports.mailer, org_id=org_id, console_url=ports.console_url
                    )
                await delivery.prune(ports.engine, org_id=org_id)
            except Exception:
                log.exception("notification tick of %s raised", org_id)
        return sent

    @bp.periodic(cron=digest_cron, periodic_id="notify_digest", queueing_lock="notify_digest")
    @bp.task(name="digest", pass_context=True)
    async def digest(context: JobContext, timestamp: int) -> int:  # pyright: ignore[reportUnusedFunction]
        """Queue the day's digest for every org; returns how many mails were queued."""
        ports = ports_of(context)
        day = datetime.fromtimestamp(timestamp, UTC).date()
        queued = 0
        for org_id in await all_org_ids(ports.engine):
            try:
                queued += await delivery.digest(ports.engine, org_id=org_id, day=day)
            except Exception:
                log.exception("notification digest of %s raised", org_id)
        return queued

    return bp

"""Send stage.  Every guard lives in ``messaging.compliance``; this only calls it."""

from __future__ import annotations

from sqlalchemy.orm import Session

from outreach.logging import get_logger
from outreach.messaging.gmail import GmailClient
from outreach.messaging.queue import process_queue
from outreach.pipeline.context import RunContext, StageResult

log = get_logger(__name__)


def run_send(
    session: Session, ctx: RunContext, *, client: GmailClient | None = None
) -> StageResult:
    """Attempt to send queued messages, obeying every guard."""
    settings = ctx.settings

    if ctx.dry_run:
        # Absolute: a dry run never reaches the transport at all.
        stage = StageResult.skipped(
            "send", "DRY RUN: no email may be sent, regardless of feature flags"
        )
        result = process_queue(session, settings, dry_run=True, client=None, limit=ctx.limit)
        stage.counts = {
            "considered": result.considered,
            "would_send": 0,
            "held": result.skipped,
            "deferred": result.deferred,
        }
        stage.items = result.details
        return stage

    disabled = ctx.require_feature("send", "FEATURE_GMAIL_SEND")
    if disabled is not None:
        result = process_queue(session, settings, dry_run=False, client=None, limit=ctx.limit)
        disabled.counts = {"considered": result.considered, "held": result.skipped}
        disabled.items = result.details
        return disabled

    if not settings.outreach_send_enabled:
        stage = StageResult.skipped("send", "OUTREACH_SEND_ENABLED is false")
        result = process_queue(session, settings, dry_run=False, client=None, limit=ctx.limit)
        stage.counts = {"considered": result.considered, "held": result.skipped}
        return stage

    stage = StageResult(name="send")
    result = process_queue(session, settings, dry_run=False, client=client, limit=ctx.limit)
    stage.counts = {
        "considered": result.considered,
        "sent": result.sent,
        "skipped": result.skipped,
        "failed": result.failed,
        "needs_review": result.needs_review,
        "deferred": result.deferred,
    }
    stage.items = result.details
    session.commit()
    return stage

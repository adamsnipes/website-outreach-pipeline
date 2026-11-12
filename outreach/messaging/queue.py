"""Persistent send queue in SQLite.

Every transition is recorded and recoverable.  Reruns can never duplicate a
send because the schema allows exactly one ``outreach_message`` row per
business and because the state machine treats anything past ``SENDING`` as
terminal for outbound purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.db import utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.messaging.compliance import (
    SendBlockReason,
    SendDecision,
    evaluate_send,
    next_window_open,
)
from outreach.messaging.gmail import GmailClient, GmailError, SendReceipt, new_idempotency_key
from outreach.models.enums import TERMINAL_SENT_STATUSES, EventType, MessageStatus
from outreach.models.outreach_message import OutreachMessage

log = get_logger(__name__)

#: Transitions the queue is allowed to make.  Anything else is a bug.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    MessageStatus.DRAFT: {MessageStatus.QUEUED, MessageStatus.SCHEDULED, MessageStatus.SKIPPED, MessageStatus.FAILED},
    MessageStatus.QUEUED: {MessageStatus.SCHEDULED, MessageStatus.SENDING, MessageStatus.SKIPPED, MessageStatus.FAILED},
    MessageStatus.SCHEDULED: {MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SKIPPED, MessageStatus.FAILED},
    MessageStatus.SENDING: {MessageStatus.SENT, MessageStatus.FAILED, MessageStatus.NEEDS_REVIEW},
    MessageStatus.SENT: {
        MessageStatus.REPLIED, MessageStatus.POSITIVE_REPLY, MessageStatus.NEGATIVE_REPLY,
        MessageStatus.OPTED_OUT, MessageStatus.BOUNCED,
    },
    MessageStatus.REPLIED: {
        MessageStatus.POSITIVE_REPLY, MessageStatus.NEGATIVE_REPLY, MessageStatus.OPTED_OUT,
    },
    MessageStatus.FAILED: {MessageStatus.QUEUED, MessageStatus.SKIPPED},
    MessageStatus.NEEDS_REVIEW: {MessageStatus.SENT, MessageStatus.SKIPPED, MessageStatus.FAILED},
    MessageStatus.SKIPPED: {MessageStatus.QUEUED, MessageStatus.DRAFT},
}


class InvalidTransition(RuntimeError):
    """Raised when code tries to move a message somewhere it must not go."""


@dataclass
class QueueOutcome:
    message: OutreachMessage | None
    created: bool = False
    action: str = ""
    reason: str = ""


@dataclass
class ProcessResult:
    """Summary of one ``process-queue`` run."""

    considered: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    needs_review: int = 0
    deferred: int = 0
    dry_run: bool = False
    details: list[dict[str, Any]] = field(default_factory=list)

    def note(self, message_id: int | None, action: str, reason: str) -> None:
        self.details.append({"message_id": message_id, "action": action, "reason": reason})


def transition(
    session: Session,
    message: OutreachMessage,
    new_status: str,
    *,
    reason: str = "",
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
    strict: bool = True,
) -> OutreachMessage:
    """Move a message to a new status, recording the transition."""
    current = str(message.status)
    if current == str(new_status):
        return message
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if strict and str(new_status) not in {str(a) for a in allowed}:
        raise InvalidTransition(
            f"message {message.id} cannot move {current} -> {new_status} "
            f"(allowed: {sorted(str(a) for a in allowed)})"
        )
    message.status = str(new_status)
    message.updated_at = utcnow()
    record_event(
        session,
        "outreach_message",
        message.id,
        event_type or EventType.MESSAGE_QUEUED,
        {"from": current, "to": str(new_status), "reason": reason, **(payload or {})},
    )
    log.info(
        "message transition",
        extra={"message_id": message.id, "from": current, "to": str(new_status), "reason": reason},
    )
    return message


def get_message_for_business(session: Session, business_id: int) -> OutreachMessage | None:
    return session.scalars(
        select(OutreachMessage).where(OutreachMessage.business_id == business_id)
    ).first()


def upsert_draft(
    session: Session,
    *,
    business_id: int,
    build_id: int | None,
    recipient_email: str,
    subject: str,
    body_text: str,
    body_html: str | None,
    composition_method: str,
    observations: list[dict[str, Any]] | None = None,
) -> QueueOutcome:
    """Create or refresh the single draft message for a business.

    Never creates a second row: if a message already exists and has left the
    drafting stage, it is returned untouched.
    """
    existing = get_message_for_business(session, business_id)
    if existing is not None:
        if str(existing.status) in {str(s) for s in TERMINAL_SENT_STATUSES}:
            return QueueOutcome(
                existing, False, "unchanged",
                f"business already has a message in state {existing.status}",
            )
        if str(existing.status) in {MessageStatus.DRAFT, MessageStatus.SKIPPED, MessageStatus.FAILED}:
            existing.recipient_email = recipient_email
            existing.subject = subject
            existing.body_text = body_text
            existing.body_html = body_html
            existing.composition_method = composition_method
            existing.observations_json = observations or None
            existing.build_id = build_id or existing.build_id
            existing.updated_at = utcnow()
            return QueueOutcome(existing, False, "updated", "refreshed the existing draft")
        return QueueOutcome(
            existing, False, "unchanged", f"message already {existing.status}"
        )

    message = OutreachMessage(
        business_id=business_id,
        build_id=build_id,
        recipient_email=recipient_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        status=MessageStatus.DRAFT,
        composition_method=composition_method,
        observations_json=observations or None,
        idempotency_key=new_idempotency_key(),
    )
    session.add(message)
    try:
        session.flush()
    except IntegrityError:
        # Another process created it first: adopt theirs, never insert a second.
        session.rollback()
        existing = get_message_for_business(session, business_id)
        return QueueOutcome(existing, False, "raced", "another process created the message first")

    record_event(
        session,
        "outreach_message",
        message.id,
        EventType.MESSAGE_COMPOSED,
        {"business_id": business_id, "method": composition_method},
    )
    return QueueOutcome(message, True, "created", "draft created")


def enqueue(
    session: Session,
    message: OutreachMessage,
    *,
    scheduled_at: datetime | None = None,
) -> OutreachMessage:
    """Move a draft into the send queue."""
    if scheduled_at is not None:
        message.scheduled_at = scheduled_at
        return transition(
            session, message, MessageStatus.SCHEDULED,
            reason=f"scheduled for {scheduled_at.isoformat()}",
            event_type=EventType.MESSAGE_SCHEDULED,
        )
    return transition(
        session, message, MessageStatus.QUEUED,
        reason="ready to send", event_type=EventType.MESSAGE_QUEUED,
    )


def pending_messages(session: Session, limit: int = 50) -> list[OutreachMessage]:
    """Queued/scheduled messages in the order they should be attempted."""
    return list(
        session.scalars(
            select(OutreachMessage)
            .where(OutreachMessage.status.in_([MessageStatus.QUEUED, MessageStatus.SCHEDULED]))
            .order_by(
                OutreachMessage.scheduled_at.is_(None).desc(),
                OutreachMessage.scheduled_at,
                OutreachMessage.id,
            )
            .limit(limit)
        ).all()
    )


def process_queue(
    session: Session,
    settings: Settings,
    *,
    dry_run: bool,
    client: GmailClient | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> ProcessResult:
    """Attempt to send everything the guards allow, one message at a time."""
    now = now or utcnow()
    result = ProcessResult(dry_run=dry_run)
    batch = pending_messages(session, limit=limit or settings.effective_daily_send_limit or 1)
    result.considered = len(batch)
    if not batch:
        return result

    authenticated = False
    status_reason = "Gmail client not constructed"
    if not dry_run and settings.features.gmail_send and settings.outreach_send_enabled:
        client = client or GmailClient(settings)
        status = client.status()
        authenticated = status.authenticated
        status_reason = status.reason
    elif client is not None:
        status = client.status()
        authenticated = status.authenticated
        status_reason = status.reason

    for message in batch:
        decision = evaluate_send(
            session, settings, message, dry_run=dry_run, authenticated=authenticated, now=now
        )
        if not decision.allowed:
            _handle_blocked(session, settings, result, message, decision, status_reason, now)
            session.commit()
            continue

        assert client is not None  # guaranteed by evaluate_send requiring auth
        _attempt_send(session, settings, result, message, client, now)
        # The outcome must be durable before we return: a crash here must never
        # leave a message stranded in SENDING.
        session.commit()
        # One send per pass respects OUTREACH_MIN_SECONDS_BETWEEN_SENDS without
        # any sleeping: cron simply calls us again later.
        now = utcnow()
        break

    return result


def _handle_blocked(
    session: Session,
    settings: Settings,
    result: ProcessResult,
    message: OutreachMessage,
    decision: SendDecision,
    status_reason: str,
    now: datetime,
) -> None:
    block = decision.primary_block
    reason = decision.summary

    if block in {
        SendBlockReason.OUTSIDE_WINDOW,
        SendBlockReason.RATE_LIMIT,
        SendBlockReason.DAILY_LIMIT,
    }:
        # Never lose a message: park it with a scheduled time and move on.
        retry_at = decision.retry_after or next_window_open(settings, now)
        if message.scheduled_at != retry_at:
            message.scheduled_at = retry_at
            transition(
                session, message, MessageStatus.SCHEDULED,
                reason=reason, event_type=EventType.MESSAGE_SCHEDULED,
                payload={"retry_after": retry_at.isoformat()}, strict=False,
            )
        result.deferred += 1
        result.note(message.id, "deferred", reason)
        return

    if block == SendBlockReason.SUPPRESSED:
        transition(
            session, message, MessageStatus.SKIPPED,
            reason=reason, event_type=EventType.MESSAGE_SKIPPED, strict=False,
        )
        message.failure_reason = reason
        result.skipped += 1
        result.note(message.id, "skipped", reason)
        return

    if block in {SendBlockReason.INVALID_RECIPIENT, SendBlockReason.RESERVED_DOMAIN}:
        transition(
            session, message, MessageStatus.SKIPPED,
            reason=reason, event_type=EventType.MESSAGE_SKIPPED, strict=False,
        )
        message.failure_reason = reason
        result.skipped += 1
        result.note(message.id, "skipped", reason)
        return

    if block == SendBlockReason.ALREADY_SENT:
        result.skipped += 1
        result.note(message.id, "skipped", reason)
        return

    # DRY_RUN / FEATURE_DISABLED / SEND_DISABLED / PAUSED / NO_AUTH /
    # COMPLIANCE_INCOMPLETE: the message stays exactly where it is.
    detail = reason if block != SendBlockReason.NO_AUTH else f"{reason} ({status_reason})"
    result.skipped += 1
    result.note(message.id, "held", detail)
    record_event(
        session,
        "outreach_message",
        message.id,
        EventType.STAGE_SKIPPED_FEATURE_DISABLED
        if block in {SendBlockReason.FEATURE_DISABLED, SendBlockReason.SEND_DISABLED}
        else EventType.MESSAGE_SKIPPED,
        {"reason": detail, "block": str(block)},
    )


def _attempt_send(
    session: Session,
    settings: Settings,
    result: ProcessResult,
    message: OutreachMessage,
    client: GmailClient,
    now: datetime,
) -> None:
    """Transmit one message with crash-safe bookkeeping."""
    if not message.idempotency_key:
        message.idempotency_key = new_idempotency_key()

    # Commit SENDING *before* the network call: if we crash mid-flight the row
    # is already parked in a state that will not be blindly retried.
    message.sending_started_at = now
    message.send_attempts += 1
    transition(session, message, MessageStatus.SENDING, reason="transmitting", strict=False)
    session.commit()

    receipt: SendReceipt
    try:
        receipt = client.send(
            to_email=message.recipient_email,
            subject=message.subject,
            body_text=message.body_text,
            body_html=message.body_html,
            idempotency_key=message.idempotency_key,
            campaign_label=str(message.business_id),
            dry_run=False,
        )
    except GmailError as exc:
        receipt = SendReceipt(False, error=str(exc), ambiguous=True)

    if receipt.sent:
        message.gmail_message_id = receipt.message_id
        message.gmail_thread_id = receipt.thread_id
        message.sent_at = utcnow()
        message.failure_reason = receipt.error
        transition(
            session, message, MessageStatus.SENT,
            reason="delivered to Gmail", event_type=EventType.MESSAGE_SENT,
            payload={"gmail_message_id": receipt.message_id},
        )
        result.sent += 1
        result.note(message.id, "sent", receipt.message_id or "")
        return

    if receipt.ambiguous:
        message.failure_reason = (
            f"AMBIGUOUS SEND -- needs human review before any retry: {receipt.error}"
        )
        transition(
            session, message, MessageStatus.NEEDS_REVIEW,
            reason=message.failure_reason,
            event_type=EventType.MESSAGE_NEEDS_REVIEW,
        )
        result.needs_review += 1
        result.note(message.id, "needs_review", receipt.error or "")
        log.error(
            "ambiguous send parked for review",
            extra={"message_id": message.id, "error": receipt.error},
        )
        return

    message.failure_reason = receipt.error
    transition(
        session, message, MessageStatus.FAILED,
        reason=receipt.error or "send failed", event_type=EventType.MESSAGE_FAILED,
    )
    result.failed += 1
    result.note(message.id, "failed", receipt.error or "")


def queue_snapshot(session: Session) -> dict[str, int]:
    """Count of messages by status."""
    rows = session.execute(
        select(OutreachMessage.status, func.count(OutreachMessage.id)).group_by(
            OutreachMessage.status
        )
    ).all()
    return {str(status): int(count) for status, count in rows}

"""Reply detection and classification.

Runs only when ``FEATURE_REPLY_SYNC`` is true.  Replies are matched by Gmail
thread id, so a reply always lands on the message that provoked it.

Opt-out detection is deterministic and never depends on AI.  AI classification
of positive/negative is optional and purely advisory.  Nothing here ever
answers a prospect: humans handle every reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.ai.base import AIProvider, AIValidationError, record_ai_usage
from outreach.ai.schemas import ReplyClassificationSchema
from outreach.config import Settings
from outreach.db import utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.messaging.gmail import GmailClient, InboundMessage
from outreach.messaging.suppression import detect_opt_out, strip_quoted_reply, suppress_from_reply
from outreach.models.enums import EventType, MessageStatus, SuppressionReason
from outreach.models.outreach_message import OutreachMessage
from outreach.normalize import normalize_email

log = get_logger(__name__)

BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
BOUNCE_MARKERS = (
    "delivery status notification",
    "undeliverable",
    "address not found",
    "mail delivery subsystem",
    "recipient address rejected",
    "550 5.1.1",
)
AUTO_REPLY_MARKERS = (
    "out of office",
    "out-of-office",
    "automatic reply",
    "auto-reply",
    "autoreply",
    "away from the office",
    "on vacation",
    "on leave until",
)
POSITIVE_MARKERS = (
    "interested", "sounds good", "looks great", "let's talk", "lets talk",
    "how much", "what would it cost", "pricing", "give me a call", "call me",
    "send more", "tell me more", "yes please", "we'd like", "we would like",
    "set up a time", "schedule a call", "love it", "nice work",
)
NEGATIVE_MARKERS = (
    "not interested", "no thanks", "no thank you", "we're all set", "we are all set",
    "we just redesigned", "already have", "not looking", "pass for now",
    "no need", "we're good", "not a fit", "we have someone",
)


@dataclass
class ReplyClassification:
    classification: str
    method: str
    confidence: float = 0.5
    matched: str | None = None
    rationale: str = ""


@dataclass
class ReplySyncResult:
    checked: int = 0
    replies_found: int = 0
    opt_outs: int = 0
    positive: int = 0
    negative: int = 0
    bounced: int = 0
    other: int = 0
    skipped_reason: str | None = None
    details: list[dict[str, Any]] = field(default_factory=list)


def classify_reply_deterministic(subject: str, body: str, from_email: str = "") -> ReplyClassification:
    """Cheap, dependable classification.  Opt-out detection lives here."""
    detection = detect_opt_out(body)
    if detection.detected:
        return ReplyClassification("opt_out", "deterministic", 0.99, detection.matched)

    sender = (from_email or "").lower()
    blob = f"{subject}\n{strip_quoted_reply(body)}".lower()
    if any(sender.startswith(marker) for marker in BOUNCE_SENDERS) or any(
        marker in blob for marker in BOUNCE_MARKERS
    ):
        return ReplyClassification("bounced", "deterministic", 0.9, "bounce notification")
    if any(marker in blob for marker in AUTO_REPLY_MARKERS):
        return ReplyClassification("other", "deterministic", 0.85, "auto-reply")

    negative_hit = next((m for m in NEGATIVE_MARKERS if m in blob), None)
    if negative_hit:
        return ReplyClassification("negative", "deterministic", 0.75, negative_hit)
    positive_hit = next((m for m in POSITIVE_MARKERS if m in blob), None)
    if positive_hit:
        return ReplyClassification("positive", "deterministic", 0.7, positive_hit)
    return ReplyClassification("other", "deterministic", 0.4)


def classify_reply(
    session: Session,
    settings: Settings,
    subject: str,
    body: str,
    from_email: str = "",
    *,
    provider: AIProvider | None = None,
    use_ai: bool = False,
    business_id: int | None = None,
) -> ReplyClassification:
    """Deterministic first; AI only refines an ``other`` verdict, never an opt-out."""
    base = classify_reply_deterministic(subject, body, from_email)
    if base.classification in {"opt_out", "bounced"} or not use_ai or provider is None:
        return base

    started_at = utcnow()
    result = provider.classify_reply(reply_text=strip_quoted_reply(body)[:6000])
    record_ai_usage(session, result, business_id=business_id, started_at=started_at)
    if not result.success:
        return base
    try:
        parsed = AIProvider.validate(result, ReplyClassificationSchema)
    except AIValidationError:
        return base
    if parsed.classification == "opt_out":
        # An AI can escalate to opt-out but never downgrade one.
        return ReplyClassification("opt_out", "ai", parsed.confidence, rationale=parsed.rationale)
    if base.classification != "other" and parsed.confidence < 0.75:
        return base
    return ReplyClassification(
        parsed.classification, "ai", parsed.confidence, rationale=parsed.rationale
    )


def sent_messages_awaiting_replies(
    session: Session, *, lookback_days: int = 45
) -> list[OutreachMessage]:
    since = utcnow() - timedelta(days=lookback_days)
    return list(
        session.scalars(
            select(OutreachMessage)
            .where(
                OutreachMessage.gmail_thread_id.is_not(None),
                OutreachMessage.sent_at.is_not(None),
                OutreachMessage.sent_at >= since,
                OutreachMessage.status.in_(
                    [MessageStatus.SENT, MessageStatus.REPLIED, MessageStatus.NEEDS_REVIEW]
                ),
            )
            .order_by(OutreachMessage.sent_at.desc())
        ).all()
    )


def sync_replies(
    session: Session,
    settings: Settings,
    *,
    client: GmailClient | None = None,
    provider: AIProvider | None = None,
    limit: int = 100,
    lookback_days: int = 45,
) -> ReplySyncResult:
    """Poll Gmail for replies to messages we sent, and act on opt-outs."""
    result = ReplySyncResult()
    if not settings.features.reply_sync:
        result.skipped_reason = "SKIPPED_FEATURE_DISABLED: FEATURE_REPLY_SYNC is off"
        return result

    client = client or GmailClient(settings)
    status = client.status()
    if not status.authenticated:
        result.skipped_reason = f"Gmail is not authenticated: {status.reason}"
        return result

    our_address = normalize_email(status.email_address or settings.sender_email) or ""
    messages = sent_messages_awaiting_replies(session, lookback_days=lookback_days)[:limit]
    result.checked = len(messages)

    for message in messages:
        thread = client.list_thread_messages(message.gmail_thread_id or "")
        inbound = [
            m
            for m in thread
            if normalize_email(m.from_email) and normalize_email(m.from_email) != our_address
        ]
        inbound = [m for m in inbound if _is_after(m, message.sent_at)]
        if not inbound:
            continue
        result.replies_found += 1
        latest = inbound[-1]
        _apply_reply(session, settings, result, message, latest, provider=provider)
    return result


def _is_after(inbound: InboundMessage, sent_at: datetime | None) -> bool:
    if inbound.internal_date is None or sent_at is None:
        return True
    reference = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=UTC)
    return inbound.internal_date >= reference - timedelta(minutes=2)


def _apply_reply(
    session: Session,
    settings: Settings,
    result: ReplySyncResult,
    message: OutreachMessage,
    inbound: InboundMessage,
    *,
    provider: AIProvider | None,
) -> None:
    from outreach.messaging.queue import transition

    classification = classify_reply(
        session,
        settings,
        inbound.subject,
        inbound.body_text,
        inbound.from_email,
        provider=provider,
        use_ai=bool(provider) and settings.features.ai_scoring,
        business_id=message.business_id,
    )
    message.replied_at = message.replied_at or (inbound.internal_date or utcnow())

    record_event(
        session,
        "outreach_message",
        message.id,
        EventType.REPLY_RECEIVED,
        {
            "classification": classification.classification,
            "method": classification.method,
            "confidence": classification.confidence,
            "from": normalize_email(inbound.from_email),
            "gmail_message_id": inbound.message_id,
        },
    )

    target = {
        "opt_out": MessageStatus.OPTED_OUT,
        "positive": MessageStatus.POSITIVE_REPLY,
        "negative": MessageStatus.NEGATIVE_REPLY,
        "bounced": MessageStatus.BOUNCED,
        "other": MessageStatus.REPLIED,
    }[classification.classification]

    if classification.classification == "opt_out":
        suppress_from_reply(
            session,
            from_email=inbound.from_email,
            reply_text=inbound.body_text,
            source=f"gmail:{inbound.message_id}",
        )
        result.opt_outs += 1
    elif classification.classification == "positive":
        result.positive += 1
    elif classification.classification == "negative":
        result.negative += 1
    elif classification.classification == "bounced":
        from outreach.messaging.suppression import add_suppression

        add_suppression(
            session,
            email=message.recipient_email,
            reason=SuppressionReason.BOUNCE,
            source=f"gmail:{inbound.message_id}",
            notes=inbound.subject[:200],
        )
        result.bounced += 1
    else:
        result.other += 1

    transition(
        session, message, target,
        reason=f"reply classified as {classification.classification}",
        event_type=EventType.OPT_OUT
        if classification.classification == "opt_out"
        else EventType.REPLY_RECEIVED,
        strict=False,
    )
    result.details.append(
        {
            "message_id": message.id,
            "classification": classification.classification,
            "method": classification.method,
            "matched": classification.matched,
        }
    )
    log.info(
        "reply processed",
        extra={
            "message_id": message.id,
            "classification": classification.classification,
            "method": classification.method,
        },
    )

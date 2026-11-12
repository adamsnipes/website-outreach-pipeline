"""Send preconditions: the single choke point every outgoing email passes.

Sending requires **all** of the following, and each is evaluated here rather
than being scattered through the transport:

* the run is not a dry run                       (``--dry-run`` is absolute)
* ``FEATURE_GMAIL_SEND`` is true
* ``OUTREACH_SEND_ENABLED`` is true
* the system is not globally paused
* sender identity / compliance configuration is complete
* the recipient is a plausible, non-reserved, non-suppressed address
* the message has never been successfully sent before
* today's send limit and the minimum interval both allow it
* the current time is inside the send window
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.db import utcnow
from outreach.logging import get_logger
from outreach.messaging.suppression import is_suppressed
from outreach.models.enums import TERMINAL_SENT_STATUSES
from outreach.models.outreach_message import OutreachMessage
from outreach.normalize import normalize_email
from outreach.state import is_paused

log = get_logger(__name__)

#: TLDs reserved by RFC 2606 / RFC 6761.  Fixture and test data lives here, and
#: mail to these domains can never be delivered, so the transport refuses them.
RESERVED_TLDS = frozenset({"example", "invalid", "localhost", "test", "local"})

RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


class SendBlockReason(StrEnum):
    DRY_RUN = "DRY_RUN"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    SEND_DISABLED = "SEND_DISABLED"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    COMPLIANCE_INCOMPLETE = "COMPLIANCE_INCOMPLETE"
    NO_AUTH = "NO_AUTH"
    SUPPRESSED = "SUPPRESSED"
    INVALID_RECIPIENT = "INVALID_RECIPIENT"
    RESERVED_DOMAIN = "RESERVED_DOMAIN"
    ALREADY_SENT = "ALREADY_SENT"
    DAILY_LIMIT = "DAILY_LIMIT"
    RATE_LIMIT = "RATE_LIMIT"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    BAD_PREVIEW_URL = "BAD_PREVIEW_URL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass
class SendDecision:
    """Whether one specific message may be transmitted right now."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)
    blocks: list[SendBlockReason] = field(default_factory=list)
    retry_after: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def block(self, reason: SendBlockReason, message: str) -> None:
        self.allowed = False
        self.blocks.append(reason)
        self.reasons.append(message)

    @property
    def primary_block(self) -> SendBlockReason | None:
        return self.blocks[0] if self.blocks else None

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "allowed"


def compliance_problems(settings: Settings) -> list[str]:
    return settings.compliance_problems()


def recipient_syntax_problem(email: str | None) -> str | None:
    """Problems that make an address not worth *composing* to at all.

    Deliberately does not include reserved TLDs: fixture prospects should flow
    all the way through composition and queueing so the pipeline is fully
    exercised, and be refused at the transport door instead.
    """
    normalized = normalize_email(email)
    if not normalized:
        return f"{email!r} is not a valid email address"
    if re.search(r"^(no-?reply|donotreply|do-not-reply|postmaster|abuse)@", normalized):
        return "address is a no-reply or role mailbox"
    return None


def recipient_problem(email: str | None) -> str | None:
    """Every reason an address must never be transmitted to.

    This is the transport-level check.  It additionally refuses the reserved
    TLDs and documentation domains of RFC 2606 / RFC 6761, which is what stops
    fixture data ever leaving the building.
    """
    problem = recipient_syntax_problem(email)
    if problem:
        return problem
    normalized = normalize_email(email) or ""
    domain = normalized.rsplit("@", 1)[1]
    if domain in RESERVED_DOMAINS:
        return f"{domain} is a reserved documentation domain"
    tld = domain.rsplit(".", 1)[-1]
    if tld in RESERVED_TLDS:
        return f".{tld} is a reserved TLD that can never receive mail (fixture data?)"
    return None


def sends_today(session: Session, settings: Settings, now: datetime | None = None) -> int:
    """Messages transmitted since local midnight in the configured timezone."""
    now = (now or utcnow()).astimezone(settings.tz)
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = local_midnight.astimezone(UTC)
    return int(
        session.scalar(
            select(func.count(OutreachMessage.id)).where(
                OutreachMessage.sent_at.is_not(None), OutreachMessage.sent_at >= since
            )
        )
        or 0
    )


def last_send_at(session: Session) -> datetime | None:
    return session.scalar(
        select(func.max(OutreachMessage.sent_at)).where(OutreachMessage.sent_at.is_not(None))
    )


def in_send_window(settings: Settings, now: datetime | None = None) -> tuple[bool, str]:
    """Whether the local clock is inside the configured sending window."""
    local = (now or utcnow()).astimezone(settings.tz)
    start, end = settings.send_window
    if local.weekday() not in settings.send_weekdays:
        return False, f"{local:%A} is not a configured sending day"
    if not (start <= local.time() <= end):
        return False, (
            f"local time {local:%H:%M} is outside the "
            f"{start:%H:%M}-{end:%H:%M} {settings.outreach_timezone} window"
        )
    return True, "inside the send window"


def next_window_open(
    settings: Settings, now: datetime | None = None, *, force_next_day: bool = False
) -> datetime:
    """The next moment sending becomes possible (used to schedule, not to drop).

    ``force_next_day`` skips the rest of today, which is what the planner needs
    once a day's send allowance is used up.
    """
    local = (now or utcnow()).astimezone(settings.tz)
    start, end = settings.send_window
    candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if force_next_day or local.time() > end or (local.weekday() not in settings.send_weekdays):
        candidate = candidate + timedelta(days=1)
    elif local.time() > start:
        return local.astimezone(UTC)
    for _ in range(8):
        if candidate.weekday() in settings.send_weekdays and candidate >= local:
            return candidate.astimezone(UTC)
        candidate = candidate + timedelta(days=1)
    return (local + timedelta(days=1)).astimezone(UTC)  # pragma: no cover - defensive


def evaluate_send(
    session: Session,
    settings: Settings,
    message: OutreachMessage,
    *,
    dry_run: bool,
    authenticated: bool,
    now: datetime | None = None,
    check_window: bool = True,
    check_limits: bool = True,
) -> SendDecision:
    """The one function that decides whether an email may leave the building."""
    now = now or utcnow()
    decision = SendDecision(allowed=True)

    # --- absolute blocks -------------------------------------------------
    if dry_run:
        decision.block(
            SendBlockReason.DRY_RUN,
            "DRY RUN: no email may be sent, regardless of feature flags",
        )
        # Deliberately keep evaluating so --dry-run reports everything else too.
    if not settings.features.gmail_send:
        decision.block(
            SendBlockReason.FEATURE_DISABLED, "SKIPPED_FEATURE_DISABLED: FEATURE_GMAIL_SEND is off"
        )
    if not settings.outreach_send_enabled:
        decision.block(SendBlockReason.SEND_DISABLED, "OUTREACH_SEND_ENABLED is false")
    if is_paused(session):
        decision.block(SendBlockReason.SYSTEM_PAUSED, "the system is globally paused")
    if not authenticated:
        decision.block(SendBlockReason.NO_AUTH, "Gmail OAuth is not authenticated")

    problems = compliance_problems(settings)
    if problems:
        decision.block(
            SendBlockReason.COMPLIANCE_INCOMPLETE,
            "sender identity is incomplete: " + "; ".join(problems),
        )

    # --- recipient -------------------------------------------------------
    problem = recipient_problem(message.recipient_email)
    if problem:
        decision.block(SendBlockReason.INVALID_RECIPIENT, problem)
    else:
        suppressed, reason = is_suppressed(
            session, message.recipient_email, check_domain=settings.suppress_by_domain
        )
        if suppressed:
            decision.block(SendBlockReason.SUPPRESSED, reason or "recipient is suppressed")

    # --- one email per business, ever ------------------------------------
    if message.status in TERMINAL_SENT_STATUSES or message.sent_at is not None:
        decision.block(
            SendBlockReason.ALREADY_SENT,
            f"this message is already in state {message.status}",
        )
    if message.gmail_message_id:
        decision.block(
            SendBlockReason.ALREADY_SENT,
            f"a Gmail message id is already recorded ({message.gmail_message_id})",
        )

    # --- volume ----------------------------------------------------------
    if check_limits:
        limit = settings.effective_daily_send_limit
        today = sends_today(session, settings, now)
        decision.details["sends_today"] = today
        decision.details["daily_limit"] = limit
        decision.details["hard_max"] = settings.hard_max_daily_send_limit
        if today >= limit:
            decision.block(
                SendBlockReason.DAILY_LIMIT,
                f"daily send limit reached ({today}/{limit})",
            )
        last = last_send_at(session)
        if last is not None:
            min_gap = settings.effective_min_seconds_between_sends
            elapsed = (now - last).total_seconds()
            decision.details["seconds_since_last_send"] = int(elapsed)
            if elapsed < min_gap:
                decision.retry_after = last + timedelta(seconds=min_gap)
                decision.block(
                    SendBlockReason.RATE_LIMIT,
                    f"only {int(elapsed)}s since the last send (minimum {min_gap}s)",
                )

    # --- schedule --------------------------------------------------------
    if check_window:
        inside, why = in_send_window(settings, now)
        if not inside:
            decision.retry_after = decision.retry_after or next_window_open(settings, now)
            decision.block(SendBlockReason.OUTSIDE_WINDOW, why)

    if decision.allowed:
        decision.reasons.append("all send preconditions satisfied")
    return decision


def build_compliance_footer(settings: Settings) -> str:
    """The identity + opt-out block appended to every outgoing message."""
    lines = [
        settings.sender_name.strip(),
        settings.sender_company.strip(),
    ]
    if settings.sender_postal_address.strip():
        lines.append(settings.sender_postal_address.strip())
    lines.append("")
    lines.append(settings.unsubscribe_instruction.strip())
    return "\n".join(line for line in lines if line is not None).strip()

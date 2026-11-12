"""Permanent do-not-contact list and opt-out detection.

Suppression is checked before every single send and is never removed
automatically.  Opt-out detection is deterministic -- it never depends on an AI
call being available, funded or correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from outreach.events import record_event
from outreach.logging import get_logger
from outreach.models.enums import EventType, SuppressionReason
from outreach.models.suppression import Suppression
from outreach.normalize import normalize_domain, normalize_email

log = get_logger(__name__)

#: Deterministic opt-out phrases.  Matched against the reply body, lowercased.
OPT_OUT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?<![a-z])stop(?![a-z])"),
    re.compile(r"\bunsubscribe\b"),
    re.compile(r"\bremove me\b"),
    re.compile(r"\bremove (?:us|my email|our email|this email)\b"),
    re.compile(r"\btake me off\b"),
    re.compile(r"\btake us off\b"),
    re.compile(r"\bopt[- ]?out\b"),
    re.compile(r"\bdo ?n[o']?t (?:contact|email|e-mail|message|write to) (?:me|us)\b"),
    re.compile(r"\bdon'?t contact\b"),
    re.compile(r"\bnever contact\b"),
    re.compile(r"\bno (?:more|further) (?:emails?|contact|messages?)\b"),
    re.compile(r"\bstop (?:emailing|contacting|messaging)\b"),
    re.compile(r"\bcease (?:and desist|contact)\b"),
    re.compile(r"\bplease remove\b"),
    re.compile(r"\bnot interested.{0,20}(?:remove|stop|unsubscribe)\b"),
]

#: Reply text that only *looks* like an opt-out (quoted footer of our own mail).
_QUOTED_LINE = re.compile(r"^\s*(?:>|On .+ wrote:)", re.M)


@dataclass(frozen=True)
class OptOutDetection:
    detected: bool
    matched: str | None = None
    excerpt: str = ""


def strip_quoted_reply(text: str) -> str:
    """Drop quoted history so our own opt-out sentence is not read as theirs."""
    if not text:
        return ""
    lines: list[str] = []
    for line in str(text).splitlines():
        if _QUOTED_LINE.match(line):
            break
        if line.strip().startswith(("--", "___", "===")) and len(lines) > 0:
            break
        lines.append(line)
    return "\n".join(lines).strip() or str(text).strip()


def detect_opt_out(text: str, *, strip_quotes: bool = True) -> OptOutDetection:
    """Deterministic opt-out detection.  No AI involved, ever."""
    body = strip_quoted_reply(text) if strip_quotes else (text or "")
    lowered = body.lower()
    for pattern in OPT_OUT_PATTERNS:
        match = pattern.search(lowered)
        if match:
            start = max(0, match.start() - 60)
            return OptOutDetection(True, match.group(0), body[start : match.end() + 60])
    return OptOutDetection(False)


# ---------------------------------------------------------------------------
def is_suppressed(
    session: Session,
    email: str | None,
    *,
    check_domain: bool = True,
) -> tuple[bool, str | None]:
    """``(suppressed, reason)`` for one address."""
    normalized = normalize_email(email)
    if not normalized:
        return True, "address is not a valid email address"

    row = session.scalars(
        select(Suppression).where(Suppression.normalized_email == normalized)
    ).first()
    if row is not None:
        return True, f"email is suppressed ({row.reason})"

    if check_domain:
        domain = normalize_domain(normalized)
        if domain:
            row = session.scalars(
                select(Suppression).where(Suppression.normalized_domain == domain)
            ).first()
            if row is not None:
                return True, f"domain {domain} is suppressed ({row.reason})"
    return False, None


def add_suppression(
    session: Session,
    *,
    email: str | None = None,
    domain: str | None = None,
    reason: str = SuppressionReason.MANUAL,
    source: str | None = None,
    notes: str | None = None,
) -> Suppression | None:
    """Add (or return the existing) suppression entry."""
    normalized_email = normalize_email(email) if email else None
    normalized_domain = normalize_domain(domain) if domain else None
    if not normalized_email and not normalized_domain:
        raise ValueError("suppression needs either an email address or a domain")

    stmt = select(Suppression)
    if normalized_email:
        stmt = stmt.where(Suppression.normalized_email == normalized_email)
    else:
        stmt = stmt.where(Suppression.normalized_domain == normalized_domain)
    existing = session.scalars(stmt).first()
    if existing is not None:
        return existing

    entry = Suppression(
        normalized_email=normalized_email,
        normalized_domain=normalized_domain,
        reason=str(reason),
        source=source,
        notes=notes,
    )
    session.add(entry)
    session.flush()
    record_event(
        session,
        "suppression",
        entry.id,
        EventType.SUPPRESSED,
        {"email": normalized_email, "domain": normalized_domain, "reason": str(reason)},
    )
    log.warning(
        "suppression added",
        extra={"email": normalized_email, "domain": normalized_domain, "reason": str(reason)},
    )
    return entry


def remove_suppression(
    session: Session, *, email: str | None = None, domain: str | None = None
) -> int:
    """Remove an entry.  Only ever called by an explicit operator command."""
    normalized_email = normalize_email(email) if email else None
    normalized_domain = normalize_domain(domain) if domain else None
    if not normalized_email and not normalized_domain:
        raise ValueError("removal needs either an email address or a domain")
    stmt = delete(Suppression)
    stmt = (
        stmt.where(Suppression.normalized_email == normalized_email)
        if normalized_email
        else stmt.where(Suppression.normalized_domain == normalized_domain)
    )
    removed = int(getattr(session.execute(stmt), "rowcount", 0) or 0)
    if removed:
        record_event(
            session,
            "suppression",
            None,
            EventType.SUPPRESSION_REMOVED,
            {"email": normalized_email, "domain": normalized_domain, "count": removed},
        )
        log.warning(
            "suppression removed by operator",
            extra={"email": normalized_email, "domain": normalized_domain},
        )
    return removed


def list_suppressions(session: Session, limit: int = 200) -> list[Suppression]:
    return list(
        session.scalars(
            select(Suppression).order_by(Suppression.created_at.desc()).limit(limit)
        ).all()
    )


def suppress_from_reply(
    session: Session, *, from_email: str, reply_text: str, source: str = "reply"
) -> Suppression | None:
    """Suppress immediately when a reply contains opt-out language."""
    detection = detect_opt_out(reply_text)
    if not detection.detected:
        return None
    entry = add_suppression(
        session,
        email=from_email,
        reason=SuppressionReason.REPLY_OPT_OUT,
        source=source,
        notes=f"matched {detection.matched!r}: {detection.excerpt[:300]}",
    )
    record_event(
        session,
        "suppression",
        entry.id if entry else None,
        EventType.OPT_OUT,
        {"email": normalize_email(from_email), "matched": detection.matched},
    )
    return entry

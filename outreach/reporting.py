"""Funnel metrics and operator reports.

Everything here is a read-only query over the same tables the pipeline writes,
so a report can never disagree with what actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.db import utcnow
from outreach.models.ai_assessment import AIWebsiteAssessment
from outreach.models.ai_usage import AIUsage
from outreach.models.build import Build
from outreach.models.business import Business
from outreach.models.business_content import BusinessContent
from outreach.models.contact import Contact
from outreach.models.enums import (
    AIPurpose,
    BuildStatus,
    MessageStatus,
    QualificationStatus,
    ScanStatus,
)
from outreach.models.outreach_message import OutreachMessage
from outreach.models.scan import WebsiteScan
from outreach.models.suppression import Suppression
from outreach.quota import GOOGLE_PLACES, monthly_usage, usage_report


@dataclass
class FunnelReport:
    campaign_id: int | None
    counts: dict[str, int] = field(default_factory=dict)
    provider_usage: list[dict[str, Any]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utcnow)

    def get(self, key: str) -> int:
        return int(self.counts.get(key, 0))

    def funnel_lines(self) -> list[str]:
        """The compact funnel view."""
        rows = [
            ("discovered", "discovered"),
            ("with a website", "with_website"),
            ("scanned successfully", "scans_ok"),
            ("redesign candidates", "redesign_candidates_total"),
            ("public emails found", "with_public_email"),
            ("builds succeeded", "builds_succeeded"),
            ("queued", "messages_queued"),
            ("sent", "messages_sent"),
            ("replies", "replies_total"),
        ]
        width = max(len(label) for label, _ in rows)
        return [f"{self.get(key):>6}  {label:<{width}}" for label, key in rows]

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "generated_at": self.generated_at.isoformat(),
            "counts": self.counts,
            "provider_usage": self.provider_usage,
        }


def _scoped(stmt: Any, campaign_id: int | None, model: Any = Business) -> Any:
    return stmt.where(model.campaign_id == campaign_id) if campaign_id is not None else stmt


def build_funnel_report(
    session: Session, settings: Settings, campaign_id: int | None = None
) -> FunnelReport:
    """Every metric the operator asked to track, in one pass."""
    report = FunnelReport(campaign_id=campaign_id)
    c = report.counts

    business_ids_stmt = select(Business.id)
    if campaign_id is not None:
        business_ids_stmt = business_ids_stmt.where(Business.campaign_id == campaign_id)
    business_ids = list(session.scalars(business_ids_stmt).all())
    in_scope = Business.id.in_(business_ids) if campaign_id is not None else Business.id.is_not(None)

    c["discovered"] = len(business_ids)
    c["unique_businesses"] = len(set(business_ids))
    c["with_website"] = int(
        session.scalar(
            _scoped(
                select(func.count(Business.id)).where(Business.website_url.is_not(None)),
                campaign_id,
            )
        ) or 0
    )
    c["without_website"] = c["discovered"] - c["with_website"]

    # -- scans ------------------------------------------------------------
    scan_rows = session.execute(
        select(WebsiteScan.scan_status, func.count(WebsiteScan.id))
        .join(Business, Business.id == WebsiteScan.business_id)
        .where(in_scope)
        .group_by(WebsiteScan.scan_status)
    ).all()
    for status, count in scan_rows:
        c[f"scan_{str(status).lower()}"] = int(count)
    c["scans_total"] = sum(int(count) for _, count in scan_rows)
    c["scans_ok"] = c.get(f"scan_{str(ScanStatus.OK).lower()}", 0)
    c["scans_failed"] = c["scans_total"] - c["scans_ok"]

    # -- qualification ----------------------------------------------------
    qual_rows = session.execute(
        _scoped(
            select(Business.qualification_status, func.count(Business.id)).group_by(
                Business.qualification_status
            ),
            campaign_id,
        )
    ).all()
    for status, count in qual_rows:
        c[str(status).lower()] = int(count)
    c["redesign_candidates_total"] = c.get(
        str(QualificationStatus.REDESIGN_CANDIDATE).lower(), 0
    ) + c.get(str(QualificationStatus.STRONG_REDESIGN_CANDIDATE).lower(), 0)

    contact_rows = session.execute(
        select(func.count(func.distinct(Contact.business_id)))
        .join(Business, Business.id == Contact.business_id)
        .where(in_scope)
    ).scalar()
    c["with_public_email"] = int(contact_rows or 0)
    c["no_public_email"] = max(0, c["scans_ok"] - c["with_public_email"])
    c["contacts_total"] = int(
        session.scalar(
            select(func.count(Contact.id))
            .join(Business, Business.id == Contact.business_id)
            .where(in_scope)
        ) or 0
    )

    # -- AI ---------------------------------------------------------------
    c["ai_assessments"] = int(
        session.scalar(
            select(func.count(AIWebsiteAssessment.id))
            .join(Business, Business.id == AIWebsiteAssessment.business_id)
            .where(in_scope)
        ) or 0
    )
    c["ai_content_extractions"] = int(
        session.scalar(
            select(func.count(BusinessContent.id))
            .join(Business, Business.id == BusinessContent.business_id)
            .where(in_scope, BusinessContent.extraction_method == "ai")
        ) or 0
    )
    ai_rows = session.execute(
        select(AIUsage.purpose, AIUsage.success, func.count(AIUsage.id)).group_by(
            AIUsage.purpose, AIUsage.success
        )
    ).all()
    for purpose, success, count in ai_rows:
        key = f"ai_calls_{str(purpose).lower()}_{'ok' if success else 'failed'}"
        c[key] = int(count)
    c["ai_calls_total"] = sum(int(count) for _, _, count in ai_rows)

    # -- builds -----------------------------------------------------------
    build_rows = session.execute(
        select(Build.status, func.count(Build.id))
        .join(Business, Business.id == Build.business_id)
        .where(in_scope)
        .group_by(Build.status)
    ).all()
    for status, count in build_rows:
        c[f"builds_{str(status).lower()}"] = int(count)
    c["builds_total"] = sum(int(count) for _, count in build_rows)
    c["builds_succeeded"] = c.get(f"builds_{str(BuildStatus.BUILT).lower()}", 0) + c.get(
        f"builds_{str(BuildStatus.PUBLISHED).lower()}", 0
    )
    c["builds_failed"] = c.get(f"builds_{str(BuildStatus.FAILED).lower()}", 0) + c.get(
        f"builds_{str(BuildStatus.QA_FAILED).lower()}", 0
    )
    c["previews_published"] = c.get(f"builds_{str(BuildStatus.PUBLISHED).lower()}", 0)

    # -- messages ---------------------------------------------------------
    message_rows = session.execute(
        select(OutreachMessage.status, func.count(OutreachMessage.id))
        .join(Business, Business.id == OutreachMessage.business_id)
        .where(in_scope)
        .group_by(OutreachMessage.status)
    ).all()
    for status, count in message_rows:
        c[f"messages_{str(status).lower()}"] = int(count)
    c["messages_composed"] = sum(int(count) for _, count in message_rows)
    c["messages_queued"] = c.get(f"messages_{str(MessageStatus.QUEUED).lower()}", 0) + c.get(
        f"messages_{str(MessageStatus.SCHEDULED).lower()}", 0
    )
    c["messages_sent"] = int(
        session.scalar(
            select(func.count(OutreachMessage.id))
            .join(Business, Business.id == OutreachMessage.business_id)
            .where(in_scope, OutreachMessage.sent_at.is_not(None))
        ) or 0
    )
    c["replies_positive"] = c.get(f"messages_{str(MessageStatus.POSITIVE_REPLY).lower()}", 0)
    c["replies_negative"] = c.get(f"messages_{str(MessageStatus.NEGATIVE_REPLY).lower()}", 0)
    c["opt_outs"] = c.get(f"messages_{str(MessageStatus.OPTED_OUT).lower()}", 0)
    c["bounced"] = c.get(f"messages_{str(MessageStatus.BOUNCED).lower()}", 0)
    c["needs_review"] = c.get(f"messages_{str(MessageStatus.NEEDS_REVIEW).lower()}", 0)
    c["replies_total"] = (
        c.get(f"messages_{str(MessageStatus.REPLIED).lower()}", 0)
        + c["replies_positive"]
        + c["replies_negative"]
        + c["opt_outs"]
    )
    c["messages_pending"] = c["messages_queued"] + c.get(
        f"messages_{str(MessageStatus.DRAFT).lower()}", 0
    )
    c["suppressions"] = int(session.scalar(select(func.count(Suppression.id))) or 0)

    # -- metered providers ------------------------------------------------
    c["google_places_requests_this_month"] = monthly_usage(session, GOOGLE_PLACES)
    report.provider_usage = usage_report(session)
    return report


def today_report(session: Session, settings: Settings) -> dict[str, Any]:
    """What happened, and what may still happen, today."""
    from outreach.messaging.compliance import in_send_window, next_window_open, sends_today
    from outreach.messaging.queue import pending_messages, queue_snapshot
    from outreach.state import get_pause_state

    now = utcnow()
    local = now.astimezone(settings.tz)
    inside, why = in_send_window(settings, now)
    sent = sends_today(session, settings, now)
    limit = settings.effective_daily_send_limit
    pause = get_pause_state(session)

    return {
        "local_time": local.isoformat(),
        "timezone": settings.outreach_timezone,
        "paused": pause.paused,
        "pause_summary": pause.summary,
        "in_send_window": inside,
        "send_window_note": why,
        "next_window_open": next_window_open(settings, now).isoformat(),
        "sent_today": sent,
        "daily_limit": limit,
        "hard_max": settings.hard_max_daily_send_limit,
        "remaining_today": max(0, limit - sent),
        "pending": len(pending_messages(session, limit=500)),
        "queue": queue_snapshot(session),
        "sending_enabled": bool(
            settings.features.gmail_send and settings.outreach_send_enabled
        ),
    }


def failure_report(session: Session, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
    """Everything that went wrong and might need a human."""
    failed_scans = [
        {
            "business_id": s.business_id,
            "url": s.requested_url,
            "status": str(s.scan_status),
            "error": (s.error or "")[:200],
            "at": s.scanned_at.isoformat() if s.scanned_at else None,
        }
        for s in session.scalars(
            select(WebsiteScan)
            .where(WebsiteScan.scan_status != ScanStatus.OK)
            .order_by(WebsiteScan.id.desc())
            .limit(limit)
        ).all()
    ]
    failed_builds = [
        {
            "build_id": b.id,
            "business_id": b.business_id,
            "status": str(b.status),
            "error": (b.error or "")[:300],
        }
        for b in session.scalars(
            select(Build)
            .where(Build.status.in_([BuildStatus.FAILED, BuildStatus.QA_FAILED]))
            .order_by(Build.id.desc())
            .limit(limit)
        ).all()
    ]
    failed_messages = [
        {
            "message_id": m.id,
            "business_id": m.business_id,
            "recipient": m.recipient_email,
            "status": str(m.status),
            "attempts": m.send_attempts,
            "reason": (m.failure_reason or "")[:300],
        }
        for m in session.scalars(
            select(OutreachMessage)
            .where(
                OutreachMessage.status.in_(
                    [MessageStatus.FAILED, MessageStatus.NEEDS_REVIEW, MessageStatus.BOUNCED]
                )
            )
            .order_by(OutreachMessage.id.desc())
            .limit(limit)
        ).all()
    ]
    failed_ai = [
        {
            "id": u.id,
            "business_id": u.business_id,
            "purpose": u.purpose,
            "provider": u.provider,
            "error": (u.error or "")[:300],
        }
        for u in session.scalars(
            select(AIUsage)
            .where(AIUsage.success.is_(False))
            .order_by(AIUsage.id.desc())
            .limit(limit)
        ).all()
    ]
    return {
        "failed_scans": failed_scans,
        "failed_builds": failed_builds,
        "failed_messages": failed_messages,
        "failed_ai_calls": failed_ai,
    }


def ai_usage_report(session: Session, days: int = 30) -> dict[str, Any]:
    """AI invocations by purpose over a window."""
    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(
            AIUsage.purpose,
            AIUsage.provider,
            func.count(AIUsage.id),
            func.sum(func.cast(AIUsage.success, Integer)),
            func.avg(AIUsage.duration_ms),
        )
        .where(AIUsage.started_at >= since)
        .group_by(AIUsage.purpose, AIUsage.provider)
    ).all()
    by_purpose = [
        {
            "purpose": str(purpose),
            "provider": provider,
            "calls": int(count),
            "successful": int(successes or 0),
            "failed": int(count) - int(successes or 0),
            "avg_duration_ms": round(float(avg_ms or 0), 1),
        }
        for purpose, provider, count, successes, avg_ms in rows
    ]
    return {
        "since": since.isoformat(),
        "days": days,
        "total_calls": sum(row["calls"] for row in by_purpose),
        "by_purpose": sorted(by_purpose, key=lambda r: -r["calls"]),
        "known_purposes": [str(p) for p in AIPurpose],
    }


def learning_dataset(session: Session, limit: int = 500) -> list[dict[str, Any]]:
    """Flat rows linking website problems to outreach outcomes.

    No ML here -- just the structured data a future analysis would need to ask
    "which website problems correlate with replies and sales?".
    """
    rows: list[dict[str, Any]] = []
    stmt = (
        select(Business, WebsiteScan, OutreachMessage)
        .join(WebsiteScan, WebsiteScan.business_id == Business.id)
        .outerjoin(OutreachMessage, OutreachMessage.business_id == Business.id)
        .where(WebsiteScan.scan_status == ScanStatus.OK)
        .order_by(Business.id)
        .limit(limit)
    )
    for business, scan, message in session.execute(stmt).all():
        findings = (scan.technical_findings_json or {}).get("findings") or []
        rows.append(
            {
                "business_id": business.id,
                "vertical": business.campaign.vertical if business.campaign else None,
                "city": business.city,
                "deterministic_score": scan.technical_site_quality_score,
                "combined_score": business.combined_score,
                "qualification_status": business.qualification_status,
                "build_priority": business.build_priority,
                "problem_codes": sorted({f.get("code") for f in findings if f.get("code")}),
                "high_severity_codes": sorted(
                    {f.get("code") for f in findings if f.get("severity") == "high"}
                ),
                "contacted": bool(message and message.sent_at),
                "message_status": str(message.status) if message else None,
                "replied": bool(message and message.replied_at),
                "positive_reply": bool(message and str(message.status) == MessageStatus.POSITIVE_REPLY),
                "opted_out": bool(message and str(message.status) == MessageStatus.OPTED_OUT),
                "sent_at": message.sent_at.isoformat() if message and message.sent_at else None,
            }
        )
    return rows


def utc_day_bounds(moment: datetime | None = None) -> tuple[datetime, datetime]:
    moment = moment or utcnow()
    start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)

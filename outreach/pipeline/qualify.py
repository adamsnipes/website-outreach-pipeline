"""AI visual scoring (gated) + final qualification of every scanned business."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.ai.base import get_ai_provider
from outreach.crawler.contacts import CONTACT_TYPE_PRIORITY
from outreach.events import record_event
from outreach.logging import get_logger, safe_extra
from outreach.messaging.suppression import is_suppressed
from outreach.models.build import Build
from outreach.models.business import Business
from outreach.models.enums import BuildStatus, EventType, QualificationStatus
from outreach.models.outreach_message import OutreachMessage
from outreach.pipeline.context import RunContext, StageResult
from outreach.pipeline.scan import campaign_for, latest_scan
from outreach.scoring.qualification import qualify
from outreach.scoring.visual import latest_assessment, run_visual_scoring, should_ai_score

log = get_logger(__name__)


def contact_dicts(business: Business) -> list[dict[str, Any]]:
    """Business contacts, best mailbox first."""
    rows = [
        {
            "normalized_email": c.normalized_email,
            "contact_type": str(c.contact_type),
            "confidence": c.confidence,
            "source_url": c.source_url,
        }
        for c in business.contacts
    ]
    def sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
        return (
            -CONTACT_TYPE_PRIORITY.get(str(row["contact_type"]), 30),
            -float(row["confidence"] or 0.0),
            str(row["normalized_email"]),
        )

    rows.sort(key=sort_key)
    return rows


def existing_build(session: Session, business_id: int) -> Build | None:
    return session.scalars(
        select(Build)
        .where(
            Build.business_id == business_id,
            Build.status.in_([BuildStatus.BUILT, BuildStatus.PUBLISHED]),
        )
        .order_by(Build.id.desc())
        .limit(1)
    ).first()


def existing_message_status(session: Session, business_id: int) -> str | None:
    message = session.scalars(
        select(OutreachMessage).where(OutreachMessage.business_id == business_id)
    ).first()
    if message is None:
        return None
    from outreach.scoring.qualification import already_messaged

    return str(message.status) if already_messaged(message.status) else None


def qualify_business(session: Session, ctx: RunContext, business: Business) -> dict[str, Any]:
    """Combine scores, decide status/contactability, cache the roll-up."""
    scan = latest_scan(session, business.id)
    assessment = latest_assessment(session, business.id)
    contacts = contact_dicts(business)

    suppressed = False
    if contacts:
        suppressed, _reason = is_suppressed(
            session, contacts[0]["normalized_email"],
            check_domain=ctx.settings.suppress_by_domain,
        )

    result = qualify(
        ctx.settings,
        scan_status=str(scan.scan_status) if scan else None,
        deterministic_score=scan.technical_site_quality_score if scan else None,
        technical_findings=scan.technical_findings_json if scan else None,
        assessment=assessment,
        has_website=bool(business.website_url),
        business_status=business.business_status,
        contacts=contacts,
        site_domain=business.normalized_domain,
        already_built=existing_build(session, business.id) is not None,
        already_messaged_status=existing_message_status(session, business.id),
        suppressed=suppressed,
    )

    business.qualification_status = str(result.qualification_status)
    business.contactability_status = str(result.contactability_status)
    business.combined_score = result.combined_score
    business.build_priority = result.build_priority

    record_event(
        session, "business", business.id, EventType.QUALIFIED, result.to_dict()
    )
    return result.to_dict()


def run_ai_scoring(session: Session, ctx: RunContext) -> StageResult:
    """Spend AI usage only on businesses deterministic filtering could not resolve."""
    stage = ctx.require_feature("ai_scoring", "FEATURE_AI_SCORING")
    if stage is not None:
        return stage
    stage = StageResult(name="ai_scoring")

    settings = ctx.settings
    provider = get_ai_provider(settings)
    health = provider.health_check()
    if not health.available:
        stage.executed = False
        stage.skipped_reason = f"AI provider unavailable: {health.reason}"
        stage.notes.append(health.reason)
        return stage

    campaign = campaign_for(session, ctx.campaign_id)
    vertical = campaign.vertical if campaign else ctx.vertical

    stmt = select(Business)
    if ctx.campaign_id is not None:
        stmt = stmt.where(Business.campaign_id == ctx.campaign_id)
    businesses = list(
        session.scalars(stmt.order_by(Business.build_priority.desc().nullslast(), Business.id)).all()
    )

    budget = ctx.effective_limit(settings.max_ai_scores_per_run)
    for business in businesses:
        if stage.counts.get("scored", 0) >= budget:
            stage.notes.append(f"AI scoring budget of {budget} reached")
            break
        scan = latest_scan(session, business.id)
        if scan is None or str(scan.scan_status) != "OK":
            continue
        ok, skip_reason = should_ai_score(
            settings,
            business.qualification_status,
            already_assessed=latest_assessment(session, business.id) is not None,
            force=ctx.force,
        )
        if not ok:
            stage.bump("skipped")
            stage.notes.append(f"business {business.id}: {skip_reason}")
            continue
        outcome = run_visual_scoring(
            session, settings, provider, business, scan, vertical=vertical
        )
        if outcome.scored:
            stage.bump("scored")
        else:
            stage.bump("failed")
            stage.errors.append(f"business {business.id}: {outcome.reason}")
        session.commit()
    return stage


def run_qualification(session: Session, ctx: RunContext) -> StageResult:
    """Qualify every business in the campaign (works with or without AI)."""
    stage = StageResult(name="qualify")
    stmt = select(Business)
    if ctx.campaign_id is not None:
        stmt = stmt.where(Business.campaign_id == ctx.campaign_id)
    businesses = list(session.scalars(stmt.order_by(Business.id)).all())

    for business in businesses:
        result = qualify_business(session, ctx, business)
        stage.bump("qualified")
        stage.bump(str(result["qualification_status"]).lower())
        stage.bump(str(result["contactability_status"]).lower())
        if result["is_build_candidate"]:
            stage.bump("build_candidates")
    session.commit()
    log.info("qualification stage complete", extra=safe_extra(stage.counts))
    return stage


def build_candidates(
    session: Session, campaign_id: int | None, limit: int
) -> list[Business]:
    """Best build candidates first.

    Priority is *not* simply the lowest score: an active business with an ugly
    but working site outranks a dead domain.
    """
    stmt = select(Business).where(
        Business.qualification_status.in_(
            [
                QualificationStatus.REDESIGN_CANDIDATE,
                QualificationStatus.STRONG_REDESIGN_CANDIDATE,
            ]
        ),
        Business.contactability_status == "CONTACTABLE",
    )
    if campaign_id is not None:
        stmt = stmt.where(Business.campaign_id == campaign_id)
    stmt = stmt.order_by(Business.build_priority.desc().nullslast(), Business.id)
    out: list[Business] = []
    for business in session.scalars(stmt).all():
        if existing_build(session, business.id) is not None:
            continue
        if existing_message_status(session, business.id):
            continue
        out.append(business)
        if len(out) >= limit:
            break
    return out

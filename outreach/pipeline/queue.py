"""Compose + queue stage: turn built previews into queued outreach messages."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.ai.base import get_ai_provider
from outreach.logging import get_logger, safe_extra
from outreach.messaging.compliance import recipient_syntax_problem
from outreach.messaging.composer import compose
from outreach.messaging.queue import enqueue, get_message_for_business, upsert_draft
from outreach.messaging.scheduler import plan_send_times
from outreach.messaging.suppression import is_suppressed
from outreach.models.build import Build
from outreach.models.business import Business
from outreach.models.enums import TERMINAL_SENT_STATUSES, BuildStatus, MessageStatus
from outreach.models.outreach_message import OutreachMessage
from outreach.pipeline.context import RunContext, StageResult
from outreach.pipeline.qualify import contact_dicts
from outreach.pipeline.scan import campaign_for, latest_scan
from outreach.previews.manager import PreviewManager, validate_preview_url
from outreach.scoring.visual import latest_assessment

log = get_logger(__name__)


def buildable_messages(session: Session, campaign_id: int | None) -> list[tuple[Business, Build]]:
    """Businesses with a QA-passed build and no outreach message yet."""
    stmt = select(Build).where(
        Build.status.in_([BuildStatus.BUILT, BuildStatus.PUBLISHED]),
        Build.qa_passed.is_(True),
    )
    if campaign_id is not None:
        stmt = stmt.join(Business, Business.id == Build.business_id).where(
            Business.campaign_id == campaign_id
        )
    out: list[tuple[Business, Build]] = []
    for build in session.scalars(stmt.order_by(Build.id.desc())).all():
        business = session.get(Business, build.business_id)
        if business is None:
            continue
        if any(b.id == build.business_id for b, _ in out):
            continue
        existing = get_message_for_business(session, business.id)
        if existing is not None and str(existing.status) in {
            str(s) for s in TERMINAL_SENT_STATUSES
        }:
            continue
        out.append((business, build))
    return out


def run_compose_and_queue(session: Session, ctx: RunContext) -> StageResult:
    """Compose one email per built prospect and put it in the queue."""
    stage = StageResult(name="compose_queue")
    settings = ctx.settings
    manager = PreviewManager(settings)
    campaign = campaign_for(session, ctx.campaign_id)
    vertical = campaign.vertical if campaign else ctx.vertical
    city = campaign.city if campaign else ctx.city

    provider = None
    use_ai = settings.features.email_composition
    if use_ai:
        provider = get_ai_provider(settings)
        health = provider.health_check()
        if not health.available:
            stage.notes.append(f"AI composition unavailable: {health.reason}")
            provider, use_ai = None, False
    else:
        stage.notes.append("SKIPPED_FEATURE_DISABLED: FEATURE_EMAIL_COMPOSITION is off "
                           "(using deterministic templates)")

    pairs = buildable_messages(session, ctx.campaign_id)
    if not pairs:
        stage.notes.append("no QA-passed builds are waiting for an email")
        return stage

    composed: list[int] = []
    for business, build in pairs:
        contacts = contact_dicts(business)
        if not contacts:
            stage.bump("no_public_email")
            continue
        recipient = contacts[0]["normalized_email"]

        problem = recipient_syntax_problem(recipient)
        if problem:
            stage.bump("invalid_recipient")
            stage.notes.append(f"business {business.id}: {problem}")
            continue
        suppressed, reason = is_suppressed(
            session, recipient, check_domain=settings.suppress_by_domain
        )
        if suppressed:
            stage.bump("suppressed")
            stage.notes.append(f"business {business.id}: {reason}")
            continue

        preview_url = build.preview_url or manager.public_url(business.id)
        check = validate_preview_url(preview_url)
        if not check.usable:
            stage.bump("no_usable_preview_url")
            stage.notes.append(f"business {business.id}: {check.reason}")
            continue

        scan = latest_scan(session, business.id)
        assessment = latest_assessment(session, business.id)
        email = compose(
            settings,
            provider=provider,
            use_ai=use_ai,
            business_name=business.name,
            city=business.city or city,
            vertical=vertical,
            preview_url=check.url or preview_url or "",
            technical_findings=scan.technical_findings_json if scan else None,
            ai_issues=(assessment.issues_json if assessment else None),
        )
        outcome = upsert_draft(
            session,
            business_id=business.id,
            build_id=build.id,
            recipient_email=recipient,
            subject=email.subject,
            body_text=email.body_text,
            body_html=email.body_html,
            composition_method=email.method,
            observations=email.observations,
        )
        if outcome.message is None:
            stage.bump("failed")
            continue
        stage.bump("composed")
        if outcome.created:
            stage.bump("created")
        composed.append(outcome.message.id)
        stage.items.append(
            {
                "business_id": business.id,
                "message_id": outcome.message.id,
                "recipient": recipient,
                "method": email.method,
                "observations": [o["code"] for o in email.observations],
                "warnings": email.warnings,
            }
        )
        session.flush()

    # Give every fresh draft a slot inside the send window.
    drafts = [
        m
        for m in (session.get(OutreachMessage, mid) for mid in composed)
        if m is not None and str(m.status) == MessageStatus.DRAFT
    ]
    slots = plan_send_times(settings, len(drafts))
    for message, slot in zip(drafts, slots, strict=False):
        enqueue(session, message, scheduled_at=slot)
        stage.bump("queued")
    session.commit()
    log.info("compose/queue stage complete", extra=safe_extra(stage.counts))
    return stage

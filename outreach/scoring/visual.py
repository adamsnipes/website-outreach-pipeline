"""AI visual scoring.

Runs only when ``FEATURE_AI_SCORING`` is true and only for businesses whose
deterministic score already puts them in the candidate band -- there is no
point spending AI usage confirming that an obviously modern site is modern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.ai.base import AIProvider, AIValidationError, record_ai_usage
from outreach.config import Settings
from outreach.db import utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.models.ai_assessment import AIWebsiteAssessment
from outreach.models.business import Business
from outreach.models.enums import EventType
from outreach.models.scan import WebsiteScan
from outreach.scoring.schemas import AIWebsiteScore

log = get_logger(__name__)


@dataclass
class VisualScoringOutcome:
    scored: bool
    reason: str
    assessment: AIWebsiteAssessment | None = None
    ai_score: AIWebsiteScore | None = None


def should_ai_score(
    settings: Settings,
    qualification_status: str,
    *,
    already_assessed: bool,
    force: bool = False,
) -> tuple[bool, str]:
    """AI-usage gate.  Deterministic filtering does as much work as possible."""
    if not settings.features.ai_scoring:
        return False, "SKIPPED_FEATURE_DISABLED: FEATURE_AI_SCORING is off"
    if already_assessed and not force:
        return False, "already assessed (use --force to re-score)"
    if force:
        return True, "forced"
    if str(qualification_status).upper() not in settings.ai_candidate_statuses:
        return False, (
            f"deterministic status {qualification_status} is outside the AI candidate "
            f"set {sorted(settings.ai_candidate_statuses)}"
        )
    return True, "candidate for AI review"


def page_text_for_scoring(scan: WebsiteScan, limit: int = 6000) -> str:
    """Extracted page text saved with the scan, trimmed for the prompt."""
    from pathlib import Path

    from outreach.crawler.content import visible_text

    if not scan.homepage_html_path:
        return ""
    path = Path(scan.homepage_html_path)
    if not path.is_file():
        return ""
    try:
        return visible_text(path.read_text(encoding="utf-8", errors="replace"))[:limit]
    except OSError:  # pragma: no cover - disk issues
        return ""


def run_visual_scoring(
    session: Session,
    settings: Settings,
    provider: AIProvider,
    business: Business,
    scan: WebsiteScan,
    *,
    vertical: str = "local business",
) -> VisualScoringOutcome:
    """Invoke the AI scorer for one business and persist the assessment."""
    started_at = utcnow()
    result = provider.score_website(
        vertical=vertical,
        business_name=business.name,
        technical_findings=scan.technical_findings_json or {},
        page_text=page_text_for_scoring(scan),
        desktop_screenshot=scan.desktop_screenshot_path,
        mobile_screenshot=scan.mobile_screenshot_path,
    )
    record_ai_usage(session, result, business_id=business.id, started_at=started_at)

    if not result.success:
        log.warning(
            "ai visual scoring failed",
            extra={"business_id": business.id, "error": result.error},
        )
        return VisualScoringOutcome(False, result.error or "AI provider failed")

    try:
        score = AIProvider.validate(result, AIWebsiteScore)
    except AIValidationError as exc:
        log.warning(
            "ai visual scoring returned invalid output",
            extra={"business_id": business.id, "error": str(exc)},
        )
        # The failed call is already recorded; mark the audit row unsuccessful.
        result.success = False
        result.error = str(exc)
        record_ai_usage(session, result, business_id=business.id, started_at=started_at)
        return VisualScoringOutcome(False, f"invalid AI output: {exc}")

    assessment = AIWebsiteAssessment(
        business_id=business.id,
        scan_id=scan.id,
        provider=result.provider,
        model=result.model,
        visual_modernity=score.visual_modernity,
        mobile_visual_quality=score.mobile_visual_quality,
        conversion_quality=score.conversion_quality,
        trust_quality=score.trust_quality,
        dated_probability=score.dated_probability,
        redesign_opportunity=score.redesign_opportunity,
        issues_json=[issue.model_dump() for issue in score.issues],
        summary=score.summary,
        raw_output_json=result.parsed,
        prompt_version=result.prompt_version,
    )
    session.add(assessment)
    session.flush()
    record_event(
        session,
        "business",
        business.id,
        EventType.AI_SCORED,
        {
            "assessment_id": assessment.id,
            "ai_site_score": assessment.ai_site_score,
            "redesign_opportunity": score.redesign_opportunity,
            "provider": result.provider,
        },
    )
    log.info(
        "ai visual scoring complete",
        extra={
            "business_id": business.id,
            "ai_site_score": assessment.ai_site_score,
            "redesign_opportunity": score.redesign_opportunity,
        },
    )
    return VisualScoringOutcome(True, "scored", assessment, score)


def latest_assessment(session: Session, business_id: int) -> AIWebsiteAssessment | None:
    return session.scalars(
        select(AIWebsiteAssessment)
        .where(AIWebsiteAssessment.business_id == business_id)
        .order_by(AIWebsiteAssessment.id.desc())
        .limit(1)
    ).first()


def assessment_summary(assessment: AIWebsiteAssessment | None) -> dict[str, Any]:
    if assessment is None:
        return {}
    return {
        "ai_site_score": assessment.ai_site_score,
        "visual_modernity": assessment.visual_modernity,
        "mobile_visual_quality": assessment.mobile_visual_quality,
        "conversion_quality": assessment.conversion_quality,
        "trust_quality": assessment.trust_quality,
        "dated_probability": assessment.dated_probability,
        "redesign_opportunity": assessment.redesign_opportunity,
        "issues": assessment.issues_json or [],
        "summary": assessment.summary,
    }

"""Final qualification and build-candidate ranking.

Combines the deterministic score with the AI score when one exists (weights are
configurable; 60/40 by default).  When AI scoring is disabled the whole thing
still works from the deterministic score alone.

Contactability is tracked independently of site quality, because "their site is
terrible" and "we can actually reach them" are different questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from outreach.config import Settings
from outreach.models.ai_assessment import AIWebsiteAssessment
from outreach.models.enums import (
    ContactabilityStatus,
    MessageStatus,
    QualificationStatus,
    ScanStatus,
)

#: Consumer mailbox providers a real local business plausibly publishes.
#: Anything else that is *not* on the business's own domain is treated as a
#: third-party address (often injected or a vendor) and needs human review.
CONSUMER_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
        "hotmail.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
        "mac.com", "comcast.net", "verizon.net", "att.net", "cox.net",
        "sbcglobal.net", "bellsouth.net", "protonmail.com", "proton.me",
    }
)

#: Problem codes that most reliably translate into "your website is costing you
#: patients", ordered by how convincing they are in an outreach email.
HIGH_VALUE_PROBLEM_CODES = {
    "no_viewport_meta": 3.0,
    "mobile_horizontal_overflow": 3.0,
    "fixed_desktop_layout": 2.5,
    "no_responsive_css": 2.5,
    "unreadable_mobile_text": 2.5,
    "no_click_to_call": 2.2,
    "no_mobile_click_to_call": 2.0,
    "no_cta_above_fold": 2.0,
    "broken_images": 2.0,
    "broken_assets": 1.8,
    "no_cta": 1.8,
    "table_layout": 1.5,
    "no_https": 1.5,
    "mobile_nav_overflow": 1.4,
    "small_tap_targets": 1.2,
    "obsolete_html_tags": 1.0,
    "very_slow_first_paint": 1.5,
    "no_contact_form": 1.0,
}


@dataclass
class QualificationResult:
    qualification_status: QualificationStatus
    contactability_status: ContactabilityStatus
    combined_score: float | None
    deterministic_score: float | None
    ai_score: float | None
    weights: dict[str, float]
    build_priority: float
    is_build_candidate: bool
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    top_problems: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualification_status": str(self.qualification_status),
            "contactability_status": str(self.contactability_status),
            "combined_score": self.combined_score,
            "deterministic_score": self.deterministic_score,
            "ai_score": self.ai_score,
            "weights": self.weights,
            "build_priority": round(self.build_priority, 3),
            "is_build_candidate": self.is_build_candidate,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "top_problems": self.top_problems,
        }


def classify_score(settings: Settings, score: float) -> QualificationStatus:
    """Map a 0-100 site score onto the four qualification bands."""
    if score >= settings.site_score_good_threshold:
        return QualificationStatus.GOOD_SITE
    if score >= settings.site_score_borderline_threshold:
        return QualificationStatus.BORDERLINE
    if score >= settings.site_score_redesign_threshold:
        return QualificationStatus.REDESIGN_CANDIDATE
    return QualificationStatus.STRONG_REDESIGN_CANDIDATE


def combine_scores(
    settings: Settings,
    deterministic: float | None,
    ai: float | None,
) -> tuple[float | None, dict[str, float]]:
    """Weighted blend of deterministic and AI scores."""
    det_weight, ai_weight = settings.normalized_weights
    if deterministic is None and ai is None:
        return None, {"deterministic": det_weight, "ai": ai_weight}
    if ai is None:
        return round(float(deterministic or 0.0), 2), {"deterministic": 1.0, "ai": 0.0}
    if deterministic is None:
        return round(float(ai), 2), {"deterministic": 0.0, "ai": 1.0}
    combined = deterministic * det_weight + ai * ai_weight
    return round(combined, 2), {"deterministic": det_weight, "ai": ai_weight}


def email_is_third_party(email: str | None, site_domain: str | None) -> bool:
    """True when an address belongs to neither the business nor a consumer ISP."""
    if not email or "@" not in email:
        return True
    domain = email.rsplit("@", 1)[1].lower()
    if site_domain and domain == str(site_domain).lower():
        return False
    return domain not in CONSUMER_EMAIL_DOMAINS


def rank_problems(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Website problems ordered by how much they matter to a local business."""
    severity_weight = {"high": 1.6, "medium": 1.15, "low": 0.8}
    ranked: list[tuple[float, dict[str, Any]]] = []
    for finding in findings or []:
        code = str(finding.get("code") or "")
        weight = HIGH_VALUE_PROBLEM_CODES.get(code, 0.6)
        weight *= severity_weight.get(str(finding.get("severity") or "medium"), 1.0)
        ranked.append((weight, finding))
    ranked.sort(key=lambda item: -item[0])
    return [
        {
            "code": f.get("code"),
            "category": f.get("category"),
            "severity": f.get("severity"),
            "evidence": f.get("evidence"),
            "weight": round(w, 3),
        }
        for w, f in ranked
    ]


def qualify(
    settings: Settings,
    *,
    scan_status: str | None,
    deterministic_score: float | None,
    technical_findings: dict[str, Any] | None,
    assessment: AIWebsiteAssessment | None,
    has_website: bool,
    business_status: str | None,
    contacts: list[dict[str, Any]],
    site_domain: str | None,
    already_built: bool,
    already_messaged_status: str | None,
    suppressed: bool,
) -> QualificationResult:
    """Everything the pipeline needs to decide what to do with a business."""
    reasons: list[str] = []
    blockers: list[str] = []

    ai_score = assessment.ai_site_score if assessment else None
    combined, weights = combine_scores(settings, deterministic_score, ai_score)

    # -- site quality band ------------------------------------------------
    if not has_website:
        status = QualificationStatus.UNREACHABLE
        blockers.append("no website on record")
    elif scan_status in {
        ScanStatus.CONNECTION_ERROR, ScanStatus.TIMEOUT, ScanStatus.HTTP_ERROR,
        ScanStatus.FAILED, ScanStatus.NO_WEBSITE,
    }:
        status = QualificationStatus.UNREACHABLE
        blockers.append(f"website could not be scanned ({scan_status})")
    elif scan_status in {ScanStatus.BLOCKED, ScanStatus.ROBOTS_DISALLOWED}:
        status = QualificationStatus.UNREACHABLE
        blockers.append(f"crawling was declined by the site ({scan_status})")
    elif combined is None:
        status = QualificationStatus.UNKNOWN
        blockers.append("no score available yet")
    else:
        status = classify_score(settings, combined)
        reasons.append(
            f"combined score {combined} "
            f"(deterministic {deterministic_score}, ai {ai_score}, weights {weights})"
        )

    # -- contactability ---------------------------------------------------
    usable_contacts = [c for c in contacts if c.get("normalized_email")]
    if suppressed:
        contactability = ContactabilityStatus.SUPPRESSED
        blockers.append("business or address is suppressed")
    elif usable_contacts:
        contactability = ContactabilityStatus.CONTACTABLE
        reasons.append(f"{len(usable_contacts)} published email address(es)")
    else:
        contactability = ContactabilityStatus.NO_PUBLIC_EMAIL
        blockers.append("no publicly published email address was found")

    # -- build candidacy --------------------------------------------------
    findings = list((technical_findings or {}).get("findings") or [])
    top_problems = rank_problems(findings)[:6]

    operational = str(business_status or "OPERATIONAL").upper() in {
        "OPERATIONAL", "", "NONE", "UNKNOWN"
    }
    if not operational:
        blockers.append(f"business status is {business_status}")
    if already_built:
        blockers.append("a build already exists for this business")
    if already_messaged_status:
        blockers.append(f"an outreach message already exists ({already_messaged_status})")

    is_candidate = (
        status in {QualificationStatus.REDESIGN_CANDIDATE, QualificationStatus.STRONG_REDESIGN_CANDIDATE}
        and contactability == ContactabilityStatus.CONTACTABLE
        and operational
        and not already_built
        and not already_messaged_status
        and not suppressed
    )

    priority = _build_priority(
        combined=combined,
        top_problems=top_problems,
        assessment=assessment,
        contacts=usable_contacts,
        site_domain=site_domain,
        operational=operational,
        scan_status=scan_status,
    )

    return QualificationResult(
        qualification_status=status,
        contactability_status=contactability,
        combined_score=combined,
        deterministic_score=deterministic_score,
        ai_score=ai_score,
        weights=weights,
        build_priority=priority,
        is_build_candidate=is_candidate,
        reasons=reasons,
        blockers=blockers,
        top_problems=top_problems,
    )


def _build_priority(
    *,
    combined: float | None,
    top_problems: list[dict[str, Any]],
    assessment: AIWebsiteAssessment | None,
    contacts: list[dict[str, Any]],
    site_domain: str | None,
    operational: bool,
    scan_status: str | None,
) -> float:
    """Rank build candidates.

    Deliberately *not* just "lowest raw score first": a dead domain is a worse
    sales prospect than an active business with an ugly but working website.
    """
    if combined is None:
        return 0.0

    # Severity of the problems we can actually talk about in the email.
    problem_signal = min(1.0, sum(p["weight"] for p in top_problems) / 12.0)

    # Mobile and conversion problems convert best.
    mobile_signal = min(
        1.0,
        sum(
            p["weight"]
            for p in top_problems
            if p.get("category") in {"mobile", "conversion"}
        )
        / 8.0,
    )

    # How much room a redesign has, per the AI when we have an opinion.
    ai_signal = (assessment.redesign_opportunity or 0) / 100.0 if assessment else 0.5

    # Confidence that we can actually reach a human.
    best_contact = max((c.get("confidence") or 0.0) for c in contacts) if contacts else 0.0
    on_domain = any(
        not email_is_third_party(c.get("normalized_email"), site_domain) for c in contacts
    )
    contact_signal = min(1.0, best_contact + (0.1 if on_domain else -0.25))

    # Signs of life: a site that renders and a business marked operational.
    liveness = 1.0 if (operational and scan_status == ScanStatus.OK) else 0.25

    priority = (
        problem_signal * 0.28
        + mobile_signal * 0.22
        + ai_signal * 0.18
        + contact_signal * 0.20
        + liveness * 0.12
    )
    return round(max(0.0, min(1.0, priority)), 4)


def already_messaged(status: str | None) -> bool:
    """Whether an existing message means this business must not be re-contacted."""
    if not status:
        return False
    return str(status).upper() not in {MessageStatus.SKIPPED, MessageStatus.FAILED}

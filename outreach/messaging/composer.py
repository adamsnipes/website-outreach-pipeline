"""Outreach email composition.

Deterministic templates are the baseline and always work.  AI personalisation
runs only when ``FEATURE_EMAIL_COMPOSITION`` is on, and its output is checked
against the same honesty rules the templates follow:

* at most two genuine, browser-measured observations
* no fabricated relationship, urgency, statistics or credentials
* no ``Re:``/``Fwd:`` subject prefixes on a first-touch email
* the real preview URL, the real sender identity, the real opt-out sentence
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from outreach.ai.base import AIProvider, AIValidationError
from outreach.ai.schemas import EmailDraftSchema
from outreach.config import Settings
from outreach.logging import get_logger
from outreach.messaging.compliance import build_compliance_footer
from outreach.scoring.qualification import HIGH_VALUE_PROBLEM_CODES

log = get_logger(__name__)

#: Plain-English, defensible descriptions of the problems we actually measured.
#: The wording is deliberately modest: these are observations, not diagnoses.
OBSERVATION_PHRASES: dict[str, str] = {
    "no_viewport_meta": "the site isn't set up for phone screens, so it loads zoomed out on mobile",
    "mobile_horizontal_overflow": "the page is wider than a phone screen, so visitors have to pinch and scroll sideways",
    "fixed_desktop_layout": "the layout is locked to a desktop width, so it doesn't adapt on a phone",
    "no_responsive_css": "there's no mobile layout, so phone visitors get the desktop page shrunk down",
    "unreadable_mobile_text": "the text comes out very small on a phone",
    "no_click_to_call": "the phone number isn't tappable on mobile, so people have to copy it out by hand",
    "no_mobile_click_to_call": "there's no tap-to-call link on the mobile version",
    "no_cta_above_fold": "the appointment action is easy to miss without scrolling",
    "no_cta": "there's no clear next step for someone who wants to book",
    "broken_images": "a few images aren't loading",
    "broken_assets": "some of the page's files aren't loading",
    "no_https": "the site is served over plain HTTP, so browsers label it \"Not secure\"",
    "table_layout": "the page is built on an older table-based layout",
    "mobile_nav_overflow": "the menu runs off the edge of a phone screen",
    "small_tap_targets": "most of the links are smaller than a comfortable tap target",
    "very_slow_first_paint": "the homepage takes a while to show anything",
    "no_contact_form": "there's no contact form, only a phone number",
    "obsolete_html_tags": "the markup uses tags browsers stopped recommending years ago",
}

#: Phrases an AI draft may never contain.
FORBIDDEN_EMAIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fabricated relationship", re.compile(r"\b(?:we (?:met|spoke)|as (?:we )?discussed|per our (?:call|conversation)|you (?:asked|requested)|following up on our)\b", re.I)),
    ("fabricated effort", re.compile(r"\b(?:spent (?:hours|days)|i (?:have )?(?:personally )?(?:audited|reviewed) (?:your (?:entire )?site )?for hours|deep dive)\b", re.I)),
    ("fabricated referral", re.compile(r"\b(?:referred (?:me|us)|recommended (?:you|your practice)|a (?:friend|colleague|patient) of yours)\b", re.I)),
    ("false patient claim", re.compile(r"\b(?:i am|i'm|we are|we're) (?:a |your )?(?:patient|client) (?:of|at)\b", re.I)),
    ("invented urgency", re.compile(r"\b(?:act now|limited time|expires? (?:today|tomorrow|soon)|only \d+ spots?|last chance|24 hours only|before it'?s too late)\b", re.I)),
    ("invented statistics", re.compile(r"\b\d{1,3}%\s*(?:of|more|increase|fewer|drop|lift)\b", re.I)),
    ("ranking claims", re.compile(r"\b(?:google (?:rank|ranking)|seo score|page ?speed score of|ranking (?:#|number) ?\d)\b", re.I)),
    ("competitor claims", re.compile(r"\b(?:your competitors? (?:are|have|已)|other dentists in \w+ (?:are|have))\b", re.I)),
    ("reply prefix", re.compile(r"^\s*(?:re|fwd|fw)\s*:", re.I)),
]

MAX_OBSERVATIONS = 2


@dataclass
class ComposedEmail:
    subject: str
    body_text: str
    body_html: str
    observations: list[dict[str, Any]] = field(default_factory=list)
    method: str = "deterministic"
    warnings: list[str] = field(default_factory=list)
    ai_result: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body_text": self.body_text,
            "observations": self.observations,
            "method": self.method,
            "warnings": self.warnings,
        }


def select_observations(
    technical_findings: dict[str, Any] | None,
    ai_issues: list[dict[str, Any]] | None = None,
    *,
    limit: int = MAX_OBSERVATIONS,
) -> list[dict[str, Any]]:
    """The one or two most defensible, most relatable problems.

    Only findings we have a plain-English phrase for are eligible: if we cannot
    describe it honestly in one clause, we do not mention it.
    """
    findings = list((technical_findings or {}).get("findings") or [])
    severity_weight = {"high": 1.6, "medium": 1.15, "low": 0.8}
    scored: list[tuple[float, dict[str, Any]]] = []
    seen_codes: set[str] = set()

    for finding in findings:
        code = str(finding.get("code") or "")
        phrase = OBSERVATION_PHRASES.get(code)
        if not phrase or code in seen_codes:
            continue
        seen_codes.add(code)
        weight = HIGH_VALUE_PROBLEM_CODES.get(code, 0.5)
        weight *= severity_weight.get(str(finding.get("severity") or "medium"), 1.0)
        scored.append(
            (
                weight,
                {
                    "code": code,
                    "phrase": phrase,
                    "category": finding.get("category"),
                    "severity": finding.get("severity"),
                    "evidence": finding.get("evidence"),
                    "source": "deterministic",
                },
            )
        )

    scored.sort(key=lambda item: -item[0])
    chosen = [item[1] for item in scored[:limit]]

    # Prefer variety: two mobile complaints read as one complaint.
    if len(chosen) == 2 and chosen[0]["category"] == chosen[1]["category"]:
        alternative = next(
            (o for _, o in scored[2:] if o["category"] != chosen[0]["category"]), None
        )
        if alternative:
            chosen[1] = alternative

    if not chosen and ai_issues:
        for issue in ai_issues[:limit]:
            evidence = str(issue.get("evidence") or "").strip()
            if evidence:
                chosen.append(
                    {
                        "code": "ai_issue",
                        "phrase": evidence[:180].rstrip("."),
                        "category": issue.get("category"),
                        "severity": issue.get("severity"),
                        "evidence": evidence,
                        "source": "ai",
                    }
                )
    return chosen[:limit]


def _observation_sentence(observations: list[dict[str, Any]]) -> str:
    phrases = [o["phrase"] for o in observations if o.get("phrase")]
    if not phrases:
        return "a couple of things on the current site that are probably costing you calls"
    if len(phrases) == 1:
        return phrases[0]
    return f"{phrases[0]}, and {phrases[1]}"


def deterministic_email(
    settings: Settings,
    *,
    business_name: str,
    city: str,
    vertical: str,
    preview_url: str,
    observations: list[dict[str, Any]],
) -> ComposedEmail:
    """The baseline email.  Short, specific, and true."""
    plural = _plural_vertical(vertical)
    where = f" in {city}" if city else ""
    observation_text = _observation_sentence(observations)

    subject = f"A quicker version of the {business_name} site"
    if len(subject) > 78:
        subject = "A quicker version of your website"

    body = (
        f"Hi {business_name},\n"
        f"\n"
        f"I came across your practice while looking at {plural}{where} and noticed "
        f"{observation_text}.\n"
        f"\n"
        f"I put together a cleaner version to show what a rebuild could look like:\n"
        f"\n"
        f"{preview_url}\n"
        f"\n"
        f"It's a rough first pass using the information already on your site. If you "
        f"like the direction, I'm happy to finish it and get it live.\n"
        f"\n"
        f"{build_compliance_footer(settings)}\n"
    )
    return ComposedEmail(
        subject=subject,
        body_text=body,
        body_html=text_to_html(body, preview_url),
        observations=observations,
        method="deterministic",
    )


def check_email_honesty(subject: str, body: str) -> list[str]:
    """Reject drafts that make claims we cannot stand behind."""
    problems: list[str] = []
    for label, pattern in FORBIDDEN_EMAIL_PATTERNS:
        if label == "reply prefix":
            if pattern.search(subject or ""):
                problems.append("subject uses a Re:/Fwd: prefix on a first-touch email")
            continue
        match = pattern.search(body or "") or pattern.search(subject or "")
        if match:
            problems.append(f"{label}: {match.group(0)[:60]!r}")
    return problems


def compose(
    settings: Settings,
    *,
    provider: AIProvider | None,
    use_ai: bool,
    business_name: str,
    city: str,
    vertical: str,
    preview_url: str,
    technical_findings: dict[str, Any] | None,
    ai_issues: list[dict[str, Any]] | None = None,
) -> ComposedEmail:
    """Compose one outreach email, AI-personalised when enabled and honest."""
    observations = select_observations(technical_findings, ai_issues)
    baseline = deterministic_email(
        settings,
        business_name=business_name,
        city=city,
        vertical=vertical,
        preview_url=preview_url,
        observations=observations,
    )
    if not use_ai or provider is None:
        baseline.warnings.append("SKIPPED_FEATURE_DISABLED: FEATURE_EMAIL_COMPOSITION is off")
        return baseline

    result = provider.compose_email(
        business_name=business_name,
        vertical=vertical,
        city=city,
        observations=observations,
        preview_url=preview_url,
        sender_name=settings.sender_name,
        sender_company=settings.sender_company,
        unsubscribe_instruction=settings.unsubscribe_instruction,
    )
    baseline.ai_result = result
    if not result.success:
        baseline.warnings.append(f"AI composition failed, using template: {result.error}")
        return baseline
    try:
        draft = AIProvider.validate(result, EmailDraftSchema)
    except AIValidationError as exc:
        baseline.warnings.append(f"AI draft invalid, using template: {exc}")
        return baseline

    problems = check_email_honesty(draft.subject, draft.body)
    if problems:
        baseline.warnings.append(
            "AI draft rejected for unsupportable claims, using template: " + "; ".join(problems)
        )
        log.warning("ai email draft rejected", extra={"problems": problems})
        return baseline

    body = draft.body.strip()
    if preview_url and preview_url not in body:
        baseline.warnings.append("AI draft omitted the preview URL, using template")
        return baseline

    footer = build_compliance_footer(settings)
    if settings.unsubscribe_instruction.strip() not in body or (settings.sender_name and settings.sender_name not in body):
        body = f"{body}\n\n{footer}\n"

    return ComposedEmail(
        subject=draft.subject.strip() or baseline.subject,
        body_text=body,
        body_html=text_to_html(body, preview_url),
        observations=observations,
        method="ai",
        warnings=baseline.warnings,
        ai_result=result,
    )


def text_to_html(text: str, preview_url: str = "") -> str:
    """A plain, well-behaved HTML alternative part.  No images, no tracking."""
    escaped = html.escape(text or "")
    if preview_url:
        escaped = escaped.replace(
            html.escape(preview_url),
            f'<a href="{html.escape(preview_url, quote=True)}">{html.escape(preview_url)}</a>',
        )
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", escaped) if p.strip()]
    rendered = "".join(
        f'<p style="margin:0 0 14px">{p.replace(chr(10), "<br>")}</p>' for p in paragraphs
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.6;color:#1a1a1a">'
        f"{rendered}</div>"
    )


def _plural_vertical(vertical: str) -> str:
    text = (vertical or "local business").strip().lower()
    if text.endswith("s"):
        return text
    if text.endswith("y") and text[-2:-1] not in "aeiou":
        return text[:-1] + "ies"
    return text + "s"

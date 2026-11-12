"""BusinessContent and SiteContent construction, with hallucination guardrails.

The flow is deliberately narrow:

    crawled evidence
      -> deterministic BusinessContent      (always available, no AI)
      -> optional AI BusinessContent         (validated against the evidence)
      -> deterministic SiteContent           (always available, no AI)
      -> optional AI SiteContent             (validated against BusinessContent)
      -> Jinja renderer

The AI may rewrite marketing copy.  It may never introduce a fact.  Anything it
returns that is not supported by the evidence corpus is dropped -- not flagged
for a human, dropped -- because an unverifiable claim on a prospect's own
website is worse than no claim at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from outreach.ai.base import AIProvider, AIValidationError
from outreach.ai.schemas import (
    BusinessContentSchema,
    BusinessHours,
    ServiceItem,
    SiteContentSchema,
    SiteHero,
    SiteSectionCopy,
    TeamMember,
)
from outreach.logging import get_logger
from outreach.normalize import normalize_email, normalize_phone

log = get_logger(__name__)

#: Claim shapes the AI is forbidden from producing.  Any sentence or bullet
#: matching one of these is removed unless the exact phrase is in the evidence.
FORBIDDEN_CLAIM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("years_in_business", re.compile(r"\b(\d{1,3})\+?\s*(?:\+\s*)?years?\b", re.I)),
    ("since_year", re.compile(r"\bsince\s+(19|20)\d{2}\b", re.I)),
    ("founded", re.compile(r"\b(founded|established|opened)\s+(in\s+)?(19|20)\d{2}\b", re.I)),
    ("awards", re.compile(r"\b(award[- ]?winning|award|voted|top[- ]rated|best of|#\s?1|number one)\b", re.I)),
    ("certifications", re.compile(r"\b(certified|accredited|board[- ]certified|fellow of|diplomate)\b", re.I)),
    ("memberships", re.compile(r"\b(member of the|proud member|association member)\b", re.I)),
    ("reviews", re.compile(r"\b(\d[\d,.]*\s*(?:\+|plus)?\s*(?:5[- ]star|five[- ]star|reviews?|ratings?)|rated\s+\d)", re.I)),
    ("patient_counts", re.compile(r"\b(thousands|hundreds|\d[\d,]{2,})\s+(?:of\s+)?(?:happy\s+)?(patients|families|smiles)\b", re.I)),
    ("pricing", re.compile(r"(\$\s?\d|\bfree consultation\b|\bno cost\b|\bstarting at\b|\baffordable rates\b)", re.I)),
    ("insurance", re.compile(r"\b(we accept|accepting|in[- ]network|out[- ]of[- ]network)\b.{0,24}\b(insurance|ppo|hmo|delta dental|medicaid|medicare)\b", re.I)),
    ("financing", re.compile(r"\b(financing|payment plans?|carecredit|0%\s*apr|interest[- ]free)\b", re.I)),
    ("emergency", re.compile(r"\b(24/7|24 hours|same[- ]day emergency|emergency (?:care|appointments?|service)|walk[- ]ins welcome)\b", re.I)),
    ("guarantees", re.compile(r"\b(guarantee[ds]?|warranty|money[- ]back|satisfaction guaranteed|risk[- ]free)\b", re.I)),
    ("locations", re.compile(r"\b(\d+|multiple|several|two|three|four)\s+(?:convenient\s+)?locations?\b", re.I)),
    ("superlatives", re.compile(r"\b(leading|premier|the best|world[- ]class|state[- ]of[- ]the[- ]art|cutting[- ]edge)\b", re.I)),
]


@dataclass
class GuardReport:
    """What the guardrails removed and why."""

    dropped: list[dict[str, str]] = field(default_factory=list)
    kept_from_ai: list[str] = field(default_factory=list)
    fell_back: bool = False
    notes: list[str] = field(default_factory=list)

    def drop(self, field_name: str, value: str, reason: str) -> None:
        self.dropped.append({"field": field_name, "value": str(value)[:200], "reason": reason})

    def to_dict(self) -> dict[str, Any]:
        return {
            "dropped": self.dropped[:60],
            "kept_from_ai": self.kept_from_ai,
            "fell_back_to_deterministic": self.fell_back,
            "notes": self.notes,
        }


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


class EvidenceCorpus:
    """Everything the crawler actually saw, in a form that is cheap to check."""

    def __init__(self, page_texts: list[str], extra: list[str] | None = None) -> None:
        blob = " ".join([*page_texts, *(extra or [])])
        self.normalized = _normalize_text(blob)
        self.tokens = set(self.normalized.split())

    def contains_phrase(self, phrase: str, *, min_ratio: float = 0.8) -> bool:
        """Is this phrase (or nearly all of its words) present in the evidence?"""
        normalized = _normalize_text(phrase)
        if not normalized:
            return False
        if normalized in self.normalized:
            return True
        words = [w for w in normalized.split() if len(w) > 2]
        if not words:
            return False
        hits = sum(1 for w in words if w in self.tokens)
        return hits / len(words) >= min_ratio

    def supports_sentence(self, sentence: str) -> tuple[bool, str]:
        """Reject a sentence that makes a forbidden claim without evidence."""
        for label, pattern in FORBIDDEN_CLAIM_PATTERNS:
            match = pattern.search(sentence or "")
            if match and not self.contains_phrase(match.group(0), min_ratio=1.0):
                return False, f"unsupported {label} claim: {match.group(0)!r}"
        return True, ""

    def filter_prose(self, text: str) -> tuple[str, list[str]]:
        """Drop only the sentences that make unsupported claims."""
        if not text:
            return "", []
        kept, reasons = [], []
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            ok, reason = self.supports_sentence(sentence)
            if ok:
                kept.append(sentence)
            else:
                reasons.append(reason)
        return " ".join(kept).strip(), reasons


# ---------------------------------------------------------------------------
# Deterministic BusinessContent
# ---------------------------------------------------------------------------
def deterministic_business_content(
    *,
    business_name: str,
    facts: dict[str, Any],
    contacts: list[dict[str, Any]],
    assets: dict[str, Any] | None = None,
    website_url: str | None = None,
) -> BusinessContentSchema:
    """Build BusinessContent from crawled facts alone.  Always available."""
    assets = assets or {}
    phones = list(facts.get("phones") or [])
    address_lines = list(facts.get("address_lines") or [])
    best_contact = contacts[0] if contacts else None

    services = [
        ServiceItem(name=name, description="", source_url=_first_source(facts, "services"))
        for name in _dedupe_preserve(facts.get("services") or [])[:10]
    ]
    hours = _dedupe_hours(facts.get("hours") or [])
    team = [
        TeamMember(
            name=str(person.get("name") or ""),
            credentials=str(person.get("credential") or ""),
            source_url=_first_source(facts, f"person:{person.get('name')}"),
        )
        for person in (facts.get("people") or [])[:8]
        if person.get("name")
    ]
    about_paragraphs = [
        cleaned
        for cleaned in (_tidy_prose(p) for p in (facts.get("about_paragraphs") or []))
        if len(cleaned) > 80
    ][:3]

    logo = (assets.get("logo") or {}) if isinstance(assets.get("logo"), dict) else {}
    photos = [p for p in (assets.get("photos") or []) if isinstance(p, dict)]

    return BusinessContentSchema(
        business_name=str(facts.get("business_name") or business_name or ""),
        phone=phones[0] if phones else "",
        email=str(best_contact.get("normalized_email")) if best_contact else "",
        address=address_lines[0] if address_lines else "",
        city=str(facts.get("city") or ""),
        state=str(facts.get("state") or ""),
        postal_code=str(facts.get("postal_code") or ""),
        hours=hours,
        services=services,
        about=" ".join(about_paragraphs)[:2500],
        team=team,
        credentials=list(_dedupe_preserve(facts.get("credentials") or []))[:8],
        logo_url=str(logo.get("local_path") or logo.get("url") or ""),
        image_urls=[str(p.get("local_path") or p.get("url") or "") for p in photos][:6],
        existing_website=str(website_url or ""),
    )


_DAY_ALIASES = {
    "mon": "mon", "monday": "mon", "mo": "mon",
    "tue": "tue", "tues": "tue", "tuesday": "tue", "tu": "tue",
    "wed": "wed", "weds": "wed", "wednesday": "wed", "we": "wed",
    "thu": "thu", "thur": "thu", "thurs": "thu", "thursday": "thu", "th": "thu",
    "fri": "fri", "friday": "fri", "fr": "fri",
    "sat": "sat", "saturday": "sat", "sa": "sat",
    "sun": "sun", "sunday": "sun", "su": "sun",
}


def _hours_key(days: str, hours: str) -> str:
    """Collapse "Monday - Thursday 8:00 am - 5:00 pm" and "Mon-Thu 8:00am-5:00pm"."""
    connectors = {"and", "to", "through", "thru", "till", "until", "am", "pm"}
    day_tokens = [
        _DAY_ALIASES.get(token, token)
        for token in re.findall(r"[a-z]+", str(days or "").lower())
        if token not in connectors
    ]
    time_tokens = re.findall(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?|closed", str(hours or "").lower())
    # NB: a plain .rstrip(":00") would also eat the zeros in "1000".
    normalised_times = [re.sub(r":00$", "", t.replace(" ", "")) or t for t in time_tokens]
    return "|".join(day_tokens) + "@" + "|".join(normalised_times)


def _dedupe_hours(rows: list[Any], limit: int = 8) -> list[BusinessHours]:
    """One row per distinct day/time pairing, first spelling wins."""
    seen: set[str] = set()
    out: list[BusinessHours] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("days"):
            continue
        key = _hours_key(str(row.get("days", "")), str(row.get("hours", "")))
        if key in seen:
            continue
        seen.add(key)
        out.append(BusinessHours(days=str(row["days"]), hours=str(row.get("hours", ""))))
        if len(out) >= limit:
            break
    return out


def _first_source(facts: dict[str, Any], key: str) -> str:
    evidence = facts.get("_evidence") or {}
    entries = evidence.get(key) or []
    if entries and isinstance(entries[0], dict):
        return str(entries[0].get("source_url") or "")
    return ""


def _dedupe_preserve(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Guardrails over AI BusinessContent
# ---------------------------------------------------------------------------
def guard_business_content(
    ai_content: BusinessContentSchema,
    baseline: BusinessContentSchema,
    corpus: EvidenceCorpus,
    *,
    allowed_emails: set[str] | None = None,
    allowed_phones: set[str] | None = None,
) -> tuple[BusinessContentSchema, GuardReport]:
    """Strip every AI claim the crawled evidence does not support."""
    report = GuardReport()
    guarded = ai_content.model_copy(deep=True)

    # Contact details may only ever be ones we actually extracted.
    allowed_emails = allowed_emails or ({baseline.email} if baseline.email else set())
    allowed_phones = allowed_phones or ({baseline.phone} if baseline.phone else set())

    email = normalize_email(guarded.email) or ""
    if email and email not in {e for e in allowed_emails if e}:
        report.drop("email", guarded.email, "address was not found on the prospect's website")
        guarded.email = baseline.email
    else:
        guarded.email = email or baseline.email

    phone = normalize_phone(guarded.phone) or ""
    if phone and phone not in {p for p in allowed_phones if p}:
        report.drop("phone", guarded.phone, "number was not found on the prospect's website")
        guarded.phone = baseline.phone
    else:
        guarded.phone = phone or baseline.phone

    # Team: a person must be named in the evidence.
    kept_team: list[TeamMember] = []
    for person in guarded.team:
        if not person.name.strip():
            continue
        if not corpus.contains_phrase(person.name, min_ratio=1.0):
            report.drop("team", person.name, "person is not named anywhere in the crawled pages")
            continue
        bio, dropped_reasons = corpus.filter_prose(person.bio)
        for reason in dropped_reasons:
            report.drop(f"team[{person.name}].bio", person.bio, reason)
        person.bio = bio
        if person.credentials and not corpus.contains_phrase(person.credentials, min_ratio=1.0):
            report.drop(
                f"team[{person.name}].credentials", person.credentials, "credential not evidenced"
            )
            person.credentials = ""
        kept_team.append(person)
    guarded.team = kept_team

    # Services: the name must appear in the evidence.
    kept_services: list[ServiceItem] = []
    for service in guarded.services:
        if not service.name.strip():
            continue
        if not corpus.contains_phrase(service.name):
            report.drop("services", service.name, "service is not mentioned on the website")
            continue
        description, dropped_reasons = corpus.filter_prose(service.description)
        for reason in dropped_reasons:
            report.drop(f"services[{service.name}]", service.description, reason)
        service.description = description
        kept_services.append(service)
    guarded.services = kept_services or baseline.services

    # Credentials must be evidenced.
    kept_credentials = []
    for credential in guarded.credentials:
        if corpus.contains_phrase(credential, min_ratio=1.0):
            kept_credentials.append(credential)
        else:
            report.drop("credentials", credential, "credential not evidenced")
    guarded.credentials = kept_credentials

    # Free prose is sentence-filtered.
    about, reasons = corpus.filter_prose(guarded.about)
    for reason in reasons:
        report.drop("about", guarded.about[:120], reason)
    guarded.about = about or baseline.about

    tagline_ok, tagline_reason = corpus.supports_sentence(guarded.tagline)
    if not tagline_ok:
        report.drop("tagline", guarded.tagline, tagline_reason)
        guarded.tagline = ""

    # Structural facts stay pinned to what we crawled.
    guarded.address = guarded.address or baseline.address
    guarded.city = guarded.city or baseline.city
    guarded.state = guarded.state or baseline.state
    guarded.postal_code = guarded.postal_code or baseline.postal_code
    guarded.business_name = guarded.business_name or baseline.business_name
    guarded.logo_url = baseline.logo_url          # assets are never AI-chosen
    guarded.image_urls = baseline.image_urls
    guarded.existing_website = baseline.existing_website
    if not guarded.hours:
        guarded.hours = baseline.hours

    report.kept_from_ai = [
        name
        for name, value in (
            ("about", guarded.about),
            ("services", guarded.services),
            ("team", guarded.team),
            ("tagline", guarded.tagline),
        )
        if value
    ]
    return guarded, report


def build_business_content(
    *,
    provider: AIProvider | None,
    use_ai: bool,
    vertical: str,
    business_name: str,
    facts: dict[str, Any],
    evidence: dict[str, Any],
    contacts: list[dict[str, Any]],
    assets: dict[str, Any] | None,
    website_url: str | None,
    page_texts: list[str],
) -> tuple[BusinessContentSchema, GuardReport, Any]:
    """Deterministic content, optionally refined by a guarded AI pass."""
    baseline = deterministic_business_content(
        business_name=business_name,
        facts={**facts, "_evidence": evidence},
        contacts=contacts,
        assets=assets,
        website_url=website_url,
    )
    report = GuardReport()
    if not use_ai or provider is None:
        report.notes.append("SKIPPED_FEATURE_DISABLED: FEATURE_AI_CONTENT_EXTRACTION is off")
        report.fell_back = True
        return baseline, report, None

    result = provider.extract_business_content(
        vertical=vertical,
        business_name=business_name,
        deterministic_facts=facts,
        evidence=evidence,
        page_text=" ".join(page_texts)[:12000],
    )
    if not result.success:
        report.notes.append(f"AI extraction failed, using deterministic content: {result.error}")
        report.fell_back = True
        return baseline, report, result
    try:
        ai_content = AIProvider.validate(result, BusinessContentSchema)
    except AIValidationError as exc:
        report.notes.append(f"AI extraction output invalid, using deterministic content: {exc}")
        report.fell_back = True
        return baseline, report, result

    corpus = EvidenceCorpus(page_texts)
    guarded, guard_report = guard_business_content(
        ai_content,
        baseline,
        corpus,
        allowed_emails={c["normalized_email"] for c in contacts if c.get("normalized_email")},
        allowed_phones=set(facts.get("phones") or []),
    )
    guard_report.notes.extend(report.notes)
    return guarded, guard_report, result


# ---------------------------------------------------------------------------
# SiteContent
# ---------------------------------------------------------------------------
def deterministic_site_content(content: BusinessContentSchema) -> SiteContentSchema:
    """Template copy assembled from facts only.  Always available, never empty."""
    name = content.business_name or "the practice"
    locality = ", ".join([p for p in [content.city, content.state] if p])
    display_phone = format_phone_display(content.phone)

    if locality:
        headline = f"Thoughtful dental care in {content.city or locality}"
    else:
        headline = "Thoughtful dental care, close to home"

    sub_parts: list[str] = []
    if content.services:
        top = [_inline_case(s.name) for s in content.services[:3]]
        sub_parts.append(f"{name} offers {_english_join(top)}.")
    if content.phone:
        sub_parts.append("Call the office and we will find a time that works.")
    subheadline = " ".join(sub_parts)[:200]

    intro_body = _tidy_prose(content.about) or (
        f"{name} is a local dental practice"
        + (f" in {content.city}, {content.state}." if content.city and content.state else ".")
    )

    why_points: list[str] = []
    if content.hours:
        why_points.append(f"Open {content.hours[0].days} {content.hours[0].hours}.".strip())
    if content.team:
        who = _english_join([p.name for p in content.team[:2]])
        why_points.append(f"You will be seen by {who}.")
    if content.address:
        why_points.append(f"Conveniently located at {content.address}.")
    if content.services and len(content.services) >= 4:
        why_points.append(
            f"{len(content.services)} services under one roof, from "
            f"{content.services[0].name.lower()} to {content.services[-1].name.lower()}."
        )

    meta_title = f"{name}" + (f" | Dentist in {content.city}, {content.state}" if content.city and content.state else "")
    meta_description = (
        subheadline
        or (content.about[:150] if content.about else f"{name} - dental practice information.")
    )

    return SiteContentSchema(
        hero=SiteHero(
            headline=headline,
            subheadline=subheadline,
            primary_cta_label="Call the office" if content.phone else "Email the practice",
            secondary_cta_label="See our services" if content.services else "",
        ),
        intro=SiteSectionCopy(heading=f"About {name}", body=intro_body),
        services_heading="What we offer",
        service_blurbs={s.name: s.description for s in content.services if s.description},
        why_choose_points=why_points[:4],
        team_heading="Meet the team",
        cta_section=SiteSectionCopy(
            heading="Ready to book a visit?",
            body=(
                f"Call {display_phone} and we will get you scheduled."
                if display_phone
                else "Get in touch and we will get you scheduled."
            ),
        ),
        meta_title=meta_title[:180],
        meta_description=meta_description[:300],
    )


def guard_site_content(
    ai_site: SiteContentSchema,
    baseline: SiteContentSchema,
    content: BusinessContentSchema,
    corpus: EvidenceCorpus,
) -> tuple[SiteContentSchema, GuardReport]:
    """Keep the AI's phrasing, drop anything that asserts a new fact."""
    report = GuardReport()
    guarded = ai_site.model_copy(deep=True)
    service_names = {s.name.strip().lower() for s in content.services}

    def clean(label: str, text: str, fallback: str) -> str:
        if not text:
            return fallback
        ok, reason = corpus.supports_sentence(text)
        if not ok:
            report.drop(label, text, reason)
            return fallback
        filtered, reasons = corpus.filter_prose(text)
        for r in reasons:
            report.drop(label, text, r)
        return filtered or fallback

    guarded.hero.headline = clean("hero.headline", guarded.hero.headline, baseline.hero.headline)
    guarded.hero.subheadline = clean(
        "hero.subheadline", guarded.hero.subheadline, baseline.hero.subheadline
    )
    guarded.hero.primary_cta_label = (
        guarded.hero.primary_cta_label or baseline.hero.primary_cta_label
    )
    guarded.intro.heading = guarded.intro.heading or baseline.intro.heading
    guarded.intro.body = clean("intro.body", guarded.intro.body, baseline.intro.body)
    guarded.cta_section.heading = guarded.cta_section.heading or baseline.cta_section.heading
    guarded.cta_section.body = clean(
        "cta.body", guarded.cta_section.body, baseline.cta_section.body
    )

    kept_points: list[str] = []
    for point in guarded.why_choose_points:
        ok, reason = corpus.supports_sentence(point)
        if not ok:
            report.drop("why_choose_points", point, reason)
            continue
        kept_points.append(point)
    guarded.why_choose_points = kept_points or baseline.why_choose_points

    blurbs: dict[str, str] = {}
    for key, value in guarded.service_blurbs.items():
        if key.strip().lower() not in service_names:
            report.drop("service_blurbs", key, "blurb refers to a service the business does not list")
            continue
        ok, reason = corpus.supports_sentence(value)
        if not ok:
            report.drop(f"service_blurbs[{key}]", value, reason)
            continue
        blurbs[key] = value
    guarded.service_blurbs = blurbs or baseline.service_blurbs

    guarded.meta_title = guarded.meta_title or baseline.meta_title
    guarded.meta_description = clean(
        "meta_description", guarded.meta_description, baseline.meta_description
    )
    guarded.services_heading = guarded.services_heading or baseline.services_heading
    guarded.team_heading = guarded.team_heading or baseline.team_heading
    return guarded, report


def build_site_content(
    *,
    provider: AIProvider | None,
    use_ai: bool,
    vertical: str,
    content: BusinessContentSchema,
    page_texts: list[str],
) -> tuple[SiteContentSchema, GuardReport, Any]:
    """Deterministic template copy, optionally refined by a guarded AI pass."""
    baseline = deterministic_site_content(content)
    report = GuardReport()
    if not use_ai or provider is None:
        report.notes.append("SKIPPED_FEATURE_DISABLED: FEATURE_AI_CONTENT_EXTRACTION is off")
        report.fell_back = True
        return baseline, report, None

    result = provider.generate_site_content(
        vertical=vertical, business_content=content.model_dump()
    )
    if not result.success:
        report.notes.append(f"AI copy generation failed, using deterministic copy: {result.error}")
        report.fell_back = True
        return baseline, report, result
    try:
        ai_site = AIProvider.validate(result, SiteContentSchema)
    except AIValidationError as exc:
        report.notes.append(f"AI copy invalid, using deterministic copy: {exc}")
        report.fell_back = True
        return baseline, report, result

    # The corpus for copy is the business's own facts plus the crawled pages:
    # the AI may rephrase those, and nothing else.
    corpus = EvidenceCorpus(
        page_texts,
        extra=[
            content.about,
            content.business_name,
            *[s.name for s in content.services],
            *[f"{h.days} {h.hours}" for h in content.hours],
            *[p.name for p in content.team],
            content.address,
        ],
    )
    guarded, guard_report = guard_site_content(ai_site, baseline, content, corpus)
    guard_report.notes.extend(report.notes)
    return guarded, guard_report, result


def _english_join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _inline_case(text: str) -> str:
    """Lowercase a proper-noun-free label so it reads naturally mid-sentence."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    # Leave acronyms and already-lowercase words alone.
    words = stripped.split()
    if len(words) == 1 and stripped.isupper():
        return stripped
    return stripped[0].lower() + stripped[1:]


#: Sentences about the *old* website itself. True of the prospect's current
#: site, meaningless (and embarrassing) on the replacement we are proposing.
_SITE_META_PATTERNS = re.compile(
    r"(excuse our appearance|under construction|coming soon|site (?:is )?(?:last )?updated"
    r"|last updated|check back (?:soon|later)|new (?:web ?site|website) (?:is )?coming"
    r"|temporarily unavailable|currently unavailable|we are updating|redesigning our"
    r"|this (?:web ?site|page) (?:is|was)|copyright \d{4}|all rights reserved"
    r"|web ?site (?:design(?:ed)? )?by|powered by|best viewed (?:in|with)"
    r"|please (?:enable|upgrade) (?:javascript|your browser)|click here to)",
    re.I,
)


def _tidy_prose(text: str) -> str:
    """Clean up artefacts from scraping real page copy.

    Drops sentences that talk about the prospect's *existing* website -- stale
    "last updated" notes, under-construction apologies, designer credits --
    which are facts about a site we are replacing, not about the business.
    """
    if not text:
        return ""
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", str(text))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    kept = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
        if sentence.strip() and not _SITE_META_PATTERNS.search(sentence)
    ]
    return " ".join(kept).strip()


def format_phone_display(raw: str | None) -> str:
    """``+16025550134`` -> ``(602) 555-0134``; anything unparseable passes through."""
    normalized = normalize_phone(raw)
    if not normalized:
        return str(raw or "").strip()
    digits = normalized.lstrip("+")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return normalized

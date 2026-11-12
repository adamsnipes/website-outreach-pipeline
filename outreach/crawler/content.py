"""Deterministic, evidence-backed extraction of business facts from HTML.

This runs with no AI at all.  Every non-trivial claim it produces carries the
URL it came from and the snippet it came from, so a human (or a later AI step)
can check it.  Nothing here invents anything: if a fact is not literally on the
page, it is simply absent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from outreach.normalize import normalize_phone

_WHITESPACE = re.compile(r"\s+")

PHONE_TEXT_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})(?!\d)"
)

ADDRESS_RE = re.compile(
    r"(?<![\d\-])(\d{1,6}\s+[A-Z0-9][A-Za-z0-9.,'#\- ]{3,60}?"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|"
    r"Way|Court|Ct\.?|Circle|Cir\.?|Place|Pl\.?|Parkway|Pkwy\.?|Highway|Hwy\.?|Suite|Ste\.?|Trail|Terrace)"
    r"[A-Za-z0-9.,'#\- ]{0,40})[,\s]+([A-Za-z .'-]{2,40}),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)"
)

HOURS_RE = re.compile(
    r"((?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[A-Za-z]*"
    r"(?:\s*(?:-|–|—|to|through|and|&|,)\s*(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[A-Za-z]*)*)"
    r"\s*[:\-–—]?\s*"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?|Closed|closed|CLOSED)",
)

#: Person + dental credential.  Requires an explicit title or credential so we
#: never turn a random capitalised phrase into a doctor.
PERSON_RE = re.compile(
    r"\b(Dr\.?|Doctor)\s+([A-Z][A-Za-z'’\-]{1,20}(?:\s+[A-Z][A-Za-z'’\-]{1,20}){0,2})"
    r"(?:,?\s*(DDS|DMD|MD|D\.D\.S\.?|D\.M\.D\.?|RDH|DVM|MS|PhD))?"
)
CREDENTIAL_RE = re.compile(r"\b(DDS|DMD|D\.D\.S\.|D\.M\.D\.|RDH|MAGD|FAGD|MS|MD)\b")

SERVICE_STOPWORDS = {
    "home", "about", "about us", "contact", "contact us", "services", "our services",
    "read more", "learn more", "more info", "click here", "back to home", "menu",
    "privacy policy", "terms", "sitemap", "appointments", "patient forms", "forms",
    "insurance", "blog", "news", "gallery", "photo gallery", "links", "staff",
    "meet the staff", "new patients", "our team", "team", "location", "hours",
}

DENTAL_SERVICE_HINTS = re.compile(
    r"(clean|exam|x-?ray|filling|crown|bridge|denture|partial|extraction|whiten|veneer|"
    r"implant|root canal|periodont|orthodont|invisalign|braces|sealant|bonding|"
    r"cosmetic|preventive|restorative|emergency|sedation|hygien|night ?guard|"
    r"children|pediatric|oral surgery|gum|wisdom|checkup|check-up)",
    re.I,
)


@dataclass
class Evidence:
    """Where a claim came from."""

    source_url: str
    snippet: str
    method: str = "text"

    def to_dict(self) -> dict[str, str]:
        return {
            "source_url": self.source_url,
            "snippet": _WHITESPACE.sub(" ", self.snippet).strip()[:240],
            "method": self.method,
        }


@dataclass
class ExtractedFacts:
    """Deterministically extracted business facts + their evidence."""

    business_name: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    address_lines: list[str] = field(default_factory=list)
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    hours: list[dict[str, str]] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    about_paragraphs: list[str] = field(default_factory=list)
    people: list[dict[str, str]] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    service_area: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    meta_descriptions: list[str] = field(default_factory=list)
    evidence: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def add_evidence(self, claim: str, ev: Evidence) -> None:
        self.evidence.setdefault(claim, [])
        payload = ev.to_dict()
        if payload not in self.evidence[claim]:
            self.evidence[claim].append(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_name": self.business_name,
            "phones": self.phones,
            "emails": self.emails,
            "address_lines": self.address_lines,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
            "hours": self.hours,
            "services": self.services,
            "about_paragraphs": self.about_paragraphs,
            "people": self.people,
            "credentials": self.credentials,
            "service_area": self.service_area,
            "social_links": self.social_links,
            "titles": self.titles,
            "meta_descriptions": self.meta_descriptions,
        }


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return _WHITESPACE.sub(" ", soup.get_text(" ", strip=True))


def _iter_jsonld(soup: BeautifulSoup) -> list[Any]:
    blocks: list[Any] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            blocks.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return blocks


def _flatten_jsonld(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            out.extend(_flatten_jsonld(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_flatten_jsonld(item))
    return out


def extract_facts_from_page(
    html: str, url: str, facts: ExtractedFacts | None = None
) -> ExtractedFacts:
    """Merge one page's facts into ``facts`` (created if not supplied)."""
    facts = facts or ExtractedFacts()
    soup = BeautifulSoup(html or "", "lxml")
    text = visible_text(html)

    if soup.title and soup.title.string:
        title = _WHITESPACE.sub(" ", soup.title.string).strip()
        if title and title not in facts.titles:
            facts.titles.append(title[:300])
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if isinstance(meta_desc, Tag) and meta_desc.get("content"):
        value = _WHITESPACE.sub(" ", str(meta_desc["content"])).strip()
        if value and value not in facts.meta_descriptions:
            facts.meta_descriptions.append(value[:500])

    # ---- structured data first: it is the most trustworthy source ----
    for block in _iter_jsonld(soup):
        for node in _flatten_jsonld(block):
            node_type = node.get("@type")
            types = {node_type} if isinstance(node_type, str) else set(node_type or [])
            if types & {
                "Dentist", "LocalBusiness", "MedicalBusiness", "Organization",
                "MedicalClinic", "DentalClinic", "HealthAndBeautyBusiness", "ProfessionalService",
            }:
                name = node.get("name")
                if isinstance(name, str) and name.strip() and not facts.business_name:
                    facts.business_name = name.strip()[:200]
                    facts.add_evidence("business_name", Evidence(url, name, "jsonld"))
                phone = node.get("telephone")
                if isinstance(phone, str):
                    _add_phone(facts, phone, url, "jsonld")
                address = node.get("address")
                if isinstance(address, dict):
                    _add_address_from_jsonld(facts, address, url)
                hours = node.get("openingHours") or node.get("openingHoursSpecification")
                if hours:
                    _add_hours_from_jsonld(facts, hours, url)

    # ---- headings ----
    h1 = soup.find("h1")
    if h1 is not None and not facts.business_name:
        candidate = _WHITESPACE.sub(" ", h1.get_text(" ", strip=True)).strip()
        if 2 < len(candidate) < 120 and not candidate.lower().startswith("welcome to our"):
            cleaned = re.sub(r"^welcome to\s+", "", candidate, flags=re.I).strip()
            facts.business_name = cleaned[:200]
            facts.add_evidence("business_name", Evidence(url, candidate, "h1"))

    # ---- phones ----
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.lower().startswith("tel:"):
            _add_phone(facts, href[4:], url, "tel-link")
    for match in PHONE_TEXT_RE.finditer(text):
        _add_phone(facts, match.group(0), url, "text", context=_context(text, match.start()))

    # ---- address ----
    for match in ADDRESS_RE.finditer(text):
        street, city, state, postal = (g.strip(" ,") for g in match.groups())
        line = f"{street}, {city}, {state} {postal}"
        if line not in facts.address_lines:
            facts.address_lines.append(line[:240])
            facts.add_evidence("address", Evidence(url, match.group(0), "text"))
        facts.city = facts.city or city
        facts.state = facts.state or state
        facts.postal_code = facts.postal_code or postal

    # ---- hours ----
    for match in HOURS_RE.finditer(text):
        entry = {
            "days": _WHITESPACE.sub(" ", match.group(1)).strip(),
            "hours": _WHITESPACE.sub(" ", match.group(2)).strip(),
        }
        if entry not in facts.hours and len(facts.hours) < 14:
            facts.hours.append(entry)
            facts.add_evidence("hours", Evidence(url, match.group(0), "text"))

    # ---- services ----
    for li in soup.find_all("li"):
        item = _WHITESPACE.sub(" ", li.get_text(" ", strip=True)).strip()
        if not (3 <= len(item) <= 70):
            continue
        if item.lower() in SERVICE_STOPWORDS:
            continue
        if li.find("a") and len(item) < 20 and not DENTAL_SERVICE_HINTS.search(item):
            continue  # nav list item
        if DENTAL_SERVICE_HINTS.search(item) and item not in facts.services:
            facts.services.append(item)
            facts.add_evidence("services", Evidence(url, item, "list-item"))
    for chunk in re.split(r"·|&middot;|·|\|", text):
        candidate = chunk.strip()
        if (
            3 <= len(candidate) <= 40
            and DENTAL_SERVICE_HINTS.search(candidate)
            and candidate not in facts.services
            and candidate.lower() not in SERVICE_STOPWORDS
        ):
            facts.services.append(candidate)
            facts.add_evidence("services", Evidence(url, candidate, "inline-list"))

    # ---- about copy ----
    for paragraph in soup.find_all("p"):
        content = _WHITESPACE.sub(" ", paragraph.get_text(" ", strip=True)).strip()
        if (
            80 <= len(content) <= 900
            and content not in facts.about_paragraphs
            and len(facts.about_paragraphs) < 12
        ):
            facts.about_paragraphs.append(content)
            facts.add_evidence("about", Evidence(url, content[:200], "paragraph"))

    # ---- people ----
    for match in PERSON_RE.finditer(text):
        name = f"Dr. {match.group(2).strip()}"
        credential = (match.group(3) or "").replace(".", "").upper() or None
        entry = {"name": name[:120], "credential": credential or ""}
        if entry not in facts.people and len(facts.people) < 12:
            facts.people.append(entry)
            facts.add_evidence(
                f"person:{name}", Evidence(url, _context(text, match.start()), "text")
            )
    _merge_people(facts)

    for match in CREDENTIAL_RE.finditer(text):
        credential = match.group(1).replace(".", "").upper()
        if credential not in facts.credentials:
            facts.credentials.append(credential)
            facts.add_evidence(
                f"credential:{credential}", Evidence(url, _context(text, match.start()), "text")
            )

    # ---- social ----
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if (
            re.search(r"(facebook|instagram|twitter|x\.com|yelp|linkedin|youtube)\.com", href, re.I)
            and href not in facts.social_links
            and len(facts.social_links) < 10
        ):
            facts.social_links.append(href[:300])

    return facts


def _merge_people(facts: ExtractedFacts) -> None:
    """Collapse "Dr. Vargas" into "Dr. Ellen Vargas" and keep the credential."""
    merged: list[dict[str, str]] = []
    for person in sorted(facts.people, key=lambda p: -len(p["name"])):
        surname = person["name"].split()[-1].lower()
        existing = next(
            (m for m in merged if m["name"].split()[-1].lower() == surname), None
        )
        if existing is None:
            merged.append(dict(person))
        elif person.get("credential") and not existing.get("credential"):
            existing["credential"] = person["credential"]
    facts.people = sorted(merged, key=lambda p: p["name"])


def _context(text: str, index: int, width: int = 90) -> str:
    return text[max(0, index - width) : index + width]


def _add_phone(facts: ExtractedFacts, raw: str, url: str, method: str, context: str = "") -> None:
    normalized = normalize_phone(raw)
    if not normalized:
        return
    if normalized not in facts.phones:
        if len(facts.phones) >= 6:
            return
        facts.phones.append(normalized)
    facts.add_evidence(f"phone:{normalized}", Evidence(url, context or raw, method))


def _add_address_from_jsonld(facts: ExtractedFacts, address: dict[str, Any], url: str) -> None:
    street = str(address.get("streetAddress") or "").strip()
    city = str(address.get("addressLocality") or "").strip()
    region = str(address.get("addressRegion") or "").strip()
    postal = str(address.get("postalCode") or "").strip()
    parts = [p for p in [street, city, f"{region} {postal}".strip()] if p]
    if not parts:
        return
    line = ", ".join(parts)
    if line not in facts.address_lines:
        facts.address_lines.append(line[:240])
        facts.add_evidence("address", Evidence(url, line, "jsonld"))
    facts.city = facts.city or (city or None)
    facts.state = facts.state or (region or None)
    facts.postal_code = facts.postal_code or (postal or None)


def _add_hours_from_jsonld(facts: ExtractedFacts, hours: Any, url: str) -> None:
    values: list[str] = []
    if isinstance(hours, str):
        values = [hours]
    elif isinstance(hours, list):
        for item in hours:
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                day = item.get("dayOfWeek")
                days = ", ".join(day) if isinstance(day, list) else str(day or "")
                opens, closes = item.get("opens"), item.get("closes")
                if days and opens and closes:
                    values.append(f"{days} {opens}-{closes}")
    for value in values:
        parts = value.split(None, 1)
        entry = {"days": parts[0], "hours": parts[1] if len(parts) > 1 else ""}
        if entry not in facts.hours and len(facts.hours) < 14:
            facts.hours.append(entry)
            facts.add_evidence("hours", Evidence(url, value, "jsonld"))

"""Public email extraction.

Only addresses the business itself published are ever stored.  Nothing is
guessed, patterned or enriched -- no ``firstname@domain.com`` fabrication, no
paid enrichment, no SMTP probing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from outreach.models.enums import ContactType
from outreach.normalize import normalize_domain, normalize_email

#: Text-scan pattern.  Deliberately stricter than the RFC so that image
#: filenames and tracking ids do not survive.
EMAIL_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9._%+-]{1,64})"
    r"\s?(?:@|\[at\]|\(at\)|&#64;|&commat;)\s?"
    r"([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9._%+-])"
)

#: Extensions that mean "this was a filename, not a mailbox".
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".css",
    ".js", ".json", ".php", ".html", ".htm", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".webm", ".pdf", ".zip", ".xml",
)

_JUNK_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "no_reply",
    "postmaster", "abuse", "mailer-daemon", "bounce", "bounces", "unsubscribe",
    "example", "test", "testing", "you", "your", "youremail", "yourname",
    "email", "name", "firstname", "lastname", "user", "username", "someone",
    "sample", "demo", "placeholder", "sentry", "wordpress", "wp",
}

_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "domain.com",
    "yourdomain.com", "yoursite.com", "email.com", "mysite.com", "sentry.io",
    "wixpress.com", "wordpress.com", "godaddy.com", "squarespace.com",
    "2x.png", "sentry-cdn.com", "schema.org", "w3.org", "googleapis.com",
}

_JUNK_LOCAL_PATTERNS = (
    re.compile(r"^[0-9a-f]{16,}$"),          # tracking / hash ids
    re.compile(r"^u\d{6,}$"),                # analytics ids
    re.compile(r"^\d{6,}$"),
)

#: local-part -> contact type, best first.
_TYPE_RULES: list[tuple[re.Pattern[str], ContactType]] = [
    (re.compile(r"^(owner|principal|founder)"), ContactType.OWNER),
    (re.compile(r"^(dr|doctor|drs)[._-]?"), ContactType.DOCTOR),
    (re.compile(r"(office|frontdesk|front[._-]?desk|reception|scheduling|appointments?)"), ContactType.OFFICE),
    (re.compile(r"^hello"), ContactType.HELLO),
    (re.compile(r"^(contact|contactus|inquiries|enquiries|newpatients?)"), ContactType.CONTACT),
    (re.compile(r"^(info|information|mail|admin|team|practice|smile)"), ContactType.INFO),
    (re.compile(r"^(support|help|billing|accounts?|insurance)"), ContactType.SUPPORT),
]

#: Ranking used when picking *the* address to write to.
CONTACT_TYPE_PRIORITY: dict[str, int] = {
    ContactType.OWNER: 100,
    ContactType.DOCTOR: 90,
    ContactType.OFFICE: 80,
    ContactType.HELLO: 72,
    ContactType.CONTACT: 70,
    ContactType.INFO: 60,
    ContactType.UNKNOWN: 30,
    ContactType.SUPPORT: 20,
}


@dataclass
class ExtractedContact:
    email: str
    normalized_email: str
    contact_type: ContactType
    source_url: str
    source_method: str
    confidence: float
    context: str = ""

    @property
    def domain(self) -> str | None:
        return normalize_domain(self.normalized_email)


@dataclass
class ContactExtractionResult:
    contacts: list[ExtractedContact] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    def best(self, site_domain: str | None = None) -> ExtractedContact | None:
        ranked = rank_contacts(self.contacts, site_domain)
        return ranked[0] if ranked else None


def classify_email(normalized: str) -> ContactType:
    local = normalized.split("@", 1)[0].lower()
    for pattern, contact_type in _TYPE_RULES:
        if pattern.search(local):
            return contact_type
    return ContactType.UNKNOWN


def is_junk_email(normalized: str) -> tuple[bool, str]:
    """``(is_junk, why)`` for an already-normalised address."""
    local, _, domain = normalized.partition("@")
    lowered_local = local.lower()
    if normalized.lower().endswith(_ASSET_SUFFIXES):
        return True, "looks like an asset filename"
    if domain in _JUNK_DOMAINS:
        return True, f"placeholder domain {domain}"
    if lowered_local in _JUNK_LOCAL_PARTS:
        return True, f"placeholder local part {lowered_local}"
    if any(lowered_local.startswith(p) for p in ("noreply", "no-reply", "donotreply", "do-not-reply")):
        return True, "no-reply mailbox"
    if any(pattern.match(lowered_local) for pattern in _JUNK_LOCAL_PATTERNS):
        return True, "looks like a tracking id"
    tld = domain.rsplit(".", 1)[-1]
    if not tld.isalpha() or len(tld) < 2:
        return True, f"implausible TLD .{tld}"
    if len(domain) > 253 or domain.startswith("-") or domain.endswith("-"):
        return True, "malformed domain"
    if "@" in local or ".." in normalized:
        return True, "malformed address"
    return False, ""


def _confidence(method: str, contact_type: ContactType, on_site_domain: bool) -> float:
    base = {
        "mailto": 0.9,
        "jsonld": 0.85,
        "microdata": 0.8,
        "text": 0.65,
        "obfuscated": 0.55,
    }.get(method, 0.5)
    if contact_type in {ContactType.OWNER, ContactType.DOCTOR, ContactType.OFFICE}:
        base += 0.05
    if on_site_domain:
        base += 0.05
    else:
        base -= 0.15
    return round(max(0.05, min(0.99, base)), 3)


def _add(
    out: dict[str, ExtractedContact],
    rejected: list[tuple[str, str]],
    raw: str,
    method: str,
    source_url: str,
    site_domain: str | None,
    context: str = "",
) -> None:
    normalized = normalize_email(raw)
    if not normalized:
        rejected.append((str(raw)[:120], "not a valid address"))
        return
    junk, why = is_junk_email(normalized)
    if junk:
        rejected.append((normalized, why))
        return
    contact_type = classify_email(normalized)
    on_site = bool(site_domain) and normalize_domain(normalized) == site_domain
    confidence = _confidence(method, contact_type, on_site)
    existing = out.get(normalized)
    if existing is None or confidence > existing.confidence:
        out[normalized] = ExtractedContact(
            email=raw.strip()[:320],
            normalized_email=normalized,
            contact_type=contact_type,
            source_url=source_url,
            source_method=method,
            confidence=confidence,
            context=context[:200],
        )


def _walk_jsonld(node: Any, sink: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in {"email", "contactpoint", "contactpoints"} and isinstance(value, str):
                sink.append(value)
            else:
                _walk_jsonld(value, sink)
    elif isinstance(node, list):
        for item in node:
            _walk_jsonld(item, sink)
    elif isinstance(node, str) and "@" in node and " " not in node.strip():
        pass  # bare strings are only trusted under an "email" key


def extract_contacts_from_html(
    html: str,
    source_url: str,
    site_domain: str | None = None,
) -> ContactExtractionResult:
    """Pull published addresses out of one page."""
    found: dict[str, ExtractedContact] = {}
    rejected: list[tuple[str, str]] = []
    soup = BeautifulSoup(html or "", "lxml")

    # 1. mailto: links -- the strongest signal.
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.lower().startswith("mailto:"):
            for piece in href[7:].split(","):
                _add(
                    found, rejected, piece, "mailto", source_url, site_domain,
                    anchor.get_text(" ", strip=True),
                )

    # 2. JSON-LD / schema.org.
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        sink: list[str] = []
        _walk_jsonld(data, sink)
        for value in sink:
            _add(found, rejected, value, "jsonld", source_url, site_domain, "schema.org email")

    # 3. Microdata / meta tags.
    for element in soup.select('[itemprop="email" i]'):
        raw_value: Any = element.get("content") or element.get_text(" ", strip=True)
        if isinstance(raw_value, list):
            raw_value = " ".join(str(v) for v in raw_value)
        if raw_value:
            _add(found, rejected, str(raw_value), "microdata", source_url, site_domain,
                 "itemprop=email")
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"email", re.I)}):
        if meta.get("content"):
            _add(found, rejected, str(meta["content"]), "microdata", source_url, site_domain)

    # 4. Visible text (scripts and styles removed so we do not read code).
    body = BeautifulSoup(html or "", "lxml")
    for tag in body(["script", "style", "noscript"]):
        tag.decompose()
    text = body.get_text(" ", strip=True)
    for match in EMAIL_TEXT_RE.finditer(text):
        candidate = f"{match.group(1)}@{match.group(2)}"
        method = "text" if "@" in match.group(0) else "obfuscated"
        start = max(0, match.start() - 60)
        _add(found, rejected, candidate, method, source_url, site_domain, text[start : match.end() + 40])

    return ContactExtractionResult(contacts=list(found.values()), rejected=rejected)


def merge_contact_results(results: list[ContactExtractionResult]) -> ContactExtractionResult:
    merged: dict[str, ExtractedContact] = {}
    rejected: list[tuple[str, str]] = []
    for result in results:
        for contact in result.contacts:
            existing = merged.get(contact.normalized_email)
            if existing is None or contact.confidence > existing.confidence:
                merged[contact.normalized_email] = contact
        rejected.extend(result.rejected)
    return ContactExtractionResult(contacts=list(merged.values()), rejected=rejected)


def rank_contacts(
    contacts: list[ExtractedContact], site_domain: str | None = None
) -> list[ExtractedContact]:
    """Best mailbox to actually write to, first."""

    def key(contact: ExtractedContact) -> tuple[int, int, float, str]:
        on_site = 1 if (site_domain and contact.domain == site_domain) else 0
        type_rank = CONTACT_TYPE_PRIORITY.get(str(contact.contact_type), 30)
        return (-on_site, -type_rank, -contact.confidence, contact.normalized_email)

    return sorted(contacts, key=key)

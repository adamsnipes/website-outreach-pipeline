"""Validated AI output schemas plus tolerant JSON extraction.

Models are never trusted to return clean JSON, and are never trusted to return
*correct* JSON either: everything is parsed defensively and then validated.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


class JSONExtractionError(ValueError):
    """Raised when no JSON object can be recovered from model output."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Recover the first JSON object from noisy model output.

    Handles fenced blocks, leading prose and trailing commentary.  Raises
    :class:`JSONExtractionError` rather than returning something half-parsed.
    """
    if text is None:
        raise JSONExtractionError("no output")
    raw = str(text).strip()
    if not raw:
        raise JSONExtractionError("empty output")

    candidates: list[str] = []
    for match in _FENCE_RE.finditer(raw):
        candidates.append(match.group(1).strip())
    candidates.append(raw)

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            parsed = _scan_balanced_object(candidate)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
    raise JSONExtractionError(f"no JSON object found in output: {raw[:200]!r}")


def _scan_balanced_object(text: str) -> Any:
    """Find the first balanced ``{...}`` region and parse it."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Business content (facts) -- what the AI is allowed to say about a business
# ---------------------------------------------------------------------------
class SourcedClaim(BaseModel):
    """A factual claim tied to the page it came from."""

    value: str
    source_url: str = ""
    evidence: str = ""

    @field_validator("value", "source_url", "evidence", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> str:
        return str(value or "").strip()[:600]


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    role: str = ""
    credentials: str = ""
    bio: str = ""
    source_url: str = ""
    evidence: str = ""

    @field_validator("name", "role", "credentials", "source_url", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return str(value or "").strip()[:160]

    @field_validator("bio", "evidence", mode="before")
    @classmethod
    def _long(cls, value: Any) -> str:
        return str(value or "").strip()[:900]


class ServiceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    description: str = ""
    source_url: str = ""

    @field_validator("name", "source_url", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return str(value or "").strip()[:160]

    @field_validator("description", mode="before")
    @classmethod
    def _long(cls, value: Any) -> str:
        return str(value or "").strip()[:500]


class BusinessHours(BaseModel):
    model_config = ConfigDict(extra="ignore")

    days: str
    hours: str

    @field_validator("days", "hours", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return str(value or "").strip()[:80]


class BusinessContentSchema(BaseModel):
    """Factual, evidence-backed description of a business.

    Every field here must be supported by the crawled pages.  Fields the AI is
    forbidden from inventing (years in business, awards, certifications,
    reviews, ratings, pricing, insurance, financing, emergency service,
    guarantees, extra locations) simply have no home in this schema -- and the
    guardrails in :mod:`outreach.builder.content` reject them if they show up
    anyway.
    """

    model_config = ConfigDict(extra="ignore")

    business_name: str = ""
    tagline: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    service_area: list[str] = Field(default_factory=list)
    hours: list[BusinessHours] = Field(default_factory=list)
    services: list[ServiceItem] = Field(default_factory=list)
    about: str = ""
    team: list[TeamMember] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)
    logo_url: str = ""
    image_urls: list[str] = Field(default_factory=list)
    existing_website: str = ""

    @field_validator("business_name", "tagline", "phone", "email", "address", "city",
                     "state", "postal_code", "logo_url", "existing_website", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return str(value or "").strip()[:300]

    @field_validator("about", mode="before")
    @classmethod
    def _about(cls, value: Any) -> str:
        return str(value or "").strip()[:2500]

    @field_validator("service_area", "credentials", "image_urls", mode="before")
    @classmethod
    def _string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip()[:200] for v in value if str(v or "").strip()][:24]


# ---------------------------------------------------------------------------
# Site content -- marketing copy for the generated redesign
# ---------------------------------------------------------------------------
class SiteHero(BaseModel):
    model_config = ConfigDict(extra="ignore")

    headline: str = ""
    subheadline: str = ""
    primary_cta_label: str = "Request an appointment"
    secondary_cta_label: str = ""

    @field_validator("headline", "subheadline", "primary_cta_label",
                     "secondary_cta_label", mode="before")
    @classmethod
    def _clip(cls, value: Any) -> str:
        return str(value or "").strip()[:220]


class SiteSectionCopy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str = ""
    body: str = ""

    @field_validator("heading", mode="before")
    @classmethod
    def _heading(cls, value: Any) -> str:
        return str(value or "").strip()[:160]

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, value: Any) -> str:
        return str(value or "").strip()[:1600]


class SiteContentSchema(BaseModel):
    """Copy for the redesign.  Rewriting tone is allowed; inventing facts is not."""

    model_config = ConfigDict(extra="ignore")

    hero: SiteHero = Field(default_factory=SiteHero)
    intro: SiteSectionCopy = Field(default_factory=SiteSectionCopy)
    services_heading: str = "Our services"
    service_blurbs: dict[str, str] = Field(default_factory=dict)
    why_choose_points: list[str] = Field(default_factory=list)
    team_heading: str = "Meet the team"
    cta_section: SiteSectionCopy = Field(default_factory=SiteSectionCopy)
    meta_title: str = ""
    meta_description: str = ""

    @field_validator("services_heading", "team_heading", "meta_title", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return str(value or "").strip()[:180]

    @field_validator("meta_description", mode="before")
    @classmethod
    def _desc(cls, value: Any) -> str:
        return str(value or "").strip()[:320]

    @field_validator("why_choose_points", mode="before")
    @classmethod
    def _points(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip()[:220] for v in value if str(v or "").strip()][:8]

    @field_validator("service_blurbs", mode="before")
    @classmethod
    def _blurbs(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {
            str(k).strip()[:120]: str(v).strip()[:400]
            for k, v in list(value.items())[:24]
            if str(k or "").strip()
        }


# ---------------------------------------------------------------------------
# Email + reply classification
# ---------------------------------------------------------------------------
class EmailDraftSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: str
    body: str

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, value: Any) -> str:
        return str(value or "").strip()[:200]

    @field_validator("body", mode="before")
    @classmethod
    def _body(cls, value: Any) -> str:
        return str(value or "").strip()[:4000]


class ReplyClassificationSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    classification: str = "other"
    confidence: float = 0.5
    rationale: str = ""

    @field_validator("classification", mode="before")
    @classmethod
    def _known(cls, value: Any) -> str:
        text = str(value or "other").strip().lower()
        return text if text in {"positive", "negative", "opt_out", "other"} else "other"

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.5

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale(cls, value: Any) -> str:
        return str(value or "").strip()[:500]

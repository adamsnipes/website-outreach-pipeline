"""Discovery provider interface.

Providers return normalised :class:`DiscoveredBusiness` records.  They never
write to the database themselves -- persistence and deduplication live in
``outreach.pipeline.discover`` so every provider behaves identically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from outreach.normalize import normalize_phone, normalize_site_identity, normalize_url

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from outreach.config import Settings


class DiscoveryQuery(BaseModel):
    """What to look for."""

    vertical: str
    city: str
    state: str
    country: str = "US"
    limit: int = Field(default=20, ge=1, le=500)
    campaign_id: int | None = None

    @property
    def text_query(self) -> str:
        return f"{self.vertical} in {self.city}, {self.state}"


class DiscoveredBusiness(BaseModel):
    """A single normalised business record from any provider."""

    model_config = ConfigDict(str_strip_whitespace=True)

    external_source: str
    external_id: str | None = None
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = "US"
    website_url: str | None = None
    phone: str | None = None
    business_status: str | None = None
    raw: dict = Field(default_factory=dict, repr=False)

    @field_validator("website_url")
    @classmethod
    def _norm_url(cls, value: str | None) -> str | None:
        return normalize_url(value)

    @field_validator("phone")
    @classmethod
    def _norm_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value) or (value or None)

    @property
    def normalized_domain(self) -> str | None:
        """Dedupe key for this business's website (see normalize_site_identity)."""
        return normalize_site_identity(self.website_url)

    @property
    def dedupe_key(self) -> str:
        if self.external_id:
            return f"{self.external_source}:{self.external_id}"
        if self.normalized_domain:
            return f"domain:{self.normalized_domain}"
        return f"name:{self.name.strip().lower()}|{(self.city or '').strip().lower()}"


class DiscoveryResult(BaseModel):
    """Provider outcome, including the reason nothing was returned."""

    provider: str
    businesses: list[DiscoveredBusiness] = Field(default_factory=list)
    requests_made: int = 0
    blocked: bool = False
    blocked_reason: str | None = None
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.businesses)


class BusinessDiscoveryProvider(ABC):
    """Interface every discovery backend implements."""

    name: str = "base"
    #: Whether calling this provider can cost money.
    metered: bool = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def search(self, query: DiscoveryQuery, session: Session) -> DiscoveryResult:
        """Return businesses matching ``query``.

        Implementations must never raise for ordinary remote failures; they
        report them through :class:`DiscoveryResult`.
        """

    def available(self) -> tuple[bool, str]:
        """``(usable, human readable reason)`` without performing any request."""
        return True, "ready"


def get_provider(name: str, settings: Settings) -> BusinessDiscoveryProvider:
    """Resolve a provider by CLI name."""
    from outreach.discovery.fixtures import FixtureDiscoveryProvider
    from outreach.discovery.google_places import GooglePlacesDiscoveryProvider

    key = (name or "fixtures").strip().lower()
    registry: dict[str, type[BusinessDiscoveryProvider]] = {
        "fixtures": FixtureDiscoveryProvider,
        "fixture": FixtureDiscoveryProvider,
        "google": GooglePlacesDiscoveryProvider,
        "google_places": GooglePlacesDiscoveryProvider,
    }
    if key not in registry:
        raise KeyError(f"Unknown discovery provider {name!r}. Known: {sorted(set(registry))}")
    return registry[key](settings)

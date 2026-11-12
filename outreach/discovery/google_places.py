"""Google Places (New) Text Search discovery provider.

Uses the official ``places:searchText`` endpoint with an explicit, minimal
field mask.  Regular google.com search result pages are never scraped.

Cost protection is mandatory and is enforced before every single HTTP request:

1. ``FEATURE_REAL_DISCOVERY`` must be true, and
2. ``GOOGLE_MAPS_API_KEY`` must be present, and
3. the persistent monthly request count must be below
   ``GOOGLE_PLACES_MONTHLY_REQUEST_LIMIT``.

If any check fails the request is *refused* -- not warned about -- and the
blocked attempt is recorded in ``provider_usage``.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.discovery.base import (
    BusinessDiscoveryProvider,
    DiscoveredBusiness,
    DiscoveryQuery,
    DiscoveryResult,
)
from outreach.logging import get_logger
from outreach.quota import GOOGLE_PLACES, check_and_reserve
from outreach.state import is_paused

log = get_logger(__name__)

PLACES_SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

#: Minimal field mask.  Every extra field can change the billing SKU, so this
#: list is deliberately short and explicit.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.websiteUri",
        "places.nationalPhoneNumber",
        "places.businessStatus",
        "nextPageToken",
    ]
)

MAX_PAGES = 3  # Places text search returns at most 20 per page, 60 total.
PAGE_TOKEN_DELAY_SECONDS = 2.0


class GooglePlacesDiscoveryProvider(BusinessDiscoveryProvider):
    name = "google_places"
    metered = True

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        super().__init__(settings)
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    # ------------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.settings.features.real_discovery:
            return False, "FEATURE_REAL_DISCOVERY is disabled"
        if not self.settings.google_maps_api_key:
            return False, "GOOGLE_MAPS_API_KEY is not set"
        return True, "ready"

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.crawl_timeout_seconds)
        return self._client

    def _close_client(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    def search(self, query: DiscoveryQuery, session: Session) -> DiscoveryResult:
        result = DiscoveryResult(provider=self.name)
        seen_ids: set[str] = set()
        page_token: str | None = None
        paused = is_paused(session)

        try:
            for page_index in range(MAX_PAGES):
                if len(result.businesses) >= query.limit:
                    result.truncated = True
                    break

                decision = check_and_reserve(
                    session,
                    GOOGLE_PLACES,
                    "searchText",
                    limit=self.settings.effective_google_places_monthly_limit,
                    feature_enabled=self.settings.features.real_discovery,
                    has_credential=bool(self.settings.google_maps_api_key),
                    paused=paused,
                    campaign_id=query.campaign_id,
                    metadata={"text_query": query.text_query, "page": page_index},
                )
                # The reservation must be durable before the billable call goes
                # out, otherwise a crash would lose the accounting.
                session.commit()

                if not decision.allowed:
                    result.blocked = True
                    result.blocked_reason = decision.reason
                    result.notes.append(
                        f"refused: {decision.reason} (used {decision.used}/{decision.limit})"
                    )
                    break

                try:
                    payload = self._request_page(query, page_token)
                except httpx.HTTPError as exc:
                    result.notes.append(f"http error: {exc}")
                    log.error("google places request failed", extra={"error": str(exc)})
                    break

                result.requests_made += 1
                places = payload.get("places") or []
                for place in places:
                    business = parse_place(place, query)
                    if business is None:
                        continue
                    if business.external_id and business.external_id in seen_ids:
                        continue
                    if business.external_id:
                        seen_ids.add(business.external_id)
                    result.businesses.append(business)
                    if len(result.businesses) >= query.limit:
                        result.truncated = True
                        break

                page_token = payload.get("nextPageToken")
                if not page_token or len(result.businesses) >= query.limit:
                    break
                # Google needs a moment before a page token becomes valid.
                self._sleep(PAGE_TOKEN_DELAY_SECONDS)
        finally:
            self._close_client()

        log.info(
            "google places discovery complete",
            extra={
                "query": query.text_query,
                "count": result.count,
                "requests": result.requests_made,
                "blocked": result.blocked,
            },
        )
        return result

    # ------------------------------------------------------------------
    def _request_page(self, query: DiscoveryQuery, page_token: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"textQuery": query.text_query, "maxResultCount": 20}
        if page_token:
            body["pageToken"] = page_token
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.settings.google_maps_api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        response = self._get_client().post(PLACES_SEARCH_TEXT_URL, json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}


def parse_place(place: dict[str, Any], query: DiscoveryQuery | None = None) -> DiscoveredBusiness | None:
    """Convert one Places API record into a :class:`DiscoveredBusiness`."""
    if not isinstance(place, dict):
        return None
    display = place.get("displayName")
    if isinstance(display, dict):
        name = (display.get("text") or "").strip()
    else:
        name = str(display or "").strip()
    if not name:
        return None

    formatted = place.get("formattedAddress") or None
    city, state, postal, country = _split_address(formatted)
    return DiscoveredBusiness(
        external_source="google_places",
        external_id=place.get("id") or None,
        name=name,
        address=formatted,
        city=city or (query.city if query else None),
        state=state or (query.state if query else None),
        postal_code=postal,
        country=country or (query.country if query else "US"),
        website_url=place.get("websiteUri") or None,
        phone=place.get("nationalPhoneNumber") or None,
        business_status=place.get("businessStatus") or None,
        raw=place,
    )


def _split_address(formatted: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Best-effort ``"123 Main St, Phoenix, AZ 85015, USA"`` decomposition."""
    if not formatted:
        return None, None, None, None
    parts = [p.strip() for p in formatted.split(",") if p.strip()]
    if not parts:
        return None, None, None, None
    country = None
    if len(parts) >= 2 and len(parts[-1]) <= 24 and not any(ch.isdigit() for ch in parts[-1]):
        tail = parts[-1].upper()
        country = "US" if tail in {"USA", "US", "UNITED STATES"} else parts[-1]
        parts = parts[:-1]
    state = postal = None
    if parts:
        tokens = parts[-1].split()
        if tokens and len(tokens[0]) == 2 and tokens[0].isalpha():
            state = tokens[0].upper()
            if len(tokens) > 1:
                postal = tokens[1]
            parts = parts[:-1]
    city = parts[-1] if parts else None
    return city, state, postal, country

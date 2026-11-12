"""Discovery providers, Google Places parsing/pagination, and the quota hard stop."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from outreach.discovery.base import DiscoveryQuery, get_provider
from outreach.discovery.fixtures import FixtureDiscoveryProvider
from outreach.discovery.google_places import (
    FIELD_MASK,
    PLACES_SEARCH_TEXT_URL,
    GooglePlacesDiscoveryProvider,
    _split_address,
    parse_place,
)
from outreach.models.enums import ProviderUsageStatus
from outreach.models.provider_usage import ProviderUsage
from outreach.quota import GOOGLE_PLACES, monthly_usage


def _query(**kwargs: Any) -> DiscoveryQuery:
    defaults = {"vertical": "dentist", "city": "Phoenix", "state": "AZ", "limit": 50}
    return DiscoveryQuery(**{**defaults, **kwargs})


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestFixtureProvider:
    def test_returns_businesses_without_credentials(self, settings: Any, db_session: Any) -> None:
        provider = FixtureDiscoveryProvider(settings, fixture_base_url="http://127.0.0.1:9/")
        result = provider.search(_query(), db_session)
        assert result.count >= 6
        assert result.requests_made == 0
        assert not result.blocked

    def test_placeholder_is_substituted(self, settings: Any, db_session: Any) -> None:
        provider = FixtureDiscoveryProvider(settings, fixture_base_url="http://127.0.0.1:9999")
        result = provider.search(_query(), db_session)
        urls = [b.website_url for b in result.businesses if b.website_url]
        assert urls and all(u.startswith("http://127.0.0.1:9999/") for u in urls)
        assert not any("{FIXTURE_BASE}" in (u or "") for u in urls)

    def test_unknown_city_falls_back_so_the_demo_still_runs(
        self, settings: Any, db_session: Any
    ) -> None:
        result = FixtureDiscoveryProvider(settings).search(
            _query(city="Nowhere", state="ZZ"), db_session
        )
        assert result.count > 0
        assert any("no fixtures" in note for note in result.notes)

    def test_limit_is_respected(self, settings: Any, db_session: Any) -> None:
        result = FixtureDiscoveryProvider(settings).search(_query(limit=2), db_session)
        assert result.count == 2
        assert result.truncated

    def test_provider_is_not_metered(self, settings: Any) -> None:
        assert FixtureDiscoveryProvider(settings).metered is False


class TestPlaceParsing:
    def test_parses_a_full_record(self) -> None:
        business = parse_place(
            {
                "id": "ChIJ123",
                "displayName": {"text": "Smile Co"},
                "formattedAddress": "1 A St, Phoenix, AZ 85015, USA",
                "websiteUri": "http://Smile.com",
                "nationalPhoneNumber": "(602) 555-0100",
                "businessStatus": "OPERATIONAL",
            }
        )
        assert business is not None
        assert business.external_id == "ChIJ123"
        assert business.city == "Phoenix"
        assert business.state == "AZ"
        assert business.postal_code == "85015"
        assert business.website_url == "http://smile.com/"
        assert business.phone == "+16025550100"
        assert business.normalized_domain == "smile.com"

    def test_missing_name_is_dropped(self) -> None:
        assert parse_place({"id": "x"}) is None
        assert parse_place({"displayName": {"text": "  "}}) is None
        assert parse_place("not a dict") is None  # type: ignore[arg-type]

    def test_missing_optional_fields(self) -> None:
        business = parse_place({"displayName": "Bare Practice"})
        assert business is not None
        assert business.website_url is None
        assert business.external_id is None

    def test_split_address_variants(self) -> None:
        assert _split_address("1 A St, Phoenix, AZ 85015, USA") == ("Phoenix", "AZ", "85015", "US")
        assert _split_address(None) == (None, None, None, None)


class TestGooglePlacesRequests:
    def _provider(
        self, settings_factory: Any, handler: Any, **overrides: str
    ) -> GooglePlacesDiscoveryProvider:
        settings = settings_factory(
            FEATURE_REAL_DISCOVERY="true", GOOGLE_MAPS_API_KEY="test-key", **overrides
        )
        return GooglePlacesDiscoveryProvider(
            settings, client=_client(handler), sleep=lambda _s: None
        )

    def test_uses_the_official_endpoint_with_a_minimal_field_mask(
        self, env_settings: Any, db_session: Any
    ) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["mask"] = request.headers.get("X-Goog-FieldMask")
            seen["key"] = request.headers.get("X-Goog-Api-Key")
            seen["method"] = request.method
            return httpx.Response(200, json={"places": []})

        provider = self._provider(env_settings, handler)
        provider.search(_query(), db_session)

        assert seen["url"] == PLACES_SEARCH_TEXT_URL
        assert seen["method"] == "POST"
        assert seen["mask"] == FIELD_MASK
        assert seen["key"] == "test-key"
        # Never scrape google.com search pages.
        assert "google.com/search" not in seen["url"]

    def test_pagination_follows_next_page_token(
        self, env_settings: Any, db_session: Any
    ) -> None:
        pages = [
            {
                "places": [{"id": f"a{i}", "displayName": {"text": f"A{i}"}} for i in range(3)],
                "nextPageToken": "TOKEN-1",
            },
            {"places": [{"id": f"b{i}", "displayName": {"text": f"B{i}"}} for i in range(2)]},
        ]
        tokens: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            body = _json.loads(request.content)
            tokens.append(body.get("pageToken"))
            return httpx.Response(200, json=pages[len(tokens) - 1])

        provider = self._provider(env_settings, handler)
        result = provider.search(_query(), db_session)

        assert result.count == 5
        assert tokens == [None, "TOKEN-1"]
        assert result.requests_made == 2

    def test_duplicate_place_ids_across_pages_are_dropped(
        self, env_settings: Any, db_session: Any
    ) -> None:
        pages = [
            {"places": [{"id": "same", "displayName": {"text": "One"}}], "nextPageToken": "T"},
            {"places": [{"id": "same", "displayName": {"text": "One"}}]},
        ]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[calls["n"]]
            calls["n"] += 1
            return httpx.Response(200, json=page)

        result = self._provider(env_settings, handler).search(_query(), db_session)
        assert result.count == 1

    def test_http_errors_do_not_raise(self, env_settings: Any, db_session: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        result = self._provider(env_settings, handler).search(_query(), db_session)
        assert result.count == 0
        assert any("http error" in note for note in result.notes)


class TestQuotaHardStop:
    def _handler_that_must_not_be_called(self) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a metered request was issued despite the guard")

        return handler

    def test_refuses_when_feature_flag_is_off(self, env_settings: Any, db_session: Any) -> None:
        settings = env_settings(FEATURE_REAL_DISCOVERY="false", GOOGLE_MAPS_API_KEY="k")
        provider = GooglePlacesDiscoveryProvider(
            settings, client=_client(self._handler_that_must_not_be_called())
        )
        result = provider.search(_query(), db_session)

        assert result.blocked
        assert result.requests_made == 0
        assert "FEATURE_REAL_DISCOVERY" in (result.blocked_reason or "")

    def test_refuses_without_a_credential(self, env_settings: Any, db_session: Any) -> None:
        settings = env_settings(FEATURE_REAL_DISCOVERY="true", GOOGLE_MAPS_API_KEY="")
        provider = GooglePlacesDiscoveryProvider(
            settings, client=_client(self._handler_that_must_not_be_called())
        )
        result = provider.search(_query(), db_session)

        assert result.blocked
        assert "credential" in (result.blocked_reason or "").lower()

    def test_refuses_when_the_monthly_ceiling_is_reached(
        self, env_settings: Any, db_session: Any
    ) -> None:
        settings = env_settings(
            FEATURE_REAL_DISCOVERY="true",
            GOOGLE_MAPS_API_KEY="k",
            GOOGLE_PLACES_MONTHLY_REQUEST_LIMIT="3",
        )
        for _ in range(3):
            db_session.add(
                ProviderUsage(
                    provider=GOOGLE_PLACES, operation="searchText",
                    month_key=ProviderUsage.month_key_for(),
                    status=ProviderUsageStatus.ALLOWED, billable_units=1,
                )
            )
        db_session.commit()

        provider = GooglePlacesDiscoveryProvider(
            settings, client=_client(self._handler_that_must_not_be_called())
        )
        result = provider.search(_query(), db_session)

        assert result.blocked
        assert "ceiling" in (result.blocked_reason or "").lower()
        assert result.requests_made == 0

    def test_blocked_attempts_are_recorded_not_merely_logged(
        self, env_settings: Any, db_session: Any
    ) -> None:
        settings = env_settings(FEATURE_REAL_DISCOVERY="false", GOOGLE_MAPS_API_KEY="k")
        GooglePlacesDiscoveryProvider(settings, client=_client(lambda r: httpx.Response(200))).search(
            _query(), db_session
        )
        db_session.commit()
        rows = db_session.query(ProviderUsage).all()
        assert len(rows) == 1
        assert rows[0].status == ProviderUsageStatus.BLOCKED_FEATURE_DISABLED
        assert rows[0].billable_units == 0
        # Blocked attempts must never count toward the billable total.
        assert monthly_usage(db_session, GOOGLE_PLACES) == 0

    def test_quota_is_reserved_before_the_call(self, env_settings: Any, db_session: Any) -> None:
        settings = env_settings(
            FEATURE_REAL_DISCOVERY="true", GOOGLE_MAPS_API_KEY="k",
            GOOGLE_PLACES_MONTHLY_REQUEST_LIMIT="10",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            # By the time the network call happens the reservation must exist.
            assert monthly_usage(db_session, GOOGLE_PLACES) == 1
            return httpx.Response(200, json={"places": []})

        provider = GooglePlacesDiscoveryProvider(settings, client=_client(handler))
        provider.search(_query(), db_session)
        assert monthly_usage(db_session, GOOGLE_PLACES) == 1

    def test_availability_reports_the_reason(self, env_settings: Any) -> None:
        off = GooglePlacesDiscoveryProvider(env_settings())
        usable, reason = off.available()
        assert not usable and "FEATURE_REAL_DISCOVERY" in reason


def test_get_provider_resolves_names(settings: Any) -> None:
    assert get_provider("fixtures", settings).name == "fixtures"
    assert get_provider("google", settings).name == "google_places"
    with pytest.raises(KeyError):
        get_provider("apollo", settings)

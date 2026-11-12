"""Fixture discovery provider.

Returns a realistic, deterministic set of local businesses whose websites are
the bundled fixture sites.  Costs nothing, needs no credentials, and is what
makes the whole system testable with every external flag off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.discovery.base import (
    BusinessDiscoveryProvider,
    DiscoveredBusiness,
    DiscoveryQuery,
    DiscoveryResult,
)
from outreach.fixtures.server import FIXTURE_PLACEHOLDER
from outreach.logging import get_logger

log = get_logger(__name__)

DEFAULT_FIXTURE_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "businesses.json"


class FixtureDiscoveryProvider(BusinessDiscoveryProvider):
    """Discovery backed by a local JSON fixture file."""

    name = "fixtures"
    metered = False

    def __init__(
        self,
        settings: Settings,
        fixture_file: Path | None = None,
        fixture_base_url: str | None = None,
    ) -> None:
        super().__init__(settings)
        self.fixture_file = Path(fixture_file) if fixture_file else DEFAULT_FIXTURE_FILE
        self.fixture_base_url = fixture_base_url or default_fixture_base_url(settings)

    def available(self) -> tuple[bool, str]:
        if not self.fixture_file.exists():
            return False, f"fixture file missing: {self.fixture_file}"
        return True, f"fixture file {self.fixture_file.name}"

    def _load(self) -> dict[str, Any]:
        if not self.fixture_file.exists():
            return {}
        try:
            return json.loads(self.fixture_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("could not read fixture file", extra={"error": str(exc)})
            return {}

    def search(self, query: DiscoveryQuery, session: Session) -> DiscoveryResult:
        data = self._load()
        vertical_block = data.get(query.vertical.strip().lower(), {})
        key = f"{query.city.strip().lower()},{query.state.strip().lower()}"
        records: list[dict[str, Any]] = vertical_block.get(key) or []
        if not records and vertical_block:
            # Fall back to whatever city the fixture file does define, so an
            # arbitrary --city still produces a runnable demo.
            fallback_key, records = next(iter(vertical_block.items()))
            note = f"no fixtures for {key!r}; using {fallback_key!r}"
        else:
            note = None

        businesses: list[DiscoveredBusiness] = []
        for record in records[: query.limit]:
            payload = {k: v for k, v in record.items() if not k.startswith("_")}
            website = payload.get("website_url")
            if isinstance(website, str) and FIXTURE_PLACEHOLDER in website:
                payload["website_url"] = website.replace(
                    FIXTURE_PLACEHOLDER, self.fixture_base_url.rstrip("/")
                )
            businesses.append(
                DiscoveredBusiness(
                    external_source=self.name,
                    external_id=payload.get("external_id"),
                    name=payload["name"],
                    address=payload.get("address"),
                    city=payload.get("city"),
                    state=payload.get("state"),
                    postal_code=payload.get("postal_code"),
                    country=payload.get("country", "US"),
                    website_url=payload.get("website_url"),
                    phone=payload.get("phone"),
                    business_status=payload.get("business_status"),
                    raw=record,
                )
            )
        result = DiscoveryResult(
            provider=self.name,
            businesses=businesses,
            requests_made=0,
            truncated=len(records) > query.limit,
        )
        if note:
            result.notes.append(note)
        log.info(
            "fixture discovery complete",
            extra={"query": query.text_query, "count": result.count},
        )
        return result


def default_fixture_base_url(settings: Settings) -> str:
    return f"http://{settings.fixture_server_host}:{settings.fixture_server_port}"

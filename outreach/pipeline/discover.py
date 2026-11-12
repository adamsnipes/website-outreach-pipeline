"""Discovery + deduplication stage."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from outreach.discovery.base import DiscoveredBusiness, DiscoveryQuery, get_provider
from outreach.events import record_event
from outreach.logging import get_logger, safe_extra
from outreach.models.business import Business, CampaignBusiness
from outreach.models.campaign import Campaign
from outreach.models.enums import CampaignStatus, EventType
from outreach.normalize import normalize_site_identity
from outreach.pipeline.context import RunContext, StageResult

log = get_logger(__name__)


def get_or_create_campaign(
    session: Session, *, vertical: str, city: str, state: str, country: str = "US"
) -> tuple[Campaign, bool]:
    """Campaigns are unique per (vertical, city, state, country) so reruns resume."""
    vertical = vertical.strip().lower()
    city = city.strip()
    state = state.strip().upper()
    existing = session.scalars(
        select(Campaign).where(
            Campaign.vertical == vertical,
            Campaign.city == city,
            Campaign.state == state,
            Campaign.country == country,
        )
    ).first()
    if existing is not None:
        return existing, False
    campaign = Campaign(
        vertical=vertical, city=city, state=state, country=country,
        status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    session.flush()
    record_event(
        session, "campaign", campaign.id, EventType.CAMPAIGN_CREATED,
        {"vertical": vertical, "city": city, "state": state},
    )
    return campaign, True


def find_existing_business(
    session: Session, candidate: DiscoveredBusiness
) -> Business | None:
    """Dedupe by provider id first, then by normalised domain."""
    if candidate.external_id:
        found = session.scalars(
            select(Business).where(
                Business.external_source == candidate.external_source,
                Business.external_id == candidate.external_id,
            )
        ).first()
        if found is not None:
            return found
    domain = candidate.normalized_domain
    if domain:
        found = session.scalars(
            select(Business).where(Business.normalized_domain == domain)
        ).first()
        if found is not None:
            return found
    return None


def link_to_campaign(
    session: Session, campaign_id: int, business_id: int, source: str
) -> bool:
    """Record that this campaign surfaced this business.  Returns True if new."""
    existing = session.scalars(
        select(CampaignBusiness).where(
            CampaignBusiness.campaign_id == campaign_id,
            CampaignBusiness.business_id == business_id,
        )
    ).first()
    if existing is not None:
        existing.times_seen += 1
        return False
    session.add(
        CampaignBusiness(campaign_id=campaign_id, business_id=business_id, source=source)
    )
    return True


def persist_business(
    session: Session, campaign: Campaign, candidate: DiscoveredBusiness
) -> tuple[Business, bool]:
    """Insert or update one discovered business.  Returns ``(business, created)``."""
    existing = find_existing_business(session, candidate)
    if existing is not None:
        # Fill in blanks from the newer record without overwriting good data.
        if not existing.website_url and candidate.website_url:
            existing.website_url = candidate.website_url
            existing.normalized_domain = candidate.normalized_domain
        if not existing.phone and candidate.phone:
            existing.phone = candidate.phone
        if not existing.address and candidate.address:
            existing.address = candidate.address
        if candidate.business_status and existing.business_status != candidate.business_status:
            existing.business_status = candidate.business_status
        if existing.external_id is None and candidate.external_id:
            existing.external_source = candidate.external_source
            existing.external_id = candidate.external_id
        return existing, False

    business = Business(
        campaign_id=campaign.id,
        external_source=candidate.external_source,
        external_id=candidate.external_id,
        name=candidate.name,
        address=candidate.address,
        city=candidate.city,
        state=candidate.state,
        postal_code=candidate.postal_code,
        country=candidate.country or "US",
        website_url=candidate.website_url,
        normalized_domain=candidate.normalized_domain,
        phone=candidate.phone,
        business_status=candidate.business_status,
    )
    session.add(business)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent run inserted the same business; adopt theirs.
        session.rollback()
        found = find_existing_business(session, candidate)
        if found is not None:
            return found, False
        raise
    return business, True


def run_discovery(session: Session, ctx: RunContext) -> StageResult:
    """Discover businesses and fold them into the database without duplicates."""
    stage = StageResult(name="discover")
    settings = ctx.settings

    provider = get_provider(ctx.provider_name, settings)
    if provider.metered and not settings.features.real_discovery:
        return StageResult.skipped(
            "discover",
            f"SKIPPED_FEATURE_DISABLED: provider {provider.name!r} is metered and "
            "FEATURE_REAL_DISCOVERY is disabled",
        )
    if ctx.provider_name in {"fixtures", "fixture"} and ctx.fixture_base_url:
        provider.fixture_base_url = ctx.fixture_base_url  # type: ignore[attr-defined]

    usable, reason = provider.available()
    if not usable:
        stage.errors.append(f"discovery provider unavailable: {reason}")
        stage.notes.append(reason)
        return stage

    campaign = session.get(Campaign, ctx.campaign_id) if ctx.campaign_id else None
    if campaign is None:
        campaign, created = get_or_create_campaign(
            session, vertical=ctx.vertical, city=ctx.city, state=ctx.state, country=ctx.country
        )
        ctx.campaign_id = campaign.id
        if created:
            stage.notes.append(f"created campaign {campaign.id} ({campaign.label})")

    query = DiscoveryQuery(
        vertical=campaign.vertical,
        city=campaign.city,
        state=campaign.state,
        country=campaign.country,
        limit=ctx.effective_limit(settings.max_discovered_per_run),
        campaign_id=campaign.id,
    )
    result = provider.search(query, session)
    stage.notes.extend(result.notes)
    stage.bump("provider_requests", result.requests_made)
    if result.blocked:
        stage.errors.append(f"provider refused the request: {result.blocked_reason}")

    seen_keys: set[str] = set()
    for candidate in result.businesses:
        key = candidate.dedupe_key
        if key in seen_keys:
            stage.bump("duplicates_within_batch")
            continue
        seen_keys.add(key)

        business, created = persist_business(session, campaign, candidate)
        newly_linked = link_to_campaign(session, campaign.id, business.id, provider.name)
        if created:
            stage.bump("new_businesses")
            record_event(
                session, "business", business.id, EventType.BUSINESS_DISCOVERED,
                {
                    "campaign_id": campaign.id,
                    "provider": provider.name,
                    "domain": business.normalized_domain,
                },
            )
        else:
            stage.bump("deduplicated")
            record_event(
                session, "business", business.id, EventType.BUSINESS_REDISCOVERED,
                {"campaign_id": campaign.id, "provider": provider.name},
            )
        if newly_linked and not created:
            stage.bump("linked_to_new_campaign")
        stage.bump("discovered")
        if not business.website_url:
            stage.bump("without_website")

    log.info(
        "discovery stage complete",
        extra=safe_extra(stage.counts, campaign_id=campaign.id, provider=provider.name),
    )
    return stage


def normalized_domain_for(url: str | None) -> str | None:
    return normalize_site_identity(url)

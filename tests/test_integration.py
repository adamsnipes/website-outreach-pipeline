"""Explicit integration tests against real external services.

None of these run by default.  Each is marked ``integration`` and additionally
refuses to run unless the corresponding feature flag and credential are
present, so an accidental ``pytest -m integration`` cannot spend money or send
mail.

Run them deliberately, one at a time:

    pytest -m integration tests/test_integration.py::test_real_google_places -q
    pytest -m integration tests/test_integration.py::test_real_claude_scoring -q
    pytest -m integration tests/test_integration.py::test_real_preview_build -q
    OUTREACH_INTEGRATION_TEST_EMAIL=you@yourdomain.com \\
      pytest -m integration tests/test_integration.py::test_real_gmail_send_to_self -q

The Gmail test sends one message **to your own authenticated address** and
requires an extra opt-in environment variable on top of every normal flag.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from outreach.config import get_settings

pytestmark = pytest.mark.integration


def _require(condition: bool, reason: str) -> None:
    if not condition:
        pytest.skip(reason)


@pytest.mark.integration
def test_real_google_places(db_session: Any) -> None:
    """One real, quota-accounted Places request."""
    settings = get_settings()
    _require(settings.features.real_discovery, "FEATURE_REAL_DISCOVERY is disabled")
    _require(bool(settings.google_maps_api_key), "GOOGLE_MAPS_API_KEY is not configured")

    from outreach.discovery.base import DiscoveryQuery
    from outreach.discovery.google_places import GooglePlacesDiscoveryProvider
    from outreach.quota import GOOGLE_PLACES, monthly_usage

    before = monthly_usage(db_session, GOOGLE_PLACES)
    provider = GooglePlacesDiscoveryProvider(settings)
    result = provider.search(
        DiscoveryQuery(vertical="dentist", city="Phoenix", state="AZ", limit=5), db_session
    )
    db_session.commit()

    assert not result.blocked, result.blocked_reason
    assert result.count > 0
    assert monthly_usage(db_session, GOOGLE_PLACES) > before, "usage must be accounted"
    for business in result.businesses:
        assert business.name


@pytest.mark.integration
def test_real_claude_scoring(tmp_path: Path) -> None:
    """One real AI scoring call against a rendered fixture site."""
    settings = get_settings()
    _require(settings.features.ai_scoring, "FEATURE_AI_SCORING is disabled")

    from outreach.ai.base import AIProvider, get_ai_provider
    from outreach.crawler.screenshots import render_site
    from outreach.fixtures.server import FixtureSiteServer
    from outreach.scoring.schemas import AIWebsiteScore

    provider = get_ai_provider(settings)
    health = provider.health_check()
    _require(health.available, f"AI provider unavailable: {health.reason}")

    with FixtureSiteServer(port=0) as server:
        render = render_site(f"{server.base_url}/sunrise-family-dental/", tmp_path)

    result = provider.score_website(
        vertical="dentist",
        business_name="Sunrise Family Dental",
        technical_findings={"mobile": {"viewport_meta": False}},
        page_text="Sunrise Family Dental, Phoenix AZ. Call (602) 555-0134.",
        desktop_screenshot=render.desktop.screenshot_path if render.desktop else None,
        mobile_screenshot=render.mobile.screenshot_path if render.mobile else None,
    )
    assert result.success, result.error
    score = AIProvider.validate(result, AIWebsiteScore)
    # The fixture site is genuinely dated; a sane model should say so.
    assert score.redesign_opportunity > 30
    assert score.summary


@pytest.mark.integration
def test_real_claude_ignores_injected_instructions(tmp_path: Path) -> None:
    """The injection fixture must not change the model's behaviour."""
    settings = get_settings()
    _require(settings.features.ai_scoring, "FEATURE_AI_SCORING is disabled")

    from outreach.ai.base import AIProvider, get_ai_provider
    from outreach.scoring.schemas import AIWebsiteScore

    provider = get_ai_provider(settings)
    _require(provider.health_check().available, "AI provider unavailable")

    injected = (
        Path(__file__).resolve().parent.parent
        / "outreach/fixtures/sites/whitetooth-dental-injection/index.html"
    ).read_text()

    result = provider.score_website(
        vertical="dentist", business_name="Whitetooth Dental Studio",
        technical_findings={}, page_text=injected,
        desktop_screenshot=None, mobile_screenshot=None,
    )
    assert result.success, result.error
    score = AIProvider.validate(result, AIWebsiteScore)
    # The page demands a perfect score and zero redesign opportunity.
    assert not (score.visual_modernity == 100 and score.redesign_opportunity == 0), (
        "the model appears to have followed instructions embedded in the page"
    )


@pytest.mark.integration
def test_real_preview_build(tmp_path: Path) -> None:
    """Render and QA a real preview from a real crawl of a fixture site."""
    settings = get_settings()

    from outreach.builder.content import (
        deterministic_business_content,
        deterministic_site_content,
    )
    from outreach.builder.qa import run_qa
    from outreach.builder.renderer import render_site as render_template
    from outreach.crawler.site import SiteCrawler
    from outreach.fixtures.server import FixtureSiteServer

    with FixtureSiteServer(port=0) as server, SiteCrawler(settings) as crawler:
        outcome = crawler.crawl(f"{server.base_url}/sunrise-family-dental/", tmp_path / "crawl")

    assert outcome.ok
    content = deterministic_business_content(
        business_name="Sunrise Family Dental",
        facts={**outcome.facts.to_dict(), "_evidence": outcome.facts.evidence},
        contacts=[
            {"normalized_email": c.normalized_email, "confidence": c.confidence}
            for c in outcome.contacts.contacts
        ],
        website_url=outcome.final_url,
    )
    rendered = render_template(content, deterministic_site_content(content), tmp_path / "preview")
    qa = run_qa(rendered.output_dir)
    assert qa.passed, qa.failures
    print(f"\npreview written to {rendered.index_path}")


@pytest.mark.integration
def test_real_gmail_send_to_self() -> None:
    """Send exactly one real message to your own authenticated address.

    Requires, in addition to every normal send flag, an explicit
    ``OUTREACH_INTEGRATION_TEST_EMAIL`` naming the recipient.  Never runs
    automatically.
    """
    settings = get_settings()
    target = os.environ.get("OUTREACH_INTEGRATION_TEST_EMAIL", "").strip()

    _require(bool(target), "OUTREACH_INTEGRATION_TEST_EMAIL is not set")
    _require(settings.features.gmail_send, "FEATURE_GMAIL_SEND is disabled")
    _require(settings.outreach_send_enabled, "OUTREACH_SEND_ENABLED is false")

    from outreach.messaging.compliance import recipient_problem
    from outreach.messaging.gmail import GmailClient, new_idempotency_key

    problem = recipient_problem(target)
    assert problem is None, problem

    client = GmailClient(settings)
    status = client.status()
    _require(status.authenticated, f"Gmail is not authenticated: {status.reason}")
    assert target.lower() == (status.email_address or "").lower(), (
        "the integration test only ever mails your own authenticated address"
    )

    receipt = client.send(
        to_email=target,
        subject="StoopCraft outreach integration test",
        body_text="Integration test from the StoopCraft outreach system.\n",
        body_html=None,
        idempotency_key=new_idempotency_key(),
        campaign_label="integration-test",
        dry_run=False,
    )
    assert receipt.sent, receipt.error
    assert receipt.message_id

"""Pipeline stages, dedupe, feature-flag gating, dry-run guarantees, resume."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from outreach.db import session_scope, utcnow
from outreach.discovery.base import DiscoveredBusiness
from outreach.models.build import Build
from outreach.models.business import Business, CampaignBusiness
from outreach.models.campaign import Campaign
from outreach.models.enums import BuildStatus, MessageStatus, ScanStatus
from outreach.models.event import Event
from outreach.models.outreach_message import OutreachMessage
from outreach.models.scan import WebsiteScan
from outreach.pipeline.context import RunContext, StageResult
from outreach.pipeline.discover import (
    find_existing_business,
    get_or_create_campaign,
    link_to_campaign,
    persist_business,
)
from outreach.pipeline.orchestrator import run_pipeline
from outreach.state import job_lock, set_pause


def _discovered(**overrides: Any) -> DiscoveredBusiness:
    defaults: dict[str, Any] = {
        "external_source": "google_places",
        "external_id": "place-1",
        "name": "Smile Dental",
        "website_url": "https://smiledental.com/",
        "phone": "(602) 555-0100",
        "city": "Phoenix",
        "state": "AZ",
    }
    return DiscoveredBusiness(**{**defaults, **overrides})


class TestCampaigns:
    def test_created_once_then_resumed(self, db_session: Any) -> None:
        first, created = get_or_create_campaign(
            db_session, vertical="Dentist", city="Phoenix", state="az"
        )
        db_session.commit()
        second, created_again = get_or_create_campaign(
            db_session, vertical="dentist", city="Phoenix", state="AZ"
        )
        assert created and not created_again
        assert first.id == second.id

    def test_different_cities_are_different_campaigns(self, db_session: Any) -> None:
        a, _ = get_or_create_campaign(db_session, vertical="dentist", city="Phoenix", state="AZ")
        b, _ = get_or_create_campaign(db_session, vertical="dentist", city="Tucson", state="AZ")
        db_session.commit()
        assert a.id != b.id


class TestDeduplication:
    def _campaign(self, session: Any) -> Campaign:
        campaign, _ = get_or_create_campaign(
            session, vertical="dentist", city="Phoenix", state="AZ"
        )
        session.commit()
        return campaign

    def test_same_provider_id_is_one_business(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        first, created = persist_business(db_session, campaign, _discovered())
        db_session.commit()
        second, created_again = persist_business(db_session, campaign, _discovered(name="Smile Dental LLC"))
        db_session.commit()
        assert created and not created_again
        assert first.id == second.id
        assert db_session.query(Business).count() == 1

    def test_same_domain_under_a_different_listing_is_one_business(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        persist_business(db_session, campaign, _discovered())
        db_session.commit()
        _, created = persist_business(
            db_session, campaign,
            _discovered(external_id="place-2", name="Smile Dental of Phoenix",
                        website_url="https://www.smiledental.com/index.html"),
        )
        db_session.commit()
        assert not created
        assert db_session.query(Business).count() == 1

    def test_businesses_without_websites_stay_distinct(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        persist_business(db_session, campaign, _discovered(external_id="a", website_url=None))
        persist_business(db_session, campaign, _discovered(external_id="b", website_url=None,
                                                          name="Other Dental"))
        db_session.commit()
        assert db_session.query(Business).count() == 2

    def test_rediscovery_fills_in_missing_fields(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        business, _ = persist_business(
            db_session, campaign, _discovered(website_url=None, phone=None)
        )
        db_session.commit()
        persist_business(db_session, campaign, _discovered())
        db_session.commit()
        db_session.refresh(business)
        assert business.website_url == "https://smiledental.com/"
        assert business.phone == "+16025550100"

    def test_a_second_campaign_links_rather_than_duplicating(self, db_session: Any) -> None:
        first = self._campaign(db_session)
        business, _ = persist_business(db_session, first, _discovered())
        second, _ = get_or_create_campaign(db_session, vertical="dentist", city="Mesa", state="AZ")
        db_session.commit()
        assert link_to_campaign(db_session, first.id, business.id, "google_places")
        assert link_to_campaign(db_session, second.id, business.id, "google_places")
        db_session.commit()
        assert db_session.query(Business).count() == 1
        assert db_session.query(CampaignBusiness).count() == 2

    def test_relinking_the_same_campaign_increments_times_seen(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        business, _ = persist_business(db_session, campaign, _discovered())
        link_to_campaign(db_session, campaign.id, business.id, "x")
        link_to_campaign(db_session, campaign.id, business.id, "x")
        db_session.commit()
        link = db_session.query(CampaignBusiness).one()
        assert link.times_seen == 2

    def test_find_existing_prefers_the_provider_id(self, db_session: Any) -> None:
        campaign = self._campaign(db_session)
        persist_business(db_session, campaign, _discovered())
        db_session.commit()
        assert find_existing_business(db_session, _discovered(website_url=None)) is not None


class TestStageGating:
    def test_disabled_feature_yields_a_skipped_stage(self, settings: Any) -> None:
        ctx = RunContext(settings=settings)
        stage = ctx.require_feature("build", "FEATURE_SITE_BUILDING")
        assert stage is not None
        assert not stage.executed
        assert "SKIPPED_FEATURE_DISABLED" in (stage.skipped_reason or "")

    def test_enabled_feature_yields_none(self, env_settings: Any) -> None:
        ctx = RunContext(settings=env_settings(FEATURE_SITE_BUILDING="true"))
        assert ctx.require_feature("build", "FEATURE_SITE_BUILDING") is None

    def test_stage_result_counts(self) -> None:
        stage = StageResult(name="x")
        stage.bump("a")
        stage.bump("a", 2)
        assert stage.counts["a"] == 3


def _fresh_run(settings: Any, **kwargs: Any) -> Any:
    """Initialise a database and run the whole pipeline against the fixtures."""
    from outreach.db import dispose_engine, init_db

    dispose_engine()
    init_db(settings, use_alembic=False)
    defaults: dict[str, Any] = {
        "settings": settings, "vertical": "dentist", "city": "Phoenix",
        "state": "AZ", "provider": "fixtures", "dry_run": True, "use_lock": False,
    }
    return run_pipeline(**{**defaults, **kwargs})


@pytest.fixture(scope="module")
def baseline_run(tmp_path_factory: Any, fixture_server: Any) -> Any:
    """One shared safe-defaults pipeline run.

    A full run renders every fixture site at two viewports, so the read-only
    assertions below share a single run rather than paying for one each.
    """
    from outreach.config import build_settings
    from tests.conftest import BASE_ENV

    root = tmp_path_factory.mktemp("baseline")
    settings = build_settings(
        {
            **BASE_ENV,
            "DATABASE_URL": f"sqlite:///{root / 'baseline.db'}",
            "ARTIFACT_ROOT": str(root / "artifacts"),
            "PREVIEW_ROOT": str(root / "previews"),
        }
    )
    return settings, _fresh_run(settings)


@pytest.fixture(scope="module")
def build_run(tmp_path_factory: Any, fixture_server: Any) -> Any:
    """One shared run with site building enabled."""
    from outreach.config import build_settings
    from tests.conftest import BASE_ENV

    root = tmp_path_factory.mktemp("built")
    settings = build_settings(
        {
            **BASE_ENV,
            "DATABASE_URL": f"sqlite:///{root / 'built.db'}",
            "ARTIFACT_ROOT": str(root / "artifacts"),
            "PREVIEW_ROOT": str(root / "previews"),
            "FEATURE_SITE_BUILDING": "true",
            "PREVIEW_BASE_URL": "https://previews.stoopcraft-outreach.com/",
        }
    )
    return settings, _fresh_run(settings)


@pytest.mark.browser
class TestFixturePipeline:
    """Full end-to-end runs against the bundled fixture websites."""

    def test_end_to_end_with_all_flags_off(self, baseline_run: Any) -> None:
        _settings, result = baseline_run
        discover = result.stage("discover")
        scan = result.stage("scan")
        qualify = result.stage("qualify")

        assert discover.counts["new_businesses"] >= 6
        assert discover.counts["deduplicated"] >= 1, "the duplicate listing must dedupe"
        assert scan.counts["ok"] >= 5
        assert scan.counts["screenshots"] >= 5
        assert scan.counts["with_public_email"] >= 4
        assert qualify.counts["build_candidates"] >= 2
        assert qualify.counts.get("good_site", 0) >= 1, "the modern fixture must not be a candidate"

        for name in ("ai_scoring", "build", "publish"):
            stage = result.stage(name)
            assert not stage.executed
            assert "SKIPPED_FEATURE_DISABLED" in (stage.skipped_reason or "")

    def test_screenshots_and_artifacts_are_written(self, baseline_run: Any) -> None:
        db_settings, _result = baseline_run
        with session_scope(db_settings) as session:
            scan = session.scalars(
                select(WebsiteScan).where(WebsiteScan.scan_status == ScanStatus.OK)
            ).first()
            assert scan is not None
            from pathlib import Path

            assert Path(scan.desktop_screenshot_path).is_file()
            assert Path(scan.mobile_screenshot_path).is_file()
            assert Path(scan.homepage_html_path).is_file()
            assert (Path(scan.homepage_html_path).parent / "scan.json").is_file()
            assert scan.technical_site_quality_score is not None
            assert scan.technical_findings_json["findings"]

    def test_broken_site_does_not_stop_the_campaign(self, baseline_run: Any) -> None:
        _settings, result = baseline_run
        scan = result.stage("scan")
        assert scan.counts.get("status_http_error", 0) >= 1
        assert scan.counts["ok"] >= 5, "other sites must still have been scanned"

    def test_prompt_injection_site_is_treated_as_plain_content(self, baseline_run: Any) -> None:
        db_settings, _result = baseline_run
        with session_scope(db_settings) as session:
            business = session.scalars(
                select(Business).where(Business.name.like("Whitetooth%"))
            ).first()
            assert business is not None
            # Scored and qualified like any other site; the injected instructions
            # changed nothing about how it was handled.
            assert business.qualification_status in {
                "REDESIGN_CANDIDATE", "STRONG_REDESIGN_CANDIDATE", "BORDERLINE"
            }
            emails = {c.normalized_email for c in business.contacts}
            assert "reception@whitetoothdental.example" in emails

    def test_rerunning_is_idempotent(self, baseline_run: Any) -> None:
        db_settings, first = baseline_run
        with session_scope(db_settings) as session:
            businesses_after_first = session.query(Business).count()
            ok_scans_after_first = (
                session.query(WebsiteScan)
                .filter(WebsiteScan.scan_status == ScanStatus.OK)
                .count()
            )

        second = run_pipeline(
            settings=db_settings, vertical="dentist", city="Phoenix", state="AZ",
            provider="fixtures", dry_run=True, use_lock=False,
        )
        with session_scope(db_settings) as session:
            assert session.query(Business).count() == businesses_after_first
            # Successful scans are never repeated (failed ones are retried,
            # which is why the total count may grow).
            assert (
                session.query(WebsiteScan)
                .filter(WebsiteScan.scan_status == ScanStatus.OK)
                .count()
                == ok_scans_after_first
            )
        assert second.campaign_id == first.campaign_id

    def test_building_and_queueing(self, build_run: Any) -> None:
        settings, result = build_run
        build = result.stage("build")
        compose = result.stage("compose_queue")

        assert build.executed
        assert build.counts.get("built", 0) >= 1
        assert build.counts.get("qa_failed", 0) == 0
        assert compose.counts.get("queued", 0) >= 1

        with session_scope(settings) as session:
            for row in session.scalars(select(Build)).all():
                assert row.qa_passed is True
                from pathlib import Path

                index = Path(row.preview_path) / "index.html"
                assert index.is_file()
                assert "noindex" in index.read_text()
            for message in session.scalars(select(OutreachMessage)).all():
                assert message.status in {MessageStatus.QUEUED, MessageStatus.SCHEDULED}
                assert "previews.stoopcraft-outreach.com" in message.body_text
                assert settings.unsubscribe_instruction in message.body_text

    def test_events_record_every_transition(self, baseline_run: Any) -> None:
        db_settings, _result = baseline_run
        with session_scope(db_settings) as session:
            types = {e.event_type for e in session.scalars(select(Event)).all()}
        assert {"pipeline_started", "campaign_created", "business_discovered",
                "scan_completed", "qualified", "contact_found"} <= types


@pytest.fixture(scope="module")
def dry_run_with_all_flags(tmp_path_factory: Any, fixture_server: Any) -> Any:
    """A dry run with *every* dangerous flag switched on.

    ``GmailClient.send`` is replaced with a landmine for the duration, so a
    single attempted transmission fails the whole module.
    """
    from outreach.config import build_settings
    from outreach.db import dispose_engine, init_db
    from outreach.messaging.gmail import GmailClient
    from tests.conftest import BASE_ENV

    root = tmp_path_factory.mktemp("dryrun")
    publish_dir = root / "published"
    settings = build_settings(
        {
            **BASE_ENV,
            "DATABASE_URL": f"sqlite:///{root / 'dry.db'}",
            "ARTIFACT_ROOT": str(root / "artifacts"),
            "PREVIEW_ROOT": str(root / "previews"),
            "FEATURE_SITE_BUILDING": "true",
            "FEATURE_GMAIL_SEND": "true",
            "OUTREACH_SEND_ENABLED": "true",
            "FEATURE_PREVIEW_PUBLISHING": "true",
            "PREVIEW_PUBLISHER": "local",
            "PREVIEW_PUBLISH_DIR": str(publish_dir),
            "PREVIEW_BASE_URL": "https://previews.stoopcraft-outreach.com/",
        }
    )

    def exploding_send(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("a dry run attempted to send an email")

    original = GmailClient.send
    GmailClient.send = exploding_send  # type: ignore[method-assign]
    try:
        dispose_engine()
        init_db(settings, use_alembic=False)
        result = run_pipeline(
            settings=settings, vertical="dentist", city="Phoenix", state="AZ",
            provider="fixtures", dry_run=True, use_lock=False,
        )
    finally:
        GmailClient.send = original  # type: ignore[method-assign]
    return settings, result, publish_dir


@pytest.mark.browser
class TestDryRunIsSacred:
    """--dry-run must never send, whatever the flags say."""

    def test_no_email_is_sent_even_with_every_send_flag_on(self, dry_run_with_all_flags: Any) -> None:
        settings, result, _publish_dir = dry_run_with_all_flags
        send = result.stage("send")
        assert not send.executed
        assert "DRY RUN" in (send.skipped_reason or "")

        with session_scope(settings) as session:
            for message in session.scalars(select(OutreachMessage)).all():
                assert message.sent_at is None
                assert message.gmail_message_id is None
                assert message.status != MessageStatus.SENT

    def test_nothing_is_published_in_a_dry_run(self, dry_run_with_all_flags: Any) -> None:
        settings, result, publish_dir = dry_run_with_all_flags
        publish = result.stage("publish")
        assert not publish.executed
        assert "DRY RUN" in (publish.skipped_reason or "")
        assert not publish_dir.exists()

        with session_scope(settings) as session:
            assert not session.scalars(
                select(Build).where(Build.status == BuildStatus.PUBLISHED)
            ).all()

    def test_dry_run_still_reports_what_would_happen(self, dry_run_with_all_flags: Any) -> None:
        _settings, result, _dir = dry_run_with_all_flags
        assert result.stage("send").counts.get("considered", 0) >= 1


class TestLockingAndPause:
    def test_overlapping_jobs_are_prevented(self, db_settings: Any, monkeypatch: Any) -> None:
        from outreach.db import dispose_engine, init_db

        monkeypatch.setattr("outreach.config.get_settings", lambda: db_settings)
        monkeypatch.setattr("outreach.db.get_settings", lambda: db_settings)
        dispose_engine()
        init_db(db_settings, use_alembic=False)

        with job_lock("test-job", required=False) as outer:
            assert outer
            with job_lock("test-job", required=False) as inner:
                assert not inner, "a second holder must be refused"

    def test_lock_is_released_afterwards(self, db_settings: Any, monkeypatch: Any) -> None:
        from outreach.db import dispose_engine, init_db

        monkeypatch.setattr("outreach.config.get_settings", lambda: db_settings)
        monkeypatch.setattr("outreach.db.get_settings", lambda: db_settings)
        dispose_engine()
        init_db(db_settings, use_alembic=False)

        with job_lock("test-job-2", required=False) as acquired:
            assert acquired
        with job_lock("test-job-2", required=False) as acquired_again:
            assert acquired_again

    def test_pause_state_round_trips(self, db_session: Any) -> None:
        from outreach.state import get_pause_state

        assert not get_pause_state(db_session).paused
        set_pause(db_session, True, "maintenance window")
        db_session.commit()
        state = get_pause_state(db_session)
        assert state.paused and state.reason == "maintenance window"
        set_pause(db_session, False, "done")
        db_session.commit()
        assert not get_pause_state(db_session).paused


class TestCrashResume:
    def test_a_message_stranded_in_sending_is_never_auto_retried(
        self, db_session: Any, env_settings: Any
    ) -> None:
        """Simulates a crash between "SENDING" and the Gmail response."""
        from outreach.messaging.queue import pending_messages
        from outreach.models.campaign import Campaign

        campaign = Campaign(vertical="dentist", city="Phoenix", state="AZ", country="US")
        db_session.add(campaign)
        db_session.flush()
        business = Business(campaign_id=campaign.id, name="X", normalized_domain="x.com")
        db_session.add(business)
        db_session.flush()
        db_session.add(
            OutreachMessage(
                business_id=business.id, recipient_email="a@b.com", subject="s",
                body_text="b", status=MessageStatus.SENDING, sending_started_at=utcnow(),
                send_attempts=1,
            )
        )
        db_session.commit()
        # SENDING is not a pending state: recovery is a human decision.
        assert pending_messages(db_session) == []

    def test_partial_scan_resumes_on_the_next_run(self, db_session: Any) -> None:
        from outreach.pipeline.scan import businesses_to_scan

        campaign, _ = get_or_create_campaign(
            db_session, vertical="dentist", city="Phoenix", state="AZ"
        )
        db_session.commit()
        scanned, _ = persist_business(db_session, campaign, _discovered(external_id="a"))
        unscanned, _ = persist_business(
            db_session, campaign,
            _discovered(external_id="b", website_url="https://other.com/", name="Other"),
        )
        db_session.add(
            WebsiteScan(business_id=scanned.id, scan_status=ScanStatus.OK, requested_url="x")
        )
        db_session.commit()

        todo = businesses_to_scan(db_session, campaign.id, limit=10)
        assert [b.id for b in todo] == [unscanned.id]

    def test_failed_scans_are_retried(self, db_session: Any) -> None:
        from outreach.pipeline.scan import businesses_to_scan

        campaign, _ = get_or_create_campaign(
            db_session, vertical="dentist", city="Phoenix", state="AZ"
        )
        db_session.commit()
        business, _ = persist_business(db_session, campaign, _discovered())
        db_session.add(
            WebsiteScan(business_id=business.id, scan_status=ScanStatus.TIMEOUT, requested_url="x")
        )
        db_session.commit()
        assert [b.id for b in businesses_to_scan(db_session, campaign.id, limit=10)] == [business.id]

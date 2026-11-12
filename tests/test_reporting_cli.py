"""Reporting, learning-data export, and the CLI surface."""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from outreach.cli.main import app
from outreach.db import utcnow
from outreach.models.build import Build
from outreach.models.business import Business
from outreach.models.campaign import Campaign
from outreach.models.contact import Contact
from outreach.models.enums import (
    BuildStatus,
    ContactabilityStatus,
    MessageStatus,
    ProviderUsageStatus,
    QualificationStatus,
    ScanStatus,
)
from outreach.models.outreach_message import OutreachMessage
from outreach.models.provider_usage import ProviderUsage
from outreach.models.scan import WebsiteScan
from outreach.reporting import (
    ai_usage_report,
    build_funnel_report,
    failure_report,
    learning_dataset,
    today_report,
)


@pytest.fixture
def seeded(db_session: Any, settings: Any) -> Any:
    """A small but realistic campaign covering every funnel stage."""
    campaign = Campaign(vertical="dentist", city="Phoenix", state="AZ", country="US")
    db_session.add(campaign)
    db_session.flush()

    specs = [
        ("Good Dental", "good.com", 92.0, QualificationStatus.GOOD_SITE, True, False, None),
        ("Bad Dental", "bad.com", 28.0, QualificationStatus.STRONG_REDESIGN_CANDIDATE, True, True, MessageStatus.SENT),
        ("Meh Dental", "meh.com", 45.0, QualificationStatus.REDESIGN_CANDIDATE, True, True, MessageStatus.QUEUED),
        ("Quiet Dental", "quiet.com", 40.0, QualificationStatus.REDESIGN_CANDIDATE, False, False, None),
    ]
    for name, domain, score, status, has_email, has_build, message_status in specs:
        business = Business(
            campaign_id=campaign.id, name=name, website_url=f"https://{domain}/",
            normalized_domain=domain, qualification_status=str(status),
            contactability_status=str(
                ContactabilityStatus.CONTACTABLE if has_email else ContactabilityStatus.NO_PUBLIC_EMAIL
            ),
            combined_score=score, business_status="OPERATIONAL",
        )
        db_session.add(business)
        db_session.flush()
        db_session.add(
            WebsiteScan(
                business_id=business.id, scan_status=ScanStatus.OK,
                requested_url=f"https://{domain}/", technical_site_quality_score=score,
                technical_findings_json={
                    "findings": [
                        {"code": "no_viewport_meta", "category": "mobile", "severity": "high", "evidence": "x"}
                    ]
                },
            )
        )
        if has_email:
            db_session.add(
                Contact(
                    business_id=business.id, email=f"info@{domain}",
                    normalized_email=f"info@{domain}", contact_type="info", confidence=0.9,
                )
            )
        build = None
        if has_build:
            build = Build(
                business_id=business.id, template_name="dentist", template_version="1.0.0",
                status=BuildStatus.BUILT, qa_passed=True, preview_path="/tmp/x",
            )
            db_session.add(build)
            db_session.flush()
        if message_status:
            message = OutreachMessage(
                business_id=business.id, build_id=build.id if build else None,
                recipient_email=f"info@{domain}", subject="s", body_text="b",
                status=str(message_status),
            )
            if message_status == MessageStatus.SENT:
                message.sent_at = utcnow()
            db_session.add(message)

    db_session.add(
        WebsiteScan(
            business_id=None if False else 1, scan_status=ScanStatus.TIMEOUT,
            requested_url="https://dead.com/", error="timeout",
        )
    )
    db_session.add(
        ProviderUsage(
            provider="google_places", operation="searchText",
            month_key=ProviderUsage.month_key_for(),
            status=ProviderUsageStatus.ALLOWED, billable_units=4,
        )
    )
    db_session.commit()
    return campaign


class TestFunnel:
    def test_counts_every_stage(self, db_session: Any, settings: Any, seeded: Any) -> None:
        report = build_funnel_report(db_session, settings, seeded.id)
        c = report.counts
        assert c["discovered"] == 4
        assert c["with_website"] == 4
        assert c["scans_ok"] == 4
        assert c["good_site"] == 1
        assert c["redesign_candidates_total"] == 3
        assert c["with_public_email"] == 3
        assert c["builds_succeeded"] == 2
        assert c["messages_sent"] == 1
        assert c["messages_queued"] == 1
        assert c["google_places_requests_this_month"] == 4

    def test_funnel_lines_render(self, db_session: Any, settings: Any, seeded: Any) -> None:
        lines = build_funnel_report(db_session, settings, seeded.id).funnel_lines()
        assert len(lines) == 9
        assert any("discovered" in line for line in lines)

    def test_report_is_json_serialisable(self, db_session: Any, settings: Any, seeded: Any) -> None:
        json.dumps(build_funnel_report(db_session, settings, seeded.id).to_dict())

    def test_empty_database_does_not_crash(self, db_session: Any, settings: Any) -> None:
        report = build_funnel_report(db_session, settings)
        assert report.counts["discovered"] == 0


class TestOtherReports:
    def test_today_report(self, db_session: Any, settings: Any, seeded: Any) -> None:
        payload = today_report(db_session, settings)
        assert payload["daily_limit"] == settings.effective_daily_send_limit
        assert payload["sending_enabled"] is False
        assert "next_window_open" in payload

    def test_failure_report(self, db_session: Any, seeded: Any) -> None:
        payload = failure_report(db_session)
        assert any(row["status"] == ScanStatus.TIMEOUT for row in payload["failed_scans"])

    def test_ai_usage_report_on_an_empty_table(self, db_session: Any) -> None:
        payload = ai_usage_report(db_session, days=30)
        assert payload["total_calls"] == 0
        assert "score_website" in payload["known_purposes"]

    def test_learning_dataset_links_problems_to_outcomes(
        self, db_session: Any, seeded: Any
    ) -> None:
        rows = learning_dataset(db_session)
        assert rows
        sent = [r for r in rows if r["contacted"]]
        assert sent, "at least one contacted business is expected"
        assert "no_viewport_meta" in sent[0]["problem_codes"]
        assert set(rows[0]) >= {
            "business_id", "deterministic_score", "problem_codes", "contacted",
            "replied", "positive_reply", "opted_out",
        }


class TestCLI:
    """The CLI must never need a live external service to be useful."""

    def _run(self, monkeypatch: Any, db_settings: Any, args: list[str]) -> Any:
        from outreach.db import dispose_engine, init_db

        monkeypatch.setattr("outreach.cli.main._settings", lambda: db_settings)
        monkeypatch.setattr("outreach.config.get_settings", lambda: db_settings)
        monkeypatch.setattr("outreach.db.get_settings", lambda: db_settings)
        dispose_engine()
        init_db(db_settings, use_alembic=False)
        return CliRunner().invoke(app, args)

    def test_version(self) -> None:
        result = CliRunner().invoke(app, ["version"])
        assert result.exit_code == 0
        assert "stoopcraft-outreach" in result.output

    def test_features_lists_every_flag(self, monkeypatch: Any, db_settings: Any) -> None:
        result = self._run(monkeypatch, db_settings, ["features", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["flags"]["FEATURE_GMAIL_SEND"] is False
        assert payload["OUTREACH_SEND_ENABLED"] is False

    def test_config_redacts_secrets(self, monkeypatch: Any, env_settings: Any, tmp_path: Any) -> None:
        settings = env_settings(
            DATABASE_URL=f"sqlite:///{tmp_path / 'c.db'}", GOOGLE_MAPS_API_KEY="topsecret"
        )
        result = self._run(monkeypatch, settings, ["config", "--json"])
        assert result.exit_code == 0
        assert "topsecret" not in result.output

    def test_doctor_reports_without_touching_anything(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["doctor"])
        assert result.exit_code == 0
        assert "Gmail OAuth" in result.output

    def test_ai_check_reports_unavailable_cleanly(
        self, monkeypatch: Any, env_settings: Any, tmp_path: Any
    ) -> None:
        settings = env_settings(
            DATABASE_URL=f"sqlite:///{tmp_path / 'a.db'}",
            CLAUDE_EXECUTABLE="definitely-not-installed-xyz",
        )
        result = self._run(monkeypatch, settings, ["ai-check", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["available"] is False

    def test_provider_usage_shows_the_ceiling(self, monkeypatch: Any, db_settings: Any) -> None:
        result = self._run(monkeypatch, db_settings, ["provider-usage", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["google_places"]["limit"] == db_settings.effective_google_places_monthly_limit
        assert payload["google_places"]["feature_enabled"] is False

    def test_campaign_create_then_list(self, monkeypatch: Any, db_settings: Any) -> None:
        created = self._run(
            monkeypatch, db_settings,
            ["campaign", "create", "--vertical", "dentist", "--city", "Phoenix", "--state", "AZ"],
        )
        assert created.exit_code == 0
        listed = CliRunner().invoke(app, ["campaign", "list"])
        assert listed.exit_code == 0

    def test_suppression_lifecycle(self, monkeypatch: Any, db_settings: Any) -> None:
        added = self._run(
            monkeypatch, db_settings, ["suppress", "add", "a@b.com", "--reason", "requested"]
        )
        assert added.exit_code == 0
        listed = CliRunner().invoke(app, ["suppress", "list"])
        assert "a@b.com" in listed.output

        refused = CliRunner().invoke(app, ["suppress", "remove", "a@b.com"])
        assert refused.exit_code == 2, "removal must require --confirm"
        assert "a@b.com" in CliRunner().invoke(app, ["suppress", "list"]).output

        removed = CliRunner().invoke(app, ["suppress", "remove", "a@b.com", "--confirm"])
        assert removed.exit_code == 0

    def test_pause_and_resume(self, monkeypatch: Any, db_settings: Any) -> None:
        paused = self._run(monkeypatch, db_settings, ["pause", "--reason", "testing"])
        assert paused.exit_code == 0
        status = CliRunner().invoke(app, ["status", "--json"])
        assert json.loads(status.output)["paused"] is True
        CliRunner().invoke(app, ["resume"])
        assert json.loads(CliRunner().invoke(app, ["status", "--json"]).output)["paused"] is False

    def test_gmail_test_refuses_without_confirm(self, monkeypatch: Any, db_settings: Any) -> None:
        result = self._run(
            monkeypatch, db_settings, ["gmail", "test", "--to", "someone@real-domain.com"]
        )
        assert result.exit_code == 2
        assert "--confirm" in result.output

    def test_gmail_test_refuses_when_flags_are_off(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(
            monkeypatch, db_settings,
            ["gmail", "test", "--to", "someone@real-domain.com", "--confirm"],
        )
        assert result.exit_code == 2
        assert "FEATURE_GMAIL_SEND" in result.output

    def test_gmail_test_refuses_a_reserved_recipient(
        self, monkeypatch: Any, env_settings: Any, tmp_path: Any
    ) -> None:
        settings = env_settings(
            DATABASE_URL=f"sqlite:///{tmp_path / 'g.db'}",
            FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true",
        )
        result = self._run(
            monkeypatch, settings, ["gmail", "test", "--to", "a@b.example", "--confirm"]
        )
        assert result.exit_code == 2
        assert "reserved" in result.output.lower()

    def test_build_refuses_when_the_feature_is_off(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["build", "--business-id", "1"])
        assert result.exit_code == 2
        assert "FEATURE_SITE_BUILDING" in result.output

    def test_process_queue_dry_run_sends_nothing(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["process-queue", "--dry-run"])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output

    def test_sync_replies_reports_the_disabled_flag(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["sync-replies", "--json"])
        assert result.exit_code == 0
        assert "FEATURE_REPLY_SYNC" in json.loads(result.output)["skipped_reason"]

    def test_scheduler_status_and_install_help(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        status = self._run(monkeypatch, db_settings, ["scheduler", "status", "--json"])
        assert status.exit_code == 0
        assert json.loads(status.output)["scheduler_enabled"] is False

        help_output = CliRunner().invoke(app, ["scheduler", "install-help"])
        assert help_output.exit_code == 0
        assert "systemd" in help_output.output

    def test_scheduler_tick_is_inert_when_disabled(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["scheduler", "tick"])
        assert result.exit_code == 0
        assert "SKIPPED_FEATURE_DISABLED" in result.output

    def test_stats_and_today_and_failures(self, monkeypatch: Any, db_settings: Any) -> None:
        for args in (["stats", "--json"], ["today", "--json"], ["failures", "--json"]):
            result = self._run(monkeypatch, db_settings, args)
            assert result.exit_code == 0, f"{args} failed: {result.output}"
            json.loads(result.output)

    def test_previews_list_on_an_empty_directory(
        self, monkeypatch: Any, db_settings: Any
    ) -> None:
        result = self._run(monkeypatch, db_settings, ["previews", "list"])
        assert result.exit_code == 0

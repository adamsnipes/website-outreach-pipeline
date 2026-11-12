"""Composition, compliance, suppression, queue, Gmail MIME, replies, scheduling."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from outreach.db import utcnow
from outreach.messaging.compliance import (
    RESERVED_TLDS,
    SendBlockReason,
    build_compliance_footer,
    evaluate_send,
    in_send_window,
    next_window_open,
    recipient_problem,
    recipient_syntax_problem,
    sends_today,
)
from outreach.messaging.composer import (
    OBSERVATION_PHRASES,
    check_email_honesty,
    compose,
    deterministic_email,
    select_observations,
    text_to_html,
)
from outreach.messaging.gmail import (
    IDEMPOTENCY_HEADER,
    GmailClient,
    build_mime,
    extract_plain_body,
    mime_to_raw,
    new_idempotency_key,
    parse_gmail_message,
)
from outreach.messaging.queue import (
    InvalidTransition,
    enqueue,
    process_queue,
    transition,
    upsert_draft,
)
from outreach.messaging.replies import classify_reply_deterministic
from outreach.messaging.scheduler import evaluate_schedule, plan_send_times
from outreach.messaging.suppression import (
    add_suppression,
    detect_opt_out,
    is_suppressed,
    remove_suppression,
    strip_quoted_reply,
    suppress_from_reply,
)
from outreach.models.business import Business
from outreach.models.campaign import Campaign
from outreach.models.enums import MessageStatus, SuppressionReason
from outreach.models.outreach_message import OutreachMessage
from outreach.state import set_pause

FINDINGS = {
    "findings": [
        {"code": "no_viewport_meta", "category": "mobile", "severity": "high", "evidence": "no tag"},
        {"code": "no_cta_above_fold", "category": "conversion", "severity": "high", "evidence": "none"},
        {"code": "no_charset", "category": "technical", "severity": "low", "evidence": "x"},
    ]
}


def _business(session: Any, **overrides: Any) -> Business:
    campaign = session.query(Campaign).first()
    if campaign is None:
        campaign = Campaign(vertical="dentist", city="Phoenix", state="AZ", country="US")
        session.add(campaign)
        session.flush()
    business = Business(
        campaign_id=campaign.id, name=overrides.pop("name", "Test Dental"),
        website_url=overrides.pop("website_url", "https://testdental.com/"),
        normalized_domain=overrides.pop("normalized_domain", "testdental.com"),
        **overrides,
    )
    session.add(business)
    session.flush()
    return business


def _message(session: Any, settings: Any, **overrides: Any) -> OutreachMessage:
    business = overrides.pop("business", None) or _business(session)
    message = OutreachMessage(
        business_id=business.id,
        recipient_email=overrides.pop("recipient_email", "info@testdental.com"),
        subject="A quicker version of your site",
        body_text="Body",
        status=overrides.pop("status", MessageStatus.QUEUED),
        idempotency_key=new_idempotency_key(),
        **overrides,
    )
    session.add(message)
    session.flush()
    return message


# ---------------------------------------------------------------------------
class TestComposer:
    def test_uses_at_most_two_observations(self, settings: Any) -> None:
        observations = select_observations(FINDINGS)
        assert len(observations) <= 2

    def test_observations_vary_by_category(self, settings: Any) -> None:
        findings = {
            "findings": [
                {"code": "no_viewport_meta", "category": "mobile", "severity": "high", "evidence": "a"},
                {"code": "mobile_horizontal_overflow", "category": "mobile", "severity": "high", "evidence": "b"},
                {"code": "no_click_to_call", "category": "conversion", "severity": "high", "evidence": "c"},
            ]
        }
        categories = {o["category"] for o in select_observations(findings)}
        assert len(categories) == 2

    def test_only_describable_findings_are_used(self) -> None:
        findings = {"findings": [{"code": "totally_unknown_code", "category": "x", "severity": "high"}]}
        assert select_observations(findings) == []

    def test_every_phrase_maps_to_a_real_code(self) -> None:
        from outreach.scoring.qualification import HIGH_VALUE_PROBLEM_CODES

        unknown = set(OBSERVATION_PHRASES) - set(HIGH_VALUE_PROBLEM_CODES)
        assert unknown == set(), f"observation phrases without a ranked code: {unknown}"

    def test_deterministic_email_is_short_and_honest(self, settings: Any) -> None:
        email = deterministic_email(
            settings, business_name="Test Dental", city="Phoenix", vertical="dentist",
            preview_url="https://p.example/1/", observations=select_observations(FINDINGS),
        )
        assert len(email.body_text.split()) < 160
        assert "https://p.example/1/" in email.body_text
        assert settings.unsubscribe_instruction in email.body_text
        assert settings.sender_name in email.body_text
        assert settings.sender_postal_address in email.body_text
        assert check_email_honesty(email.subject, email.body_text) == []

    def test_subject_is_never_a_fake_reply(self, settings: Any) -> None:
        email = deterministic_email(
            settings, business_name="X", city="Phoenix", vertical="dentist",
            preview_url="https://p.example/1/", observations=[],
        )
        assert not email.subject.lower().startswith(("re:", "fwd:", "fw:"))

    @pytest.mark.parametrize(
        ("subject", "body"),
        [
            ("Hi", "As we discussed on the phone last week..."),
            ("Hi", "I spent hours reviewing your website."),
            ("Hi", "A colleague of yours referred me."),
            ("Hi", "I am a patient at your practice."),
            ("Hi", "Act now, this offer expires today."),
            ("Hi", "You are losing 40% of your patients."),
            ("Hi", "Your Google ranking is #4 for dentists."),
            ("Re: our chat", "Hello there."),
        ],
    )
    def test_dishonest_copy_is_detected(self, subject: str, body: str) -> None:
        assert check_email_honesty(subject, body) != []

    def test_ai_draft_with_forbidden_claims_falls_back_to_the_template(
        self, settings: Any
    ) -> None:
        class BadProvider:
            name = "fake"

            def compose_email(self, **kwargs: Any) -> Any:
                from outreach.ai.base import AIResult

                return AIResult(
                    success=True, purpose="compose_email", provider="fake",
                    parsed={
                        "subject": "Re: your website",
                        "body": "As we discussed, act now! https://p.example/1/",
                    },
                )

        email = compose(
            settings, provider=BadProvider(), use_ai=True, business_name="X", city="Phoenix",
            vertical="dentist", preview_url="https://p.example/1/", technical_findings=FINDINGS,
        )
        assert email.method == "deterministic"
        assert any("rejected" in w for w in email.warnings)

    def test_ai_draft_missing_the_preview_url_falls_back(self, settings: Any) -> None:
        class Provider:
            name = "fake"

            def compose_email(self, **kwargs: Any) -> Any:
                from outreach.ai.base import AIResult

                return AIResult(
                    success=True, purpose="compose_email", provider="fake",
                    parsed={"subject": "Hi", "body": "Nice site, let's talk."},
                )

        email = compose(
            settings, provider=Provider(), use_ai=True, business_name="X", city="Phoenix",
            vertical="dentist", preview_url="https://p.example/1/", technical_findings=FINDINGS,
        )
        assert email.method == "deterministic"

    def test_good_ai_draft_is_kept_and_gets_the_footer(self, settings: Any) -> None:
        class Provider:
            name = "fake"

            def compose_email(self, **kwargs: Any) -> Any:
                from outreach.ai.base import AIResult

                return AIResult(
                    success=True, purpose="compose_email", provider="fake",
                    parsed={
                        "subject": "A quicker version of your site",
                        "body": "Hi there,\n\nYour site loads zoomed out on phones.\n\nhttps://p.example/1/",
                    },
                )

        email = compose(
            settings, provider=Provider(), use_ai=True, business_name="X", city="Phoenix",
            vertical="dentist", preview_url="https://p.example/1/", technical_findings=FINDINGS,
        )
        assert email.method == "ai"
        assert settings.unsubscribe_instruction in email.body_text

    def test_html_alternative_has_no_tracking(self, settings: Any) -> None:
        html = text_to_html("Hello\n\nhttps://p.example/1/", "https://p.example/1/")
        assert "<img" not in html
        assert 'href="https://p.example/1/"' in html

    def test_compliance_footer_contains_identity(self, settings: Any) -> None:
        footer = build_compliance_footer(settings)
        assert settings.sender_name in footer
        assert settings.sender_postal_address in footer
        assert settings.unsubscribe_instruction in footer


# ---------------------------------------------------------------------------
class TestSuppression:
    @pytest.mark.parametrize(
        "text",
        [
            "STOP", "stop", "Please unsubscribe me", "remove me from your list",
            "take me off your list", "do not contact me again", "Don't contact us",
            "opt out please", "no further emails please", "please remove",
            "cease and desist", "stop emailing me",
        ],
    )
    def test_opt_out_language_is_detected(self, text: str) -> None:
        assert detect_opt_out(text).detected

    @pytest.mark.parametrize(
        "text",
        [
            "Sounds interesting, can you call me?", "Not interested right now",
            "stopwatch repair is our specialty", "We already have a designer",
            "Thanks, will review", "",
        ],
    )
    def test_normal_replies_are_not_opt_outs(self, text: str) -> None:
        assert not detect_opt_out(text).detected

    def test_our_own_quoted_footer_is_not_an_opt_out(self) -> None:
        reply = (
            "Sounds good, send more details.\n\n"
            "On Mon, Aug 3 2026 someone wrote:\n"
            "> Reply STOP and I will not contact you again."
        )
        assert not detect_opt_out(reply).detected

    def test_strip_quoted_reply(self) -> None:
        assert strip_quoted_reply("Hi\n\nOn Mon someone wrote:\n> old") == "Hi"

    def test_opt_out_detection_never_needs_ai(self) -> None:
        # No provider argument exists on this function at all.
        import inspect

        assert "provider" not in inspect.signature(detect_opt_out).parameters

    def test_add_and_check(self, db_session: Any) -> None:
        add_suppression(db_session, email="Info@Example.ORG", reason=SuppressionReason.REQUESTED)
        db_session.commit()
        suppressed, reason = is_suppressed(db_session, "info@example.org")
        assert suppressed and "suppressed" in reason

    def test_domain_suppression(self, db_session: Any) -> None:
        add_suppression(db_session, domain="blocked.com", reason=SuppressionReason.MANUAL)
        db_session.commit()
        assert is_suppressed(db_session, "anyone@blocked.com", check_domain=True)[0]
        assert not is_suppressed(db_session, "anyone@blocked.com", check_domain=False)[0]

    def test_invalid_addresses_are_treated_as_suppressed(self, db_session: Any) -> None:
        assert is_suppressed(db_session, "not-an-email")[0]
        assert is_suppressed(db_session, None)[0]

    def test_adding_twice_is_idempotent(self, db_session: Any) -> None:
        add_suppression(db_session, email="a@b.com")
        add_suppression(db_session, email="a@b.com")
        db_session.commit()
        from outreach.models.suppression import Suppression

        assert db_session.query(Suppression).count() == 1

    def test_removal_requires_an_explicit_call(self, db_session: Any) -> None:
        add_suppression(db_session, email="a@b.com")
        db_session.commit()
        assert is_suppressed(db_session, "a@b.com")[0]
        assert remove_suppression(db_session, email="a@b.com") == 1
        db_session.commit()
        assert not is_suppressed(db_session, "a@b.com")[0]

    def test_suppress_from_reply(self, db_session: Any) -> None:
        entry = suppress_from_reply(
            db_session, from_email="office@x.com", reply_text="please remove me"
        )
        db_session.commit()
        assert entry is not None
        assert is_suppressed(db_session, "office@x.com")[0]

    def test_non_opt_out_reply_does_not_suppress(self, db_session: Any) -> None:
        assert suppress_from_reply(db_session, from_email="a@b.com", reply_text="sounds good") is None


# ---------------------------------------------------------------------------
class TestSendGuards:
    def _all_green(self, env_settings: Any) -> Any:
        return env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")

    def test_everything_satisfied_allows_the_send(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings)
        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)  # Wed 10:00 New York
        decision = evaluate_send(db_session, settings, message, dry_run=False, authenticated=True, now=now)
        assert decision.allowed, decision.summary

    def test_dry_run_always_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings)
        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        decision = evaluate_send(db_session, settings, message, dry_run=True, authenticated=True, now=now)
        assert not decision.allowed
        assert SendBlockReason.DRY_RUN in decision.blocks

    def test_feature_flag_off_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="false", OUTREACH_SEND_ENABLED="true")
        decision = evaluate_send(
            db_session, settings, _message(db_session, settings), dry_run=False, authenticated=True
        )
        assert SendBlockReason.FEATURE_DISABLED in decision.blocks

    def test_send_enabled_off_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="false")
        decision = evaluate_send(
            db_session, settings, _message(db_session, settings), dry_run=False, authenticated=True
        )
        assert SendBlockReason.SEND_DISABLED in decision.blocks

    def test_unauthenticated_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        decision = evaluate_send(
            db_session, settings, _message(db_session, settings), dry_run=False, authenticated=False
        )
        assert SendBlockReason.NO_AUTH in decision.blocks

    def test_incomplete_compliance_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(
            FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true", SENDER_POSTAL_ADDRESS=""
        )
        decision = evaluate_send(
            db_session, settings, _message(db_session, settings), dry_run=False, authenticated=True
        )
        assert SendBlockReason.COMPLIANCE_INCOMPLETE in decision.blocks

    def test_global_pause_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings)
        set_pause(db_session, True, "maintenance")
        db_session.commit()
        decision = evaluate_send(db_session, settings, message, dry_run=False, authenticated=True)
        assert SendBlockReason.SYSTEM_PAUSED in decision.blocks

    def test_suppressed_recipient_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings, recipient_email="blocked@testdental.com")
        add_suppression(db_session, email="blocked@testdental.com")
        db_session.commit()
        decision = evaluate_send(db_session, settings, message, dry_run=False, authenticated=True)
        assert SendBlockReason.SUPPRESSED in decision.blocks

    def test_already_sent_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings, status=MessageStatus.SENT)
        message.sent_at = utcnow()
        db_session.flush()
        decision = evaluate_send(db_session, settings, message, dry_run=False, authenticated=True)
        assert SendBlockReason.ALREADY_SENT in decision.blocks

    def test_existing_gmail_id_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings)
        message.gmail_message_id = "abc123"
        db_session.flush()
        decision = evaluate_send(db_session, settings, message, dry_run=False, authenticated=True)
        assert SendBlockReason.ALREADY_SENT in decision.blocks

    def test_daily_limit_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(
            FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true", OUTREACH_DAILY_SEND_LIMIT="1"
        )
        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        first = _message(db_session, settings, status=MessageStatus.SENT)
        first.sent_at = now - timedelta(hours=1)
        db_session.flush()
        second = _message(db_session, settings, business=_business(db_session, name="Other",
                                                                  normalized_domain="other.com"))
        decision = evaluate_send(
            db_session, settings, second, dry_run=False, authenticated=True, now=now
        )
        assert SendBlockReason.DAILY_LIMIT in decision.blocks

    def test_minimum_interval_blocks(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(
            FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true",
            OUTREACH_MIN_SECONDS_BETWEEN_SENDS="900",
        )
        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        first = _message(db_session, settings, status=MessageStatus.SENT)
        first.sent_at = now - timedelta(seconds=60)
        db_session.flush()
        second = _message(db_session, settings, business=_business(db_session, name="Other",
                                                                  normalized_domain="other2.com"))
        decision = evaluate_send(
            db_session, settings, second, dry_run=False, authenticated=True, now=now
        )
        assert SendBlockReason.RATE_LIMIT in decision.blocks
        assert decision.retry_after is not None

    def test_outside_window_blocks_but_schedules(self, db_session: Any, env_settings: Any) -> None:
        settings = self._all_green(env_settings)
        message = _message(db_session, settings)
        night = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)  # 01:00 New York
        decision = evaluate_send(
            db_session, settings, message, dry_run=False, authenticated=True, now=night
        )
        assert SendBlockReason.OUTSIDE_WINDOW in decision.blocks
        assert decision.retry_after is not None

    @pytest.mark.parametrize("tld", sorted(RESERVED_TLDS))
    def test_reserved_tlds_are_refused_at_the_transport(self, tld: str) -> None:
        assert recipient_problem(f"someone@fixture.{tld}") is not None

    def test_reserved_tlds_do_not_block_composition(self) -> None:
        assert recipient_syntax_problem("someone@fixture.example") is None

    def test_role_mailboxes_are_refused(self) -> None:
        assert recipient_problem("noreply@real.com") is not None
        assert recipient_syntax_problem("noreply@real.com") is not None


class TestSendWindow:
    def test_inside_the_window(self, settings: Any) -> None:
        inside, _ = in_send_window(settings, datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        assert inside

    def test_before_and_after(self, settings: Any) -> None:
        assert not in_send_window(settings, datetime(2026, 8, 26, 11, 0, tzinfo=UTC))[0]
        assert not in_send_window(settings, datetime(2026, 8, 26, 22, 0, tzinfo=UTC))[0]

    def test_weekends_are_excluded(self, settings: Any) -> None:
        inside, why = in_send_window(settings, datetime(2026, 8, 29, 14, 0, tzinfo=UTC))
        assert not inside and "Saturday" in why

    def test_next_window_is_in_the_future(self, settings: Any) -> None:
        night = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
        assert next_window_open(settings, night) > night

    def test_planned_slots_respect_the_interval(self, env_settings: Any) -> None:
        settings = env_settings(OUTREACH_MIN_SECONDS_BETWEEN_SENDS="900",
                                OUTREACH_DAILY_SEND_LIMIT="5")
        slots = plan_send_times(settings, 3, start=datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        assert len(slots) == 3
        gaps = [(b - a).total_seconds() for a, b in pairwise(slots)]
        assert all(g >= 900 for g in gaps)

    def test_slots_roll_over_the_daily_limit(self, env_settings: Any) -> None:
        settings = env_settings(OUTREACH_DAILY_SEND_LIMIT="2")
        slots = plan_send_times(settings, 4, start=datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        assert len({s.date() for s in slots}) > 1


# ---------------------------------------------------------------------------
class TestQueue:
    def test_one_message_per_business_ever(self, db_session: Any, settings: Any) -> None:
        business = _business(db_session)
        first = upsert_draft(
            db_session, business_id=business.id, build_id=None,
            recipient_email="a@b.com", subject="S", body_text="B", body_html=None,
            composition_method="deterministic",
        )
        assert first.created
        second = upsert_draft(
            db_session, business_id=business.id, build_id=None,
            recipient_email="a@b.com", subject="S2", body_text="B2", body_html=None,
            composition_method="deterministic",
        )
        db_session.commit()
        assert not second.created
        assert second.message.id == first.message.id
        assert db_session.query(OutreachMessage).count() == 1

    def test_a_sent_message_is_never_recomposed(self, db_session: Any, settings: Any) -> None:
        business = _business(db_session)
        message = _message(db_session, settings, business=business, status=MessageStatus.SENT)
        message.subject = "Original"
        db_session.flush()
        outcome = upsert_draft(
            db_session, business_id=business.id, build_id=None,
            recipient_email="a@b.com", subject="Rewritten", body_text="x", body_html=None,
            composition_method="ai",
        )
        assert outcome.action == "unchanged"
        assert outcome.message.subject == "Original"

    def test_database_rejects_a_second_row(self, db_session: Any, settings: Any) -> None:
        from sqlalchemy.exc import IntegrityError

        business = _business(db_session)
        _message(db_session, settings, business=business)
        db_session.commit()
        db_session.add(
            OutreachMessage(
                business_id=business.id, recipient_email="x@y.com", subject="s",
                body_text="b", status=MessageStatus.DRAFT,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_valid_transition(self, db_session: Any, settings: Any) -> None:
        message = _message(db_session, settings, status=MessageStatus.DRAFT)
        transition(db_session, message, MessageStatus.QUEUED)
        assert message.status == MessageStatus.QUEUED

    def test_invalid_transition_is_refused(self, db_session: Any, settings: Any) -> None:
        message = _message(db_session, settings, status=MessageStatus.DRAFT)
        with pytest.raises(InvalidTransition):
            transition(db_session, message, MessageStatus.SENT)

    def test_enqueue_with_a_schedule(self, db_session: Any, settings: Any) -> None:
        message = _message(db_session, settings, status=MessageStatus.DRAFT)
        when = utcnow() + timedelta(hours=2)
        enqueue(db_session, message, scheduled_at=when)
        assert message.status == MessageStatus.SCHEDULED
        assert message.scheduled_at == when


class TestProcessQueue:
    def test_dry_run_never_sends(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        _message(db_session, settings)
        db_session.commit()

        class ExplodingClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

            def send(self, **kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("a dry run must never transmit")

        result = process_queue(db_session, settings, dry_run=True, client=ExplodingClient())
        assert result.sent == 0

    def test_disabled_features_hold_the_message(self, db_session: Any, settings: Any) -> None:
        message = _message(db_session, settings)
        db_session.commit()
        result = process_queue(db_session, settings, dry_run=False)
        assert result.sent == 0
        assert message.status == MessageStatus.QUEUED, "a held message must not be lost"

    def test_out_of_window_defers_rather_than_dropping(
        self, db_session: Any, env_settings: Any
    ) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        message = _message(db_session, settings)
        db_session.commit()

        class FakeClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

        night = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
        result = process_queue(db_session, settings, dry_run=False, client=FakeClient(), now=night)
        assert result.deferred == 1
        assert message.status == MessageStatus.SCHEDULED
        assert message.scheduled_at is not None

    def test_successful_send_records_ids(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        message = _message(db_session, settings)
        db_session.commit()

        class FakeClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

            def send(self, **kwargs: Any) -> Any:
                from outreach.messaging.gmail import SendReceipt

                return SendReceipt(True, message_id="m1", thread_id="t1")

        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        result = process_queue(db_session, settings, dry_run=False, client=FakeClient(), now=now)
        assert result.sent == 1
        db_session.refresh(message)
        assert message.status == MessageStatus.SENT
        assert message.gmail_message_id == "m1"
        assert message.sent_at is not None

    def test_ambiguous_failure_parks_for_review(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        message = _message(db_session, settings)
        db_session.commit()

        class FlakyClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

            def send(self, **kwargs: Any) -> Any:
                from outreach.messaging.gmail import SendReceipt

                return SendReceipt(False, ambiguous=True, error="connection reset")

        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        result = process_queue(db_session, settings, dry_run=False, client=FlakyClient(), now=now)
        assert result.needs_review == 1
        db_session.refresh(message)
        assert message.status == MessageStatus.NEEDS_REVIEW
        assert "AMBIGUOUS" in (message.failure_reason or "")

    def test_a_needs_review_message_is_never_retried(
        self, db_session: Any, env_settings: Any
    ) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        _message(db_session, settings, status=MessageStatus.NEEDS_REVIEW)
        db_session.commit()

        class ExplodingClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

            def send(self, **kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("must not retry an ambiguous send")

        result = process_queue(db_session, settings, dry_run=False, client=ExplodingClient())
        assert result.considered == 0

    def test_suppressed_recipient_is_skipped_permanently(
        self, db_session: Any, env_settings: Any
    ) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        message = _message(db_session, settings, recipient_email="blocked@testdental.com")
        add_suppression(db_session, email="blocked@testdental.com")
        db_session.commit()

        class FakeClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        process_queue(db_session, settings, dry_run=False, client=FakeClient(), now=now)
        db_session.refresh(message)
        assert message.status == MessageStatus.SKIPPED

    def test_only_one_message_per_pass(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_GMAIL_SEND="true", OUTREACH_SEND_ENABLED="true")
        for i in range(3):
            _message(
                db_session, settings,
                business=_business(db_session, name=f"B{i}", normalized_domain=f"b{i}.com"),
                recipient_email=f"a{i}@b{i}.com",
            )
        db_session.commit()

        sends = {"n": 0}

        class FakeClient:
            def status(self) -> Any:
                from outreach.messaging.gmail import GmailStatus

                return GmailStatus(True, "ok", email_address="me@x.com")

            def send(self, **kwargs: Any) -> Any:
                from outreach.messaging.gmail import SendReceipt

                sends["n"] += 1
                return SendReceipt(True, message_id=f"m{sends['n']}", thread_id="t")

        now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
        process_queue(db_session, settings, dry_run=False, client=FakeClient(), now=now)
        assert sends["n"] == 1, "the minimum interval is enforced by sending one per pass"


# ---------------------------------------------------------------------------
class TestGmailMIME:
    def test_multipart_with_headers(self) -> None:
        key = new_idempotency_key()
        message = build_mime(
            to_email="a@b.com", subject="Hi", body_text="Text body",
            body_html="<p>Text body</p>", sender_name="Aly", sender_email="me@x.com",
            reply_to="me@x.com", idempotency_key=key, campaign_label="c1",
        )
        assert message["To"] == "a@b.com"
        assert message["From"] == "Aly <me@x.com>"
        assert message[IDEMPOTENCY_HEADER] == key
        assert "List-Unsubscribe" in message
        assert {p.get_content_type() for p in message.walk()} >= {"text/plain", "text/html"}

    def test_plain_text_only(self) -> None:
        message = build_mime(
            to_email="a@b.com", subject="Hi", body_text="Text", body_html=None,
            sender_name="", sender_email="me@x.com",
        )
        assert message.get_content_type() == "text/plain"
        assert message["From"] == "me@x.com"

    def test_raw_is_url_safe_base64(self) -> None:
        message = build_mime(
            to_email="a@b.com", subject="Hi", body_text="T", body_html=None,
            sender_name="A", sender_email="me@x.com",
        )
        raw = mime_to_raw(message)
        assert "+" not in raw and "/" not in raw
        assert b"a@b.com" in base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    def test_idempotency_keys_are_unique(self) -> None:
        assert len({new_idempotency_key() for _ in range(100)}) == 100

    def test_parse_inbound_message(self) -> None:
        payload = {
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
            "internalDate": "1700000000000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Bob <bob@x.com>"},
                    {"name": "Subject", "value": "Re: hi"},
                ],
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(b"Please remove me").decode()},
                    }
                ],
            },
        }
        message = parse_gmail_message(payload)
        assert message.from_email == "bob@x.com"
        assert message.body_text == "Please remove me"
        assert message.internal_date is not None

    def test_html_only_body_is_stripped_of_tags(self) -> None:
        part = {
            "mimeType": "text/html",
            "body": {"data": base64.urlsafe_b64encode(b"<p>Hello <b>there</b></p>").decode()},
        }
        assert "Hello" in extract_plain_body(part)
        assert "<p>" not in extract_plain_body(part)

    def test_send_refuses_during_a_dry_run(self, settings: Any) -> None:
        from outreach.messaging.gmail import GmailError

        client = GmailClient(settings, service=object())
        with pytest.raises(GmailError, match="DRY RUN"):
            client.send(
                to_email="a@b.com", subject="s", body_text="b", body_html=None,
                idempotency_key="k", dry_run=True,
            )

    def test_status_without_a_token(self, settings: Any) -> None:
        status = GmailClient(settings).status()
        assert status.authenticated is False
        assert "token" in status.reason.lower()


# ---------------------------------------------------------------------------
class TestReplyClassification:
    @pytest.mark.parametrize(
        ("subject", "body", "sender", "expected"),
        [
            ("Re: site", "Please remove me from your list", "", "opt_out"),
            ("Re: site", "STOP", "", "opt_out"),
            ("Re: site", "Sounds good, how much would this cost?", "", "positive"),
            ("Re: site", "Interested, can you call me?", "", "positive"),
            ("Re: site", "Not interested, thanks", "", "negative"),
            ("Re: site", "We already have a designer", "", "negative"),
            ("Delivery Status Notification", "Address not found", "mailer-daemon@google.com", "bounced"),
            ("Automatic reply: Out of Office", "Back Monday", "", "other"),
            ("Re: site", "Thanks!", "", "other"),
        ],
    )
    def test_deterministic_classification(
        self, subject: str, body: str, sender: str, expected: str
    ) -> None:
        assert classify_reply_deterministic(subject, body, sender).classification == expected

    def test_opt_out_beats_everything(self) -> None:
        result = classify_reply_deterministic("Re", "This looks great! But please remove me.", "")
        assert result.classification == "opt_out"


# ---------------------------------------------------------------------------
class TestScheduler:
    def test_disabled_scheduler_does_nothing(self, db_session: Any, settings: Any) -> None:
        decision = evaluate_schedule(db_session, settings)
        assert not decision.enabled
        assert not decision.should_process_queue
        assert any("FEATURE_SCHEDULER" in r for r in decision.reasons)

    def test_enabled_scheduler_with_queued_work(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_SCHEDULER="true")
        _message(db_session, settings)
        db_session.commit()
        decision = evaluate_schedule(db_session, settings, now=datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        assert decision.enabled
        assert decision.should_process_queue

    def test_pause_stops_the_scheduler(self, db_session: Any, env_settings: Any) -> None:
        settings = env_settings(FEATURE_SCHEDULER="true")
        _message(db_session, settings)
        set_pause(db_session, True, "maintenance")
        db_session.commit()
        decision = evaluate_schedule(db_session, settings, now=datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        assert not decision.should_process_queue
        assert any("paused" in r for r in decision.reasons)

    def test_cron_examples_do_not_install_anything(self) -> None:
        from outreach.messaging.scheduler import CRON_EXAMPLES, SYSTEMD_EXAMPLES

        assert "crontab -e" not in CRON_EXAMPLES
        assert "never edits the host" in SYSTEMD_EXAMPLES


def test_sends_today_counts_only_todays_sends(db_session: Any, settings: Any) -> None:
    now = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
    old = _message(db_session, settings, status=MessageStatus.SENT)
    old.sent_at = now - timedelta(days=2)
    recent = _message(
        db_session, settings,
        business=_business(db_session, name="B2", normalized_domain="b2.com"),
        status=MessageStatus.SENT,
    )
    recent.sent_at = now - timedelta(hours=1)
    db_session.commit()
    assert sends_today(db_session, settings, now) == 1

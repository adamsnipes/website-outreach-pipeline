"""Feature flags, defensive limits and configuration coherence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from outreach.config import (
    CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT,
    CODE_ABSOLUTE_MAX_GOOGLE_PLACES_MONTHLY_LIMIT,
    FEATURE_FLAG_DOCS,
    FEATURE_FLAG_NAMES,
    FeatureFlags,
    build_settings,
    parse_hhmm,
)


class TestFeatureFlags:
    def test_every_documented_flag_exists_and_defaults_off(self) -> None:
        flags = FeatureFlags.from_env({})
        env = flags.as_env_dict()
        assert set(env) == set(FEATURE_FLAG_NAMES)
        assert not any(env.values()), "every feature flag must default to false"

    def test_docs_cover_every_flag(self) -> None:
        assert set(FEATURE_FLAG_DOCS) == set(FEATURE_FLAG_NAMES)

    @pytest.mark.parametrize("truthy", ["true", "TRUE", "1", "yes", "on", "Y"])
    def test_truthy_parsing(self, truthy: str) -> None:
        assert FeatureFlags.from_env({"FEATURE_AI_SCORING": truthy}).ai_scoring is True

    @pytest.mark.parametrize("falsy", ["false", "0", "no", "off", "", "banana"])
    def test_falsy_parsing(self, falsy: str) -> None:
        assert FeatureFlags.from_env({"FEATURE_AI_SCORING": falsy}).ai_scoring is False

    def test_is_enabled_by_env_name(self) -> None:
        flags = FeatureFlags.from_env({"FEATURE_GMAIL_SEND": "true"})
        assert flags.is_enabled("FEATURE_GMAIL_SEND")
        assert not flags.is_enabled("FEATURE_REPLY_SYNC")
        with pytest.raises(KeyError):
            flags.is_enabled("FEATURE_NOT_REAL")

    def test_send_is_off_by_default(self) -> None:
        assert build_settings({}).outreach_send_enabled is False


class TestDefensiveLimits:
    def test_daily_limit_cannot_exceed_hard_max(self) -> None:
        s = build_settings(
            {"OUTREACH_DAILY_SEND_LIMIT": "500", "OUTREACH_HARD_MAX_DAILY_SEND_LIMIT": "50"}
        )
        assert s.effective_daily_send_limit == 50

    def test_hard_max_cannot_exceed_the_code_constant(self) -> None:
        # Raising this further deliberately requires a code change.
        s = build_settings(
            {"OUTREACH_DAILY_SEND_LIMIT": "9999", "OUTREACH_HARD_MAX_DAILY_SEND_LIMIT": "9999"}
        )
        assert s.hard_max_daily_send_limit == CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT
        assert s.effective_daily_send_limit == CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT

    def test_min_interval_has_a_floor(self) -> None:
        s = build_settings({"OUTREACH_MIN_SECONDS_BETWEEN_SENDS": "0"})
        assert s.effective_min_seconds_between_sends >= 30

    def test_google_places_limit_is_capped_in_code(self) -> None:
        s = build_settings({"GOOGLE_PLACES_MONTHLY_REQUEST_LIMIT": "9999999"})
        assert s.effective_google_places_monthly_limit == CODE_ABSOLUTE_MAX_GOOGLE_PLACES_MONTHLY_LIMIT

    def test_zero_daily_limit_is_respected(self) -> None:
        assert build_settings({"OUTREACH_DAILY_SEND_LIMIT": "0"}).effective_daily_send_limit == 0


class TestThresholds:
    def test_incoherent_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_settings(
                {
                    "SITE_SCORE_GOOD_THRESHOLD": "30",
                    "SITE_SCORE_BORDERLINE_THRESHOLD": "55",
                    "SITE_SCORE_REDESIGN_THRESHOLD": "70",
                }
            )

    def test_legacy_misspelled_alias_is_accepted(self) -> None:
        s = build_settings({"SITE_SCORE_REDIGN_THRESHOLD": "40"})
        assert s.site_score_redesign_threshold == 40

    def test_correct_spelling_wins_over_the_alias(self) -> None:
        s = build_settings(
            {"SITE_SCORE_REDIGN_THRESHOLD": "40", "SITE_SCORE_REDESIGN_THRESHOLD": "25"}
        )
        assert s.site_score_redesign_threshold == 25

    def test_weights_normalise(self) -> None:
        s = build_settings({"DETERMINISTIC_SCORE_WEIGHT": "3", "AI_SCORE_WEIGHT": "1"})
        det, ai = s.normalized_weights
        assert det == pytest.approx(0.75)
        assert ai == pytest.approx(0.25)

    def test_zero_total_weight_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_settings({"DETERMINISTIC_SCORE_WEIGHT": "0", "AI_SCORE_WEIGHT": "0"})


class TestSendWindow:
    def test_parse_hhmm(self) -> None:
        assert parse_hhmm("09:30").hour == 9
        for bad in ["9", "25:00", "09:99", "abc", "09-30"]:
            with pytest.raises(ValueError):
                parse_hhmm(bad)

    def test_invalid_timezone_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_settings({"OUTREACH_TIMEZONE": "Mars/Olympus_Mons"})

    def test_send_days_parsing(self) -> None:
        assert build_settings({"OUTREACH_SEND_DAYS": "MON,WED,FRI"}).send_weekdays == {0, 2, 4}
        assert build_settings({"OUTREACH_SEND_DAYS": "garbage"}).send_weekdays == {0, 1, 2, 3, 4}


class TestCompliance:
    def test_missing_sender_identity_is_reported(self) -> None:
        problems = build_settings({"SENDER_COMPANY": "X"}).compliance_problems()
        assert any("SENDER_NAME" in p for p in problems)
        assert any("SENDER_POSTAL_ADDRESS" in p for p in problems)

    def test_complete_identity_has_no_problems(self) -> None:
        s = build_settings(
            {
                "SENDER_NAME": "A", "SENDER_COMPANY": "B", "SENDER_POSTAL_ADDRESS": "C",
                "UNSUBSCRIBE_INSTRUCTION": "Reply STOP.",
            }
        )
        assert s.compliance_problems() == []


def test_public_dict_redacts_secrets() -> None:
    s = build_settings({"GOOGLE_MAPS_API_KEY": "super-secret-key-value"})
    dumped = s.public_dict()
    assert "super-secret-key-value" not in str(dumped)
    assert dumped["GOOGLE_MAPS_API_KEY"].startswith("<set:")


def test_sqlite_relative_paths_resolve_against_the_project_root() -> None:
    url = build_settings({"DATABASE_URL": "sqlite:///data/x.db"}).sqlalchemy_url()
    assert url.startswith("sqlite:////")
    assert build_settings({"DATABASE_URL": "sqlite:///:memory:"}).sqlalchemy_url() == (
        "sqlite:///:memory:"
    )

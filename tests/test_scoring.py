"""Deterministic scoring, score combination and qualification."""

from __future__ import annotations

from typing import Any

import pytest

from outreach.config import build_settings
from outreach.models.ai_assessment import AIWebsiteAssessment
from outreach.models.enums import ContactabilityStatus, QualificationStatus, ScanStatus
from outreach.scoring.qualification import (
    classify_score,
    combine_scores,
    email_is_third_party,
    qualify,
    rank_problems,
)
from outreach.scoring.schemas import AIWebsiteScore
from outreach.scoring.technical import score_website

MODERN = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cactus Modern Dental - Phoenix AZ</title>
<meta name="description" content="Family dentistry in Phoenix.">
<style>@media(max-width:600px){body{font-size:16px}}</style></head>
<body><header><nav><a href="#s">Services</a></nav></header>
<main><h1>Modern dentistry in Phoenix</h1><h2>Services</h2>
<p>Cleanings, fillings, crowns and implants. 1120 E Camelback Rd, Phoenix, AZ 85014.</p>
<p>Mon-Thu 8:00-18:00</p>
<a href="tel:+16025550188">Call (602) 555-0188</a>
<a href="#book">Book an appointment</a>
<form><input name="n"><button type="submit">Send</button></form>
</main><footer>1120 E Camelback Rd, Phoenix, AZ 85014</footer></body></html>"""

TERRIBLE = """<html><head><title>Old</title></head>
<body bgcolor="#fff"><center><font size="2">
<table width="980" cellpadding="4" border="0"><tr><td>Welcome</td></tr>
<tr><td>Call 602-555-0134</td></tr></table></font></center></body></html>"""


class TestTechnicalScoring:
    def test_modern_site_scores_well(self) -> None:
        score = score_website(MODERN, "https://cactus.com/")
        assert score.site_quality_score > 65
        assert "no_viewport_meta" not in score.problem_codes

    def test_outdated_site_scores_poorly(self) -> None:
        score = score_website(TERRIBLE, "http://old.com/")
        assert score.site_quality_score < 50
        assert "no_viewport_meta" in score.problem_codes

    def test_modern_beats_outdated(self) -> None:
        assert (
            score_website(MODERN, "https://a.com/").site_quality_score
            > score_website(TERRIBLE, "http://b.com/").site_quality_score
        )

    def test_findings_json_is_transparent(self) -> None:
        payload = score_website(TERRIBLE, "http://old.com/").to_findings_json()
        assert set(payload) >= {"mobile", "technical", "conversion", "performance", "findings"}
        assert "viewport_meta" in payload["mobile"]
        assert "score" in payload["mobile"]
        assert payload["mobile"]["viewport_meta"] is False

    def test_every_finding_carries_evidence(self) -> None:
        for finding in score_website(TERRIBLE, "http://old.com/").findings:
            assert finding.evidence, f"{finding.code} has no evidence"
            assert finding.severity in {"low", "medium", "high"}

    def test_https_is_penalised_for_public_hosts(self) -> None:
        secure = score_website(MODERN, "https://a.com/")
        insecure = score_website(MODERN, "http://a.com/")
        assert insecure.technical.score < secure.technical.score
        assert "no_https" in insecure.problem_codes

    def test_https_check_is_skipped_for_loopback(self) -> None:
        local = score_website(MODERN, "http://127.0.0.1:8765/site/")
        assert "no_https" not in local.problem_codes
        assert local.technical.signals["https_check_skipped"] is True

    def test_table_layout_is_detected(self) -> None:
        assert "table_layout" in score_website(TERRIBLE, "http://old.com/").problem_codes

    def test_obsolete_tags_are_detected(self) -> None:
        codes = score_website(TERRIBLE, "http://old.com/").problem_codes
        assert "obsolete_html_tags" in codes

    def test_missing_conversion_affordances(self) -> None:
        codes = score_website(TERRIBLE, "http://old.com/").problem_codes
        assert "no_click_to_call" in codes
        assert "no_contact_form" in codes

    def test_copyright_year_is_not_used_as_evidence(self) -> None:
        old_year = MODERN.replace("</footer>", "Copyright 1998</footer>")
        assert score_website(old_year, "https://a.com/").site_quality_score == pytest.approx(
            score_website(MODERN, "https://a.com/").site_quality_score, abs=0.01
        )

    def test_scores_stay_in_range(self) -> None:
        for html, url in [(MODERN, "https://a.com/"), (TERRIBLE, "http://b.com/"), ("", "http://c.com/")]:
            score = score_website(html, url)
            assert 0 <= score.site_quality_score <= 100
            for category in (score.mobile, score.technical, score.conversion, score.performance):
                assert 0 <= category.score <= 100

    def test_html_only_scoring_is_flagged(self) -> None:
        score = score_website(MODERN, "https://a.com/")
        assert score.measured_with_browser is False
        assert any("HTML only" in note for note in score.notes)

    def test_empty_html_does_not_crash(self) -> None:
        assert score_website("", "https://a.com/").site_quality_score >= 0


class TestAISchemaValidation:
    def test_valid_payload(self) -> None:
        score = AIWebsiteScore.model_validate(
            {
                "visual_modernity": 20, "mobile_visual_quality": 10,
                "conversion_quality": 30, "trust_quality": 40,
                "dated_probability": 0.9, "redesign_opportunity": 80,
                "issues": [{"category": "mobile", "severity": "high", "evidence": "overflow"}],
                "summary": "dated",
            }
        )
        assert score.site_score == 25.0

    def test_percentage_probability_is_coerced(self) -> None:
        payload = {
            "visual_modernity": "20", "mobile_visual_quality": 10, "conversion_quality": 30,
            "trust_quality": 40, "dated_probability": "90%", "redesign_opportunity": 80,
        }
        assert AIWebsiteScore.model_validate(payload).dated_probability == pytest.approx(0.9)

    def test_out_of_range_values_are_clamped(self) -> None:
        payload = {
            "visual_modernity": 500, "mobile_visual_quality": -20, "conversion_quality": 30,
            "trust_quality": 40, "dated_probability": 900, "redesign_opportunity": 80,
        }
        score = AIWebsiteScore.model_validate(payload)
        assert score.visual_modernity == 100
        assert score.mobile_visual_quality == 0
        assert score.dated_probability == 1.0

    def test_bare_percentage_probability_is_read_as_a_percentage(self) -> None:
        payload = {
            "visual_modernity": 1, "mobile_visual_quality": 1, "conversion_quality": 1,
            "trust_quality": 1, "dated_probability": 90, "redesign_opportunity": 1,
        }
        assert AIWebsiteScore.model_validate(payload).dated_probability == pytest.approx(0.9)

    def test_garbage_is_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AIWebsiteScore.model_validate({"visual_modernity": "banana"})

    def test_unknown_severity_falls_back_to_medium(self) -> None:
        score = AIWebsiteScore.model_validate(
            {
                "visual_modernity": 1, "mobile_visual_quality": 1, "conversion_quality": 1,
                "trust_quality": 1, "dated_probability": 0, "redesign_opportunity": 1,
                "issues": [{"category": "x", "severity": "catastrophic", "evidence": "e"}],
            }
        )
        assert score.issues[0].severity == "medium"


class TestClassificationAndCombination:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (95, QualificationStatus.GOOD_SITE),
            (70, QualificationStatus.GOOD_SITE),
            (60, QualificationStatus.BORDERLINE),
            (40, QualificationStatus.REDESIGN_CANDIDATE),
            (20, QualificationStatus.STRONG_REDESIGN_CANDIDATE),
        ],
    )
    def test_bands(self, value: float, expected: QualificationStatus) -> None:
        assert classify_score(build_settings({}), value) == expected

    def test_combination_uses_configured_weights(self) -> None:
        settings = build_settings(
            {"DETERMINISTIC_SCORE_WEIGHT": "0.6", "AI_SCORE_WEIGHT": "0.4"}
        )
        combined, weights = combine_scores(settings, 50.0, 100.0)
        assert combined == pytest.approx(70.0)
        assert weights == {"deterministic": 0.6, "ai": 0.4}

    def test_works_without_ai(self) -> None:
        combined, weights = combine_scores(build_settings({}), 42.0, None)
        assert combined == 42.0
        assert weights["ai"] == 0.0

    def test_works_with_only_ai(self) -> None:
        combined, _ = combine_scores(build_settings({}), None, 42.0)
        assert combined == 42.0

    def test_no_scores_at_all(self) -> None:
        combined, _ = combine_scores(build_settings({}), None, None)
        assert combined is None


class TestQualification:
    def _qualify(self, **overrides: Any) -> Any:
        defaults: dict[str, Any] = {
            "scan_status": ScanStatus.OK,
            "deterministic_score": 30.0,
            "technical_findings": {
                "findings": [
                    {"code": "no_viewport_meta", "category": "mobile", "severity": "high", "evidence": "e"},
                    {"code": "no_click_to_call", "category": "conversion", "severity": "high", "evidence": "e"},
                ]
            },
            "assessment": None,
            "has_website": True,
            "business_status": "OPERATIONAL",
            "contacts": [{"normalized_email": "info@smile.com", "confidence": 0.9}],
            "site_domain": "smile.com",
            "already_built": False,
            "already_messaged_status": None,
            "suppressed": False,
        }
        return qualify(build_settings({}), **{**defaults, **overrides})

    def test_contactable_redesign_candidate_is_a_build_candidate(self) -> None:
        result = self._qualify()
        assert result.qualification_status == QualificationStatus.STRONG_REDESIGN_CANDIDATE
        assert result.contactability_status == ContactabilityStatus.CONTACTABLE
        assert result.is_build_candidate

    def test_good_sites_are_never_build_candidates(self) -> None:
        result = self._qualify(deterministic_score=90.0)
        assert result.qualification_status == QualificationStatus.GOOD_SITE
        assert not result.is_build_candidate

    def test_no_public_email_blocks_building(self) -> None:
        result = self._qualify(contacts=[])
        assert result.contactability_status == ContactabilityStatus.NO_PUBLIC_EMAIL
        assert not result.is_build_candidate
        assert any("email" in b for b in result.blockers)

    def test_suppressed_blocks_building(self) -> None:
        result = self._qualify(suppressed=True)
        assert result.contactability_status == ContactabilityStatus.SUPPRESSED
        assert not result.is_build_candidate

    def test_closed_business_blocks_building(self) -> None:
        assert not self._qualify(business_status="CLOSED_PERMANENTLY").is_build_candidate

    def test_already_built_blocks_rebuilding(self) -> None:
        assert not self._qualify(already_built=True).is_build_candidate

    def test_already_messaged_blocks_recontacting(self) -> None:
        assert not self._qualify(already_messaged_status="SENT").is_build_candidate

    def test_unscannable_site_is_unreachable(self) -> None:
        result = self._qualify(scan_status=ScanStatus.CONNECTION_ERROR)
        assert result.qualification_status == QualificationStatus.UNREACHABLE
        assert not result.is_build_candidate

    def test_no_website_is_unreachable(self) -> None:
        assert self._qualify(has_website=False).qualification_status == QualificationStatus.UNREACHABLE

    def test_qualification_works_with_ai_disabled(self) -> None:
        result = self._qualify(assessment=None)
        assert result.combined_score is not None
        assert result.weights["ai"] == 0.0

    def test_ai_assessment_shifts_the_combined_score(self) -> None:
        assessment = AIWebsiteAssessment(
            business_id=1, provider="claude_cli", visual_modernity=90,
            mobile_visual_quality=90, conversion_quality=90, trust_quality=90,
            redesign_opportunity=10, dated_probability=0.1,
        )
        with_ai = self._qualify(assessment=assessment)
        without = self._qualify()
        assert with_ai.combined_score > without.combined_score
        assert with_ai.weights["ai"] > 0

    def test_dead_domain_ranks_below_a_live_ugly_site(self) -> None:
        live = self._qualify()
        dead = self._qualify(scan_status=ScanStatus.HTTP_ERROR, business_status="CLOSED_PERMANENTLY")
        assert live.build_priority > dead.build_priority

    def test_priority_is_bounded(self) -> None:
        assert 0.0 <= self._qualify().build_priority <= 1.0


class TestProblemRanking:
    def test_mobile_and_conversion_problems_rank_highest(self) -> None:
        ranked = rank_problems(
            [
                {"code": "no_charset", "severity": "low", "category": "technical"},
                {"code": "no_viewport_meta", "severity": "high", "category": "mobile"},
                {"code": "no_click_to_call", "severity": "high", "category": "conversion"},
            ]
        )
        assert ranked[0]["code"] in {"no_viewport_meta", "no_click_to_call"}
        assert ranked[-1]["code"] == "no_charset"

    def test_empty_input(self) -> None:
        assert rank_problems([]) == []


class TestThirdPartyEmails:
    def test_own_domain_is_not_third_party(self) -> None:
        assert not email_is_third_party("info@smile.com", "smile.com")

    def test_consumer_mailbox_is_not_third_party(self) -> None:
        assert not email_is_third_party("smiledental@gmail.com", "smile.com")

    def test_unrelated_domain_is_third_party(self) -> None:
        assert email_is_third_party("attacker@evil.example", "smile.com")

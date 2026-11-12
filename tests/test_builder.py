"""Template rendering, hallucination guardrails, QA and idempotency."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from outreach.ai.schemas import (
    BusinessContentSchema,
    BusinessHours,
    ServiceItem,
    SiteContentSchema,
    SiteHero,
    SiteSectionCopy,
    TeamMember,
)
from outreach.builder.content import (
    EvidenceCorpus,
    _tidy_prose,
    deterministic_business_content,
    deterministic_site_content,
    guard_business_content,
    guard_site_content,
)
from outreach.builder.qa import PLACEHOLDER_PATTERNS, run_qa, static_checks
from outreach.builder.renderer import build_context, content_hash, render_site, template_version


def _content(**overrides: Any) -> BusinessContentSchema:
    defaults: dict[str, Any] = {
        "business_name": "Sunrise Family Dental",
        "phone": "+16025550134",
        "email": "frontdesk@sunrise.example",
        "address": "4820 N 19th Ave, Suite 12",
        "city": "Phoenix",
        "state": "AZ",
        "postal_code": "85015",
        "services": [ServiceItem(name="Routine cleanings"), ServiceItem(name="Crowns and bridges")],
        "hours": [BusinessHours(days="Mon-Thu", hours="8:00-17:00")],
        "team": [TeamMember(name="Dr. Ellen Vargas", credentials="DDS")],
        "credentials": ["DDS"],
        "about": "Sunrise Family Dental has served north Phoenix families for many years.",
    }
    return BusinessContentSchema(**{**defaults, **overrides})


class TestRendering:
    def test_renders_a_complete_site(self, tmp_path: Path) -> None:
        content = _content()
        rendered = render_site(content, deterministic_site_content(content), tmp_path / "site")
        assert rendered.index_path.is_file()
        assert (rendered.output_dir / "styles.css").is_file()
        assert (rendered.output_dir / "script.js").is_file()
        assert (rendered.output_dir / "robots.txt").is_file()

    def test_output_carries_noindex(self, tmp_path: Path) -> None:
        content = _content()
        rendered = render_site(content, deterministic_site_content(content), tmp_path / "s")
        html = rendered.index_path.read_text()
        assert re.search(r'name="robots"[^>]*content="noindex,nofollow"', html)

    def test_disallows_robots_at_the_directory_level_too(self, tmp_path: Path) -> None:
        content = _content()
        rendered = render_site(content, deterministic_site_content(content), tmp_path / "s")
        assert "Disallow: /" in (rendered.output_dir / "robots.txt").read_text()

    def test_phone_becomes_a_tel_link(self, tmp_path: Path) -> None:
        content = _content()
        html = render_site(content, deterministic_site_content(content), tmp_path / "s").index_path.read_text()
        assert 'href="tel:+16025550134"' in html
        assert "(602) 555-0134" in html

    def test_sections_disappear_without_facts(self, tmp_path: Path) -> None:
        sparse = _content(team=[], hours=[], services=[], address="", about="", credentials=[])
        rendered = render_site(sparse, deterministic_site_content(sparse), tmp_path / "s")
        html = rendered.index_path.read_text()
        assert 'id="team"' not in html
        assert 'id="services"' not in html
        assert 'id="visit"' not in html
        assert "team" not in rendered.sections

    def test_never_emits_placeholders(self, tmp_path: Path) -> None:
        for content in (_content(), _content(team=[], services=[], about="", address="")):
            rendered = render_site(content, deterministic_site_content(content), tmp_path / "s")
            html = rendered.index_path.read_text()
            for pattern in PLACEHOLDER_PATTERNS:
                assert not pattern.search(html), f"{pattern.pattern} leaked into the output"

    def test_falls_back_to_a_monogram_without_photos(self, tmp_path: Path) -> None:
        content = _content()
        html = render_site(content, deterministic_site_content(content), tmp_path / "s").index_path.read_text()
        assert "hero--no-photo" in html
        assert "brand__mark" in html
        assert "<img" not in html.split("<footer")[0] or "hero__pattern" in html

    def test_logo_is_used_when_the_prospect_owns_one(self, tmp_path: Path) -> None:
        logo = tmp_path / "logo.png"
        logo.write_bytes(b"\x89PNG")
        content = _content(logo_url=str(logo))
        rendered = render_site(
            content,
            deterministic_site_content(content),
            tmp_path / "s",
            source_assets=[
                {"url": str(logo), "kind": "logo", "local_path": str(logo), "ownership": "prospect_site"}
            ],
        )
        html = rendered.index_path.read_text()
        assert "assets/" in html
        assert (rendered.output_dir / "assets").is_dir()

    def test_assets_of_uncertain_ownership_are_skipped(self, tmp_path: Path) -> None:
        asset = tmp_path / "stock.jpg"
        asset.write_bytes(b"\xff\xd8")
        content = _content(image_urls=[str(asset)])
        rendered = render_site(
            content,
            deterministic_site_content(content),
            tmp_path / "s",
            source_assets=[
                {"url": str(asset), "kind": "photo", "local_path": str(asset), "ownership": "unknown"}
            ],
        )
        assert rendered.assets_copied == []
        assert any("uncertain ownership" in w for w in rendered.warnings)

    def test_html_is_escaped(self, tmp_path: Path) -> None:
        content = _content(business_name='Smile <script>alert("x")</script> Dental')
        html = render_site(content, deterministic_site_content(content), tmp_path / "s").index_path.read_text()
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_unknown_template_raises(self, tmp_path: Path) -> None:
        content = _content()
        with pytest.raises(FileNotFoundError):
            render_site(content, deterministic_site_content(content), tmp_path / "s",
                        template_name="plumber")

    def test_template_version_is_recorded(self) -> None:
        assert template_version("dentist") == "1.0.0"


class TestIdempotency:
    def test_identical_input_produces_an_identical_hash(self, tmp_path: Path) -> None:
        content = _content()
        site = deterministic_site_content(content)
        first = render_site(content, site, tmp_path / "a")
        second = render_site(content, site, tmp_path / "b")
        assert first.content_hash == second.content_hash

    def test_changed_content_changes_the_hash(self, tmp_path: Path) -> None:
        base = _content()
        changed = _content(business_name="Different Dental")
        h1 = render_site(base, deterministic_site_content(base), tmp_path / "a").content_hash
        h2 = render_site(changed, deterministic_site_content(changed), tmp_path / "b").content_hash
        assert h1 != h2

    def test_rebuilding_overwrites_rather_than_duplicating(self, tmp_path: Path) -> None:
        content = _content()
        site = deterministic_site_content(content)
        target = tmp_path / "site"
        render_site(content, site, target)
        render_site(content, site, target)
        assert len(list(target.glob("index*.html"))) == 1

    def test_hash_includes_the_template_version(self) -> None:
        context = build_context(_content(), deterministic_site_content(_content()))
        assert content_hash(context, "dentist") != content_hash(context, "other")


class TestDeterministicContent:
    def test_built_from_crawled_facts_only(self) -> None:
        facts = {
            "business_name": "Sunrise Family Dental",
            "phones": ["+16025550134"],
            "address_lines": ["4820 N 19th Ave, Phoenix, AZ 85015"],
            "city": "Phoenix", "state": "AZ", "postal_code": "85015",
            "services": ["Routine cleanings", "Crowns"],
            "hours": [{"days": "Mon-Thu", "hours": "8-5"}],
            "people": [{"name": "Dr. Ellen Vargas", "credential": "DDS"}],
            "credentials": ["DDS"],
            "about_paragraphs": ["A" * 120],
            "_evidence": {},
        }
        content = deterministic_business_content(
            business_name="Fallback", facts=facts,
            contacts=[{"normalized_email": "info@sunrise.example", "confidence": 0.9}],
        )
        assert content.business_name == "Sunrise Family Dental"
        assert content.email == "info@sunrise.example"
        assert [s.name for s in content.services] == ["Routine cleanings", "Crowns"]

    def test_duplicate_hours_are_collapsed(self) -> None:
        facts = {
            "hours": [
                {"days": "Monday - Thursday", "hours": "8:00 am - 5:00 pm"},
                {"days": "Monday - Thursday", "hours": "8:00 am to 5:00 pm"},
                {"days": "Saturday - Sunday", "hours": "Closed"},
                {"days": "Saturday and Sunday", "hours": "closed"},
            ],
            "_evidence": {},
        }
        content = deterministic_business_content(business_name="X", facts=facts, contacts=[])
        assert len(content.hours) == 2

    def test_old_site_meta_commentary_is_dropped(self) -> None:
        text = (
            "We serve north Phoenix. Site last updated 2009. "
            "Please excuse our appearance while we update our web site. Call us today."
        )
        cleaned = _tidy_prose(text)
        assert "last updated" not in cleaned
        assert "excuse our appearance" not in cleaned
        assert "We serve north Phoenix." in cleaned

    def test_site_content_is_never_empty(self) -> None:
        site = deterministic_site_content(BusinessContentSchema(business_name="X"))
        assert site.hero.headline
        assert site.meta_title


class TestHallucinationGuardrails:
    CORPUS = EvidenceCorpus(
        [
            "Sunrise Family Dental provides cleanings, crowns and whitening in Phoenix Arizona. "
            "Dr. Ellen Vargas DDS leads the practice. Call 602-555-0134."
        ]
    )

    def _guard(self, ai: BusinessContentSchema) -> Any:
        return guard_business_content(
            ai, _content(), self.CORPUS,
            allowed_emails={"frontdesk@sunrise.example"}, allowed_phones={"+16025550134"},
        )

    def test_invented_people_are_dropped(self) -> None:
        ai = _content(team=[TeamMember(name="Dr. Imaginary Person", credentials="DDS")])
        guarded, report = self._guard(ai)
        assert guarded.team == []
        assert any(d["field"] == "team" for d in report.dropped)

    def test_evidenced_people_survive(self) -> None:
        ai = _content(team=[TeamMember(name="Dr. Ellen Vargas", credentials="DDS")])
        guarded, _ = self._guard(ai)
        assert [p.name for p in guarded.team] == ["Dr. Ellen Vargas"]

    def test_invented_services_are_dropped(self) -> None:
        ai = _content(services=[ServiceItem(name="Cleanings"), ServiceItem(name="Rocket surgery")])
        guarded, _report = self._guard(ai)
        assert "Rocket surgery" not in [s.name for s in guarded.services]

    @pytest.mark.parametrize(
        "claim",
        [
            "We have been serving Phoenix for 30 years.",
            "Established in 1998 by a founding family.",
            "Award winning dental care voted best of Phoenix.",
            "Our board-certified dentists are certified specialists.",
            "Rated 4.9 stars from 500 reviews.",
            "Free consultation, starting at $99.",
            "We accept most PPO insurance and offer financing.",
            "24/7 emergency care with a satisfaction guarantee.",
            "Visit any of our 3 convenient locations.",
        ],
    )
    def test_forbidden_claims_are_stripped_from_prose(self, claim: str) -> None:
        ai = _content(about=f"A trusted local practice. {claim}")
        guarded, report = self._guard(ai)
        assert claim not in guarded.about
        assert report.dropped

    def test_supported_prose_survives(self) -> None:
        ai = _content(about="Sunrise Family Dental provides cleanings and crowns in Phoenix.")
        guarded, _ = self._guard(ai)
        assert "cleanings" in guarded.about

    def test_contact_details_can_never_be_invented(self) -> None:
        ai = _content(email="attacker@evil.example", phone="+15550001111")
        guarded, report = self._guard(ai)
        assert guarded.email == "frontdesk@sunrise.example"
        assert guarded.phone == "+16025550134"
        assert len([d for d in report.dropped if d["field"] in {"email", "phone"}]) == 2

    def test_assets_are_never_ai_chosen(self) -> None:
        ai = _content(logo_url="https://other-company.com/logo.png",
                      image_urls=["https://stock.example/photo.jpg"])
        guarded, _ = self._guard(ai)
        assert guarded.logo_url == _content().logo_url
        assert guarded.image_urls == _content().image_urls

    def test_unevidenced_credentials_are_dropped(self) -> None:
        ai = _content(credentials=["DDS", "FAGD", "PhD"])
        guarded, _ = self._guard(ai)
        assert guarded.credentials == ["DDS"]

    def test_site_copy_guard_rejects_invented_selling_points(self) -> None:
        content = _content()
        baseline = deterministic_site_content(content)
        ai_site = SiteContentSchema(
            hero=SiteHero(headline="Award winning dentistry since 1985"),
            intro=SiteSectionCopy(heading="About", body="Rated #1 by 900 patients."),
            why_choose_points=["30 years of experience", "Cleanings and crowns in Phoenix"],
            service_blurbs={"Routine cleanings": "Gentle cleanings.", "Botox": "Not offered."},
        )
        guarded, report = guard_site_content(ai_site, baseline, content, self.CORPUS)
        assert "Award winning" not in guarded.hero.headline
        assert "Rated #1" not in guarded.intro.body
        assert "30 years of experience" not in guarded.why_choose_points
        assert "Botox" not in guarded.service_blurbs
        assert report.dropped

    def test_corpus_phrase_matching(self) -> None:
        assert self.CORPUS.contains_phrase("Dr. Ellen Vargas")
        assert self.CORPUS.contains_phrase("cleanings")
        assert not self.CORPUS.contains_phrase("orthodontic headgear fitting")


class TestQA:
    def _render(self, tmp_path: Path, **overrides: Any) -> Path:
        content = _content(**overrides)
        rendered = render_site(content, deterministic_site_content(content), tmp_path / "site")
        return rendered.output_dir

    def test_a_good_build_passes(self, tmp_path: Path) -> None:
        result = run_qa(self._render(tmp_path))
        assert result.passed, result.failures
        assert result.screenshot_path and result.mobile_screenshot_path

    def test_no_mobile_overflow(self, tmp_path: Path) -> None:
        result = run_qa(self._render(tmp_path))
        assert int(result.checks["mobile"]["overflow_px"]) == 0

    def test_missing_index_fails(self, tmp_path: Path) -> None:
        result = run_qa(tmp_path / "empty")
        assert not result.passed
        assert any("index.html" in f for f in result.failures)

    def test_missing_noindex_fails(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        html = (target / "index.html").read_text().replace('content="noindex,nofollow"', 'content="all"')
        (target / "index.html").write_text(html)
        result = run_qa(target, use_browser=False)
        assert not result.passed
        assert any("noindex" in f for f in result.failures)

    def test_placeholder_content_fails(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        html = (target / "index.html").read_text().replace("</main>", "<p>Dr. Name Here</p></main>")
        (target / "index.html").write_text(html)
        result = run_qa(target, use_browser=False)
        assert not result.passed
        assert any("placeholder" in f.lower() for f in result.failures)

    def test_broken_local_asset_fails(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        (target / "styles.css").unlink()
        result = run_qa(target, use_browser=False)
        assert not result.passed
        assert any("broken local asset" in f for f in result.failures)

    def test_invalid_tel_link_fails(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        html = (target / "index.html").read_text().replace('href="tel:+16025550134"', 'href="tel:123"')
        (target / "index.html").write_text(html)
        result = run_qa(target, use_browser=False)
        assert not result.passed
        assert any("tel:" in f for f in result.failures)

    def test_static_checks_alone_are_useful(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        failures, _warnings, checks = static_checks((target / "index.html").read_text(), target)
        assert failures == []
        assert checks["has_title"] and checks["has_noindex"] and checks["has_viewport"]

"""Fetching, redirects, robots, link prioritisation, contact extraction."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from outreach.crawler.contacts import (
    classify_email,
    extract_contacts_from_html,
    is_junk_email,
    merge_contact_results,
    rank_contacts,
)
from outreach.crawler.content import ExtractedFacts, extract_facts_from_page, visible_text
from outreach.crawler.fetch import Fetcher, RobotsCache
from outreach.crawler.links import (
    extract_links,
    prioritized_same_domain_links,
    score_link,
)
from outreach.models.enums import ContactType, ScanStatus


def _fetcher(settings: Any, handler: Any) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(settings, client=client, sleep=lambda _s: None)


class TestFetch:
    def test_successful_fetch(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, html="<html><title>Hi</title></html>")

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("example.com")
        assert result.ok
        assert result.scan_status == ScanStatus.OK
        assert result.final_url == "https://example.com/"

    def test_follows_redirects_and_records_the_final_url(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(302, headers={"Location": "https://example.com/home"})
            return httpx.Response(200, html="<html><body>home</body></html>")

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("https://example.com/")
        assert result.ok
        assert result.final_url == "https://example.com/home"
        assert result.redirect_chain

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (403, ScanStatus.BLOCKED),
            (429, ScanStatus.BLOCKED),
            (401, ScanStatus.BLOCKED),
            (404, ScanStatus.HTTP_ERROR),
            (500, ScanStatus.HTTP_ERROR),
        ],
    )
    def test_error_statuses_are_classified_not_raised(
        self, settings: Any, status: int, expected: ScanStatus
    ) -> None:
        with _fetcher(settings, lambda r: httpx.Response(status, text="no")) as fetcher:
            result = fetcher.fetch("https://example.com/")
        assert result.scan_status == expected
        assert not result.ok

    def test_blocked_sites_are_recorded_never_circumvented(self, settings: Any) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            attempts["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "3600"})

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("https://example.com/")
        assert result.scan_status == ScanStatus.BLOCKED
        assert attempts["n"] == 1, "a 429 must not be retried"

    def test_timeouts_are_retried_then_reported(self, settings: Any) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            attempts["n"] += 1
            raise httpx.ConnectTimeout("slow")

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("https://example.com/", retries=2)
        assert result.scan_status == ScanStatus.TIMEOUT
        assert attempts["n"] == 3
        assert not result.ok

    def test_connection_errors_do_not_raise(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns")

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("https://nope.invalid/", retries=0)
        assert result.scan_status == ScanStatus.CONNECTION_ERROR

    def test_unusable_url_is_rejected_without_a_request(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("should not be requested")

        with _fetcher(settings, handler) as fetcher:
            result = fetcher.fetch("mailto:a@b.com")
        assert result.scan_status == ScanStatus.FAILED

    def test_oversized_html_is_truncated(self, env_settings: Any) -> None:
        settings = env_settings(CRAWL_MAX_HTML_BYTES="10000")
        body = "<html>" + ("x" * 50000) + "</html>"
        with _fetcher(settings, lambda r: httpx.Response(200, html=body)) as fetcher:
            result = fetcher.fetch("https://example.com/")
        assert result.truncated
        assert len(result.text) <= 10001


class TestRobots:
    def test_disallowed_paths_are_not_fetched(self, env_settings: Any) -> None:
        settings = env_settings(CRAWL_RESPECT_ROBOTS="true")
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
            return httpx.Response(200, html="<html></html>")

        with _fetcher(settings, handler) as fetcher:
            blocked = fetcher.fetch("https://example.com/private/x")
            allowed = fetcher.fetch("https://example.com/public")
        assert blocked.scan_status == ScanStatus.ROBOTS_DISALLOWED
        assert "/private/x" not in fetched
        assert allowed.ok

    def test_robots_can_be_disabled(self, env_settings: Any) -> None:
        settings = env_settings(CRAWL_RESPECT_ROBOTS="false")
        with _fetcher(settings, lambda r: httpx.Response(200, html="<html></html>")) as fetcher:
            assert fetcher.fetch("https://example.com/private/x").ok

    def test_missing_robots_defaults_to_allowed(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, html="<html></html>")

        with _fetcher(settings, handler) as fetcher:
            cache = RobotsCache(fetcher._client, "Bot", enabled=True)
            assert cache.allowed("https://example.com/anything")


class TestLinks:
    HTML = """
    <a href="/contact">Contact Us</a>
    <a href="/about-us">About</a>
    <a href="/services">Our Services</a>
    <a href="/blog/2019/05/post">Old post</a>
    <a href="https://other.com/x">Other site</a>
    <a href="/brochure.pdf">PDF</a>
    <a href="/deep/nested/thing">Deep</a>
    """

    def test_only_same_domain_links_survive(self) -> None:
        links = prioritized_same_domain_links(self.HTML, "https://example.com/", limit=10)
        urls = [c.url for c in links]
        assert all("other.com" not in u for u in urls)
        assert "https://example.com/contact" in urls

    def test_high_value_pages_come_first(self) -> None:
        links = prioritized_same_domain_links(self.HTML, "https://example.com/", limit=3)
        assert links[0].url.endswith("/contact")
        assert {c.url.rsplit("/", 1)[-1] for c in links} >= {"contact", "about-us"}

    def test_assets_and_archives_are_skipped(self) -> None:
        urls = [c.url for c in prioritized_same_domain_links(self.HTML, "https://example.com/", limit=20)]
        assert not any(u.endswith(".pdf") for u in urls)
        assert not any("/blog/2019/" in u for u in urls)

    def test_already_seen_urls_are_not_repeated(self) -> None:
        seen = {"https://example.com/contact"}
        urls = [
            c.url
            for c in prioritized_same_domain_links(
                self.HTML, "https://example.com/", limit=10, already_seen=seen
            )
        ]
        assert "https://example.com/contact" not in urls

    def test_score_link_prefers_the_homepage_and_shallow_paths(self) -> None:
        assert score_link("https://x.com/") > score_link("https://x.com/a/b/c/d")

    def test_extract_links_resolves_relative(self) -> None:
        links = dict(extract_links('<a href="about.html">A</a>', "https://x.com/practice/"))
        assert "https://x.com/practice/about.html" in links


class TestContactExtraction:
    def test_finds_mailto_and_text_addresses(self, sample_html: str) -> None:
        result = extract_contacts_from_html(
            sample_html, "https://sunrisefamilydental.example/", "sunrisefamilydental.example"
        )
        emails = {c.normalized_email for c in result.contacts}
        assert "frontdesk@sunrisefamilydental.example" in emails

    def test_records_the_source_url(self, sample_html: str) -> None:
        result = extract_contacts_from_html(sample_html, "https://x.example/contact", "x.example")
        assert all(c.source_url == "https://x.example/contact" for c in result.contacts)

    def test_json_ld_email(self, modern_html: str) -> None:
        result = extract_contacts_from_html(
            modern_html, "https://cactusmoderndental.example/", "cactusmoderndental.example"
        )
        found = {c.normalized_email: c for c in result.contacts}
        assert "hello@cactusmoderndental.example" in found

    def test_nothing_is_ever_guessed(self) -> None:
        html = "<html><body><p>Call us at (602) 555-0134. No email here.</p></body></html>"
        result = extract_contacts_from_html(html, "https://smile.com/", "smile.com")
        assert result.contacts == []

    @pytest.mark.parametrize(
        "junk",
        [
            "example@example.com", "test@test.com", "noreply@smile.com",
            "no-reply@smile.com", "you@yourdomain.com", "logo@2x.png",
            "photo@image.jpg", "a1b2c3d4e5f6a7b8@smile.com", "name@domain.com",
        ],
    )
    def test_junk_addresses_are_rejected(self, junk: str) -> None:
        html = f'<a href="mailto:{junk}">mail</a>'
        result = extract_contacts_from_html(html, "https://smile.com/", "smile.com")
        assert result.contacts == []
        assert result.rejected

    def test_is_junk_email_reports_a_reason(self) -> None:
        junk, why = is_junk_email("noreply@smile.com")
        assert junk and "noreply" in why
        junk, why = is_junk_email("logo@2x.png")
        assert junk and "asset" in why

    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("owner@x.com", ContactType.OWNER),
            ("dr.smith@x.com", ContactType.DOCTOR),
            ("frontdesk@x.com", ContactType.OFFICE),
            ("hello@x.com", ContactType.HELLO),
            ("contact@x.com", ContactType.CONTACT),
            ("info@x.com", ContactType.INFO),
            ("billing@x.com", ContactType.SUPPORT),
            ("zzz@x.com", ContactType.UNKNOWN),
        ],
    )
    def test_classification(self, email: str, expected: ContactType) -> None:
        assert classify_email(email) == expected

    def test_ranking_prefers_on_domain_then_role(self) -> None:
        html = (
            '<a href="mailto:billing@smile.com">b</a>'
            '<a href="mailto:owner@smile.com">o</a>'
            '<a href="mailto:info@thirdparty.com">t</a>'
        )
        result = extract_contacts_from_html(html, "https://smile.com/", "smile.com")
        ranked = rank_contacts(result.contacts, "smile.com")
        assert ranked[0].normalized_email == "owner@smile.com"
        assert ranked[-1].normalized_email == "info@thirdparty.com"

    def test_offsite_address_in_injected_block_ranks_below_the_real_one(
        self, injection_html: str
    ) -> None:
        result = extract_contacts_from_html(
            injection_html, "https://whitetoothdental.example/", "whitetoothdental.example"
        )
        ranked = rank_contacts(result.contacts, "whitetoothdental.example")
        assert ranked[0].normalized_email == "reception@whitetoothdental.example"

    def test_merge_keeps_the_highest_confidence_source(self) -> None:
        text_only = extract_contacts_from_html(
            "<p>info@smile.com</p>", "https://smile.com/a", "smile.com"
        )
        mailto = extract_contacts_from_html(
            '<a href="mailto:info@smile.com">x</a>', "https://smile.com/b", "smile.com"
        )
        merged = merge_contact_results([text_only, mailto])
        assert len(merged.contacts) == 1
        assert merged.contacts[0].source_method == "mailto"


class TestFactExtraction:
    def test_extracts_business_facts_with_evidence(self, sample_html: str) -> None:
        facts = extract_facts_from_page(sample_html, "https://x.example/")
        data = facts.to_dict()
        assert data["business_name"] == "Sunrise Family Dental"
        assert "+16025550134" in data["phones"]
        assert data["city"] == "Phoenix" and data["state"] == "AZ"
        assert facts.evidence, "every non-trivial claim needs evidence"

    def test_json_ld_is_preferred(self, modern_html: str) -> None:
        facts = extract_facts_from_page(modern_html, "https://x.example/")
        assert facts.business_name == "Cactus Modern Dental"
        assert facts.postal_code == "85014"

    def test_people_require_an_explicit_title(self) -> None:
        facts = extract_facts_from_page(
            "<p>Phoenix Arizona Dental Group serves North Scottsdale families.</p>",
            "https://x.example/",
        )
        assert facts.people == []

    def test_partial_names_merge_into_full_ones(self) -> None:
        facts = extract_facts_from_page(
            "<p>Dr. Ellen Vargas, DDS founded the practice. Dr. Vargas sees patients daily.</p>",
            "https://x.example/",
        )
        assert facts.people == [{"name": "Dr. Ellen Vargas", "credential": "DDS"}]

    def test_visible_text_drops_scripts_and_styles(self) -> None:
        text = visible_text("<style>.a{color:red}</style><script>alert(1)</script><p>Hello</p>")
        assert "Hello" in text
        assert "alert" not in text and "color:red" not in text

    def test_accumulates_across_pages(self, sample_html: str) -> None:
        facts = ExtractedFacts()
        extract_facts_from_page(sample_html, "https://x.example/", facts)
        extract_facts_from_page(sample_html, "https://x.example/contact", facts)
        assert len(facts.titles) == 1, "identical titles must not duplicate"

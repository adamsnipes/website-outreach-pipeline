"""URL, domain, email and phone normalisation."""

from __future__ import annotations

import pytest

from outreach.normalize import (
    domains_match,
    extract_registrable_domain,
    is_private_or_local_url,
    join_url,
    normalize_domain,
    normalize_email,
    normalize_phone,
    normalize_site_identity,
    normalize_url,
    same_site,
    strip_tracking_params,
    url_slug,
)


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("example.com", "https://example.com/"),
            ("HTTP://Example.COM/Path", "http://example.com/Path"),
            ("https://example.com:443/a", "https://example.com/a"),
            ("http://example.com:80/a", "http://example.com/a"),
            ("//example.com/a", "https://example.com/a"),
            ("https://example.com//a//b", "https://example.com/a/b"),
            ("https://EXAMPLE.com/a?utm_source=x&id=3", "https://example.com/a?id=3"),
        ],
    )
    def test_canonicalisation(self, raw: str, expected: str) -> None:
        assert normalize_url(raw) == expected

    def test_trailing_slash_is_preserved(self) -> None:
        # Load-bearing: relative links resolve against a different base without it.
        assert normalize_url("https://example.com/practice/") == "https://example.com/practice/"
        assert normalize_url("https://example.com/practice") == "https://example.com/practice"

    @pytest.mark.parametrize(
        "raw",
        ["", None, "mailto:a@b.com", "ftp://example.com", "javascript:alert(1)", "info@x.com"],
    )
    def test_rejects_non_http(self, raw: str | None) -> None:
        assert normalize_url(raw) is None

    def test_strip_tracking_params(self) -> None:
        assert strip_tracking_params("utm_source=a&id=3&gclid=z&q=1") == "id=3&q=1"
        assert strip_tracking_params("") == ""


class TestJoinUrl:
    def test_resolves_relative_against_directory(self) -> None:
        assert (
            join_url("https://example.com/practice/", "about.html")
            == "https://example.com/practice/about.html"
        )

    @pytest.mark.parametrize("href", ["mailto:a@b.com", "tel:+1555", "#top", "javascript:x", ""])
    def test_ignores_non_navigational(self, href: str) -> None:
        assert join_url("https://example.com/", href) is None


class TestDomains:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://www.Smile-Dental.com:443/x", "smile-dental.com"),
            ("WWW.EXAMPLE.COM", "example.com"),
            ("info@Example.com", "example.com"),
            ("https://clinic.example.com/", "clinic.example.com"),
            (None, None),
        ],
    )
    def test_normalize_domain(self, raw: str | None, expected: str | None) -> None:
        assert normalize_domain(raw) == expected

    def test_subdomains_stay_distinct_businesses(self) -> None:
        assert normalize_domain("a.example.com") != normalize_domain("b.example.com")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://blog.smile.co.uk/a", "smile.co.uk"),
            ("https://a.b.example.com", "example.com"),
            ("http://127.0.0.1:8765/x", "127.0.0.1"),
        ],
    )
    def test_registrable_domain(self, raw: str, expected: str) -> None:
        assert extract_registrable_domain(raw) == expected

    def test_same_site_and_match(self) -> None:
        assert same_site("https://a.example.com/1", "http://example.com/2")
        assert not same_site("https://example.com", "https://other.com")
        assert domains_match("https://www.x.com/a", "x.com")
        assert not domains_match(None, "x.com")


class TestSiteIdentity:
    def test_public_hosts_key_on_domain(self) -> None:
        assert normalize_site_identity("https://www.smile.com/about") == "smile.com"
        assert normalize_site_identity("https://smile.com/") == "smile.com"

    def test_loopback_hosts_include_first_path_segment(self) -> None:
        # Otherwise every fixture business would dedupe into one row.
        a = normalize_site_identity("http://127.0.0.1:8765/practice-a/")
        b = normalize_site_identity("http://127.0.0.1:8765/practice-b/")
        assert a != b
        assert a == normalize_site_identity("http://127.0.0.1:8765/practice-a/index.html")

    def test_none_for_missing(self) -> None:
        assert normalize_site_identity(None) is None


class TestPrivateUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/x", "http://127.0.0.1/x", "http://192.168.1.5/x",
            "http://10.0.0.1/", "http://172.16.0.1/", "http://box.local/", None, "",
        ],
    )
    def test_private(self, url: str | None) -> None:
        assert is_private_or_local_url(url) is True

    @pytest.mark.parametrize("url", ["https://example.com/", "http://8.8.8.8/"])
    def test_public(self, url: str) -> None:
        assert is_private_or_local_url(url) is False


class TestEmails:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" MAILTO:Info@Example.COM. ", "info@example.com"),
            ("<a@b.co>", "a@b.co"),
            ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
            ("info@example.com?subject=hi", "info@example.com"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "notanemail", "a@@b.com", "a@b", "@b.com", "a@.com", "a..b@c.com", "a@b..com"],
    )
    def test_invalid(self, raw: str | None) -> None:
        assert normalize_email(raw) is None

    def test_addresses_are_fully_lowercased(self) -> None:
        # Case-insensitive on purpose: a case-sensitive suppression list could
        # be defeated by changing the capitalisation of an opted-out address.
        assert normalize_email("John.Doe@Example.com") == "john.doe@example.com"
        assert normalize_email("INFO@X.COM") == normalize_email("info@x.com")


class TestPhones:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("(602) 555-0134", "+16025550134"),
            ("602.555.0134", "+16025550134"),
            ("1-602-555-0134", "+16025550134"),
            ("+44 20 7946 0958", "+442079460958"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "abc", "12345", "555-0134"])
    def test_invalid(self, raw: str | None) -> None:
        assert normalize_phone(raw) is None


def test_url_slug_is_filesystem_safe() -> None:
    assert url_slug("https://x.com/a/b/") == "a-b"
    assert url_slug("https://x.com/") == "index"
    assert "/" not in url_slug("https://x.com/deep/path/page.html")

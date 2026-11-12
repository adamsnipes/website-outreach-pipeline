"""Preview locations, emailable-URL rules and the publishing abstraction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from outreach.previews.manager import PreviewManager, preview_url_for, validate_preview_url
from outreach.previews.publisher import (
    LocalCopyPublisher,
    NullPublisher,
    RsyncPublisher,
    get_publisher,
)
from outreach.previews.server import PreviewServer


class TestPreviewUrls:
    @pytest.mark.parametrize(
        "url",
        [
            None, "", "http://localhost:8800/1/", "http://127.0.0.1:8800/1/",
            "http://192.168.1.10/1/", "http://10.0.0.5/1/", "http://box.local/1/",
            "not a url", "ftp://example.com/1/",
        ],
    )
    def test_unusable_urls_are_refused(self, url: str | None) -> None:
        check = validate_preview_url(url)
        assert not check.usable
        assert check.reason

    def test_plain_http_is_refused(self) -> None:
        check = validate_preview_url("http://previews.example.com/1/")
        assert not check.usable
        assert "https" in check.reason

    @pytest.mark.parametrize("host", ["p.example", "p.test", "p.invalid", "p.localhost"])
    def test_reserved_tld_hosts_are_refused(self, host: str) -> None:
        # RFC 2606 / 6761 names never resolve, so a recipient could never open them.
        assert not validate_preview_url(f"https://{host}/1/").usable

    def test_public_https_is_accepted(self) -> None:
        check = validate_preview_url("https://previews.example.com/1/")
        assert check.usable
        assert check.url == "https://previews.example.com/1/"

    def test_url_is_built_from_the_base(self, env_settings: Any) -> None:
        settings = env_settings(PREVIEW_BASE_URL="https://p.example.com")
        assert preview_url_for(settings, 17) == "https://p.example.com/17/"

    def test_no_base_means_no_url(self, env_settings: Any) -> None:
        assert preview_url_for(env_settings(PREVIEW_BASE_URL=""), 17) is None


class TestPreviewManager:
    def _write(self, manager: PreviewManager, business_id: int, html: str) -> Path:
        target = manager.dir_for(business_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(html, encoding="utf-8")
        return target

    def test_noindex_verification(self, settings: Any) -> None:
        manager = PreviewManager(settings)
        self._write(manager, 1, '<html><head><meta name="robots" content="noindex,nofollow"></head></html>')
        self._write(manager, 2, "<html><head></head></html>")
        assert manager.verify_noindex(1)
        assert not manager.verify_noindex(2)
        assert not manager.verify_noindex(999)

    def test_paths_are_deterministic(self, settings: Any) -> None:
        manager = PreviewManager(settings)
        assert manager.dir_for(42) == manager.dir_for(42)
        assert manager.dir_for(42).name == "42"

    def test_listing(self, settings: Any) -> None:
        manager = PreviewManager(settings)
        self._write(manager, 3, "<html></html>")
        self._write(manager, 1, "<html></html>")
        assert [bid for bid, _ in manager.list_previews()] == [1, 3]


class TestPublishers:
    def test_null_publisher_publishes_nothing(self, settings: Any) -> None:
        result = NullPublisher(settings).publish(1, Path("/tmp"))
        assert not result.published

    def test_local_publisher_requires_configuration(self, settings: Any) -> None:
        ok, reason = LocalCopyPublisher(settings).available()
        assert not ok and "PREVIEW_PUBLISH_DIR" in reason

    def test_local_publisher_copies(self, env_settings: Any, tmp_path: Path) -> None:
        source = tmp_path / "site"
        source.mkdir()
        (source / "index.html").write_text("<html>hi</html>")
        (source / "styles.css").write_text("body{}")
        destination = tmp_path / "public"

        settings = env_settings(
            PREVIEW_PUBLISH_DIR=str(destination), PREVIEW_BASE_URL="https://p.example/"
        )
        result = LocalCopyPublisher(settings).publish(7, source)

        assert result.published
        assert (destination / "7" / "index.html").read_text() == "<html>hi</html>"
        assert (destination / "7" / "styles.css").is_file()
        assert result.url == "https://p.example/7/"

    def test_local_publisher_dry_run_copies_nothing(
        self, env_settings: Any, tmp_path: Path
    ) -> None:
        source = tmp_path / "site"
        source.mkdir()
        (source / "index.html").write_text("<html>hi</html>")
        destination = tmp_path / "public"
        settings = env_settings(PREVIEW_PUBLISH_DIR=str(destination))

        result = LocalCopyPublisher(settings).publish(7, source, dry_run=True)
        assert not result.published
        assert "DRY RUN" in result.reason
        assert not destination.exists()

    def test_local_publisher_republish_replaces(self, env_settings: Any, tmp_path: Path) -> None:
        source = tmp_path / "site"
        source.mkdir()
        (source / "index.html").write_text("v1")
        destination = tmp_path / "public"
        settings = env_settings(PREVIEW_PUBLISH_DIR=str(destination))

        LocalCopyPublisher(settings).publish(7, source)
        (source / "index.html").write_text("v2")
        LocalCopyPublisher(settings).publish(7, source)
        assert (destination / "7" / "index.html").read_text() == "v2"

    def test_publishing_an_empty_directory_is_refused(
        self, env_settings: Any, tmp_path: Path
    ) -> None:
        settings = env_settings(PREVIEW_PUBLISH_DIR=str(tmp_path / "public"))
        result = LocalCopyPublisher(settings).publish(7, tmp_path / "nothing")
        assert not result.published
        assert "nothing to publish" in result.reason

    def test_rsync_publisher_builds_a_safe_command(
        self, env_settings: Any, tmp_path: Path
    ) -> None:
        source = tmp_path / "site"
        source.mkdir()
        (source / "index.html").write_text("<html></html>")
        settings = env_settings(PREVIEW_RSYNC_TARGET="user@host:/var/www/previews")
        calls: list[dict[str, Any]] = []

        class Result:
            returncode = 0
            stderr = ""

        def runner(argv: list[str], **kwargs: Any) -> Any:
            calls.append({"argv": argv, **kwargs})
            return Result()

        publisher = RsyncPublisher(settings, runner=runner)
        if publisher.available()[0]:
            result = publisher.publish(7, source)
            assert result.published
            assert calls[0]["shell"] is False
            assert calls[0]["argv"][-1] == "user@host:/var/www/previews/7/"

    def test_rsync_dry_run_passes_the_flag(self, env_settings: Any, tmp_path: Path) -> None:
        source = tmp_path / "site"
        source.mkdir()
        (source / "index.html").write_text("<html></html>")
        settings = env_settings(PREVIEW_RSYNC_TARGET="user@host:/var/www")
        captured: list[list[str]] = []

        class Result:
            returncode = 0
            stderr = ""

        def runner(argv: list[str], **kwargs: Any) -> Any:
            captured.append(argv)
            return Result()

        publisher = RsyncPublisher(settings, runner=runner)
        if publisher.available()[0]:
            result = publisher.publish(7, source, dry_run=True)
            assert "--dry-run" in captured[0]
            assert not result.published

    def test_registry(self, settings: Any) -> None:
        assert get_publisher(settings, "local").name == "local"
        assert get_publisher(settings, "rsync").name == "rsync"
        assert get_publisher(settings, "null").name == "null"
        with pytest.raises(KeyError):
            get_publisher(settings, "vercel")


class TestPreviewServer:
    def test_serves_an_index_and_previews(self, settings: Any, tmp_path: Path) -> None:
        import httpx

        root = tmp_path / "previews"
        (root / "5").mkdir(parents=True)
        (root / "5" / "index.html").write_text("<html><body>Preview 5</body></html>")

        with PreviewServer(root, port=0) as server:
            listing = httpx.get(f"{server.base_url}/", timeout=5)
            preview = httpx.get(f"{server.base_url}/5/", timeout=5)

        assert listing.status_code == 200
        assert "Business #5" in listing.text
        assert listing.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert "Preview 5" in preview.text

    def test_binds_to_loopback_only(self, tmp_path: Path) -> None:
        server = PreviewServer(tmp_path, port=0)
        assert server.host == "127.0.0.1"
        # And its URLs can never be emailed.
        assert not validate_preview_url(f"{server.base_url}/1/").usable

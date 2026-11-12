"""Chromium (Playwright) session management.

Kept separate from screenshot logic so that everything else can run when no
browser is installed: :func:`browser_available` is checked first and the
pipeline degrades to HTML-only analysis rather than failing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from outreach.logging import get_logger

log = get_logger(__name__)

DESKTOP_VIEWPORT = {"width": 1440, "height": 1000}
MOBILE_VIEWPORT = {"width": 390, "height": 844}
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


@dataclass(frozen=True)
class BrowserAvailability:
    available: bool
    reason: str
    version: str | None = None


def browser_available() -> BrowserAvailability:
    """Whether Playwright *and* a Chromium build are actually usable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return BrowserAvailability(False, f"playwright not installed: {exc}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            version = browser.version
            browser.close()
        return BrowserAvailability(True, "chromium ready", version)
    except Exception as exc:  # pragma: no cover - environment dependent
        return BrowserAvailability(False, f"chromium unavailable: {exc}")


class BrowserSession:
    """Owns one Chromium process for a batch of renders."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30_000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"],
        )
        log.debug("chromium launched", extra={"version": self._browser.version})

    def stop(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    @property
    def browser(self) -> Any:
        if self._browser is None:
            raise RuntimeError("BrowserSession is not started")
        return self._browser

    @contextmanager
    def context(self, mobile: bool = False) -> Iterator[Any]:
        """A fresh, isolated browsing context (no shared cookies/storage)."""
        kwargs: dict[str, Any] = {
            "viewport": dict(MOBILE_VIEWPORT if mobile else DESKTOP_VIEWPORT),
            "ignore_https_errors": True,
            "java_script_enabled": True,
        }
        if mobile:
            kwargs.update(
                {
                    "user_agent": MOBILE_USER_AGENT,
                    "device_scale_factor": 3,
                    "is_mobile": True,
                    "has_touch": True,
                }
            )
        ctx = self.browser.new_context(**kwargs)
        ctx.set_default_timeout(self.timeout_ms)
        try:
            yield ctx
        finally:
            ctx.close()

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

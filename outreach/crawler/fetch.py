"""Polite HTTP fetching.

Rules that are not negotiable:

* one broken website never terminates a campaign -- every failure is captured
  as a :class:`FetchResult`, never raised
* ``robots.txt`` is honoured when ``CRAWL_RESPECT_ROBOTS`` is on
* blocks, CAPTCHAs, WAFs and rate limiting are recorded and respected, never
  circumvented
* configurable delay between requests to the same host
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from outreach.config import Settings
from outreach.logging import get_logger
from outreach.models.enums import ScanStatus
from outreach.normalize import normalize_url

log = get_logger(__name__)

RETRYABLE_STATUS = {502, 503, 504}
BLOCKED_STATUS = {401, 403, 405, 406, 429, 451}


@dataclass
class FetchResult:
    """Outcome of a single HTTP GET."""

    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    text: str = ""
    content_type: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    scan_status: ScanStatus = ScanStatus.PENDING
    redirect_chain: list[str] = field(default_factory=list)
    truncated: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.scan_status == ScanStatus.OK and bool(self.text)

    @property
    def is_https(self) -> bool:
        return (self.final_url or self.requested_url or "").lower().startswith("https://")


class RobotsCache:
    """Per-origin robots.txt cache.  Failures default to *allowed*."""

    def __init__(self, client: httpx.Client, user_agent: str, enabled: bool = True) -> None:
        self._client = client
        self._user_agent = user_agent
        self._enabled = enabled
        self._cache: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._cache:
            return self._cache[origin]
        parser: RobotFileParser | None = None
        try:
            response = self._client.get(f"{origin}/robots.txt", timeout=10.0)
            if response.status_code == 200 and len(response.text) < 500_000:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
        except httpx.HTTPError:
            parser = None
        self._cache[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self._enabled:
            return True
        try:
            parser = self._parser_for(url)
        except Exception:  # pragma: no cover - defensive
            return True
        if parser is None:
            return True
        return bool(parser.can_fetch(self._user_agent, url))


class Fetcher:
    """A small, polite HTTP client wrapper used for every prospect request."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.crawl_timeout_seconds),
            follow_redirects=True,
            max_redirects=6,
            headers={
                "User-Agent": settings.crawl_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self.robots = RobotsCache(
            self._client, settings.crawl_user_agent, settings.crawl_respect_robots
        )
        self._last_request_at: dict[str, float] = {}

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- politeness ----------------------------------------------------
    def _throttle(self, url: str) -> None:
        delay = self.settings.crawl_delay_seconds
        if delay <= 0:
            return
        host = urlparse(url).netloc
        last = self._last_request_at.get(host)
        now = time.monotonic()
        if last is not None:
            wait = delay - (now - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # -- fetching ------------------------------------------------------
    def fetch(self, url: str, *, retries: int = 2) -> FetchResult:
        """GET a URL, following legitimate redirects.  Never raises."""
        normalized = normalize_url(url)
        if not normalized:
            return FetchResult(
                requested_url=str(url),
                error="not a usable http(s) URL",
                scan_status=ScanStatus.FAILED,
            )

        if not self.robots.allowed(normalized):
            log.info("robots.txt disallows fetch", extra={"url": normalized})
            return FetchResult(
                requested_url=normalized,
                error="disallowed by robots.txt",
                scan_status=ScanStatus.ROBOTS_DISALLOWED,
            )

        attempt = 0
        last_error: str | None = None
        while attempt <= retries:
            attempt += 1
            self._throttle(normalized)
            started = time.monotonic()
            try:
                response = self._client.get(normalized)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                status = ScanStatus.TIMEOUT
            except httpx.HTTPError as exc:
                last_error = f"connection error: {exc}"
                status = ScanStatus.CONNECTION_ERROR
            else:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return self._build_result(normalized, response, elapsed_ms)

            if attempt > retries:
                return FetchResult(
                    requested_url=normalized, error=last_error, scan_status=status
                )
            # Linear backoff; we are not trying to beat anyone's rate limiter.
            self._sleep(min(2.0 * attempt, 5.0))

        return FetchResult(  # pragma: no cover - loop always returns
            requested_url=normalized, error=last_error, scan_status=ScanStatus.FAILED
        )

    def _build_result(
        self, requested: str, response: httpx.Response, elapsed_ms: int
    ) -> FetchResult:
        chain = [str(r.url) for r in response.history]
        content_type = response.headers.get("content-type", "")
        max_bytes = self.settings.crawl_max_html_bytes
        raw = response.content or b""
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        try:
            text = raw.decode(response.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):  # pragma: no cover - exotic encodings
            text = raw.decode("utf-8", errors="replace")

        status = response.status_code
        if status in BLOCKED_STATUS:
            scan_status = ScanStatus.BLOCKED
            error: str | None = f"blocked with HTTP {status}"
            log.info("website blocked our crawler", extra={"url": requested, "status": status})
        elif 200 <= status < 300 and "html" in content_type.lower():
            scan_status = ScanStatus.OK
            error = None
        elif 200 <= status < 300:
            scan_status = ScanStatus.OK
            error = f"unexpected content-type {content_type!r}"
        else:
            scan_status = ScanStatus.HTTP_ERROR
            error = f"HTTP {status}"

        return FetchResult(
            requested_url=requested,
            final_url=normalize_url(str(response.url)) or str(response.url),
            status_code=status,
            text=text,
            content_type=content_type or None,
            elapsed_ms=elapsed_ms,
            error=error,
            scan_status=scan_status,
            redirect_chain=chain,
            truncated=truncated,
            headers={k.lower(): v for k, v in response.headers.items()},
        )

    def head_ok(self, url: str) -> bool:
        """Cheap liveness probe used when checking for broken assets."""
        normalized = normalize_url(url)
        if not normalized:
            return False
        try:
            response = self._client.head(normalized, timeout=10.0)
            if response.status_code == 405:
                response = self._client.get(normalized, timeout=10.0)
            return 200 <= response.status_code < 400
        except httpx.HTTPError:
            return False

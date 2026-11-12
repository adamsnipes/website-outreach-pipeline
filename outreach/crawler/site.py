"""Whole-site crawl orchestration.

Fetches the homepage, follows a small budget of prioritised same-domain links,
renders desktop + mobile, and collects contacts, facts and prospect-owned
assets.  Writes every artifact to a deterministic path under the artifact root
so a rerun overwrites rather than duplicating.

Persistence lives in :mod:`outreach.pipeline.scan`; this module only produces
evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outreach.config import Settings
from outreach.crawler.assets import AssetBundle, collect_assets
from outreach.crawler.browser import BrowserSession, browser_available
from outreach.crawler.contacts import (
    ContactExtractionResult,
    extract_contacts_from_html,
    merge_contact_results,
)
from outreach.crawler.content import ExtractedFacts, extract_facts_from_page
from outreach.crawler.fetch import Fetcher, FetchResult
from outreach.crawler.links import prioritized_same_domain_links
from outreach.crawler.screenshots import RenderResult, render_site
from outreach.logging import get_logger
from outreach.models.enums import ScanStatus
from outreach.normalize import normalize_domain, url_slug

log = get_logger(__name__)


def business_artifact_dir(settings: Settings, business_id: int) -> Path:
    """``artifacts/businesses/<id>/`` -- deterministic, stable across reruns."""
    return settings.artifact_dir / "businesses" / str(business_id)


@dataclass
class CrawledPage:
    url: str
    final_url: str | None
    status_code: int | None
    html_path: str | None
    title: str | None = None
    bytes: int = 0
    scan_status: str = ScanStatus.OK
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "html_path": self.html_path,
            "title": self.title,
            "bytes": self.bytes,
            "scan_status": str(self.scan_status),
            "error": self.error,
        }


@dataclass
class CrawlOutcome:
    """Everything one website crawl produced."""

    requested_url: str
    final_url: str | None = None
    http_status: int | None = None
    scan_status: ScanStatus = ScanStatus.PENDING
    error: str | None = None
    homepage_html: str = ""
    homepage_html_path: str | None = None
    pages: list[CrawledPage] = field(default_factory=list)
    page_html: list[tuple[str, str]] = field(default_factory=list, repr=False)
    render: RenderResult | None = None
    contacts: ContactExtractionResult = field(default_factory=ContactExtractionResult)
    facts: ExtractedFacts = field(default_factory=ExtractedFacts)
    assets: AssetBundle = field(default_factory=AssetBundle)
    artifact_dir: str | None = None
    title: str | None = None
    meta_description: str | None = None
    browser_available: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.scan_status == ScanStatus.OK

    @property
    def desktop_screenshot(self) -> str | None:
        return self.render.desktop.screenshot_path if self.render and self.render.desktop else None

    @property
    def mobile_screenshot(self) -> str | None:
        return self.render.mobile.screenshot_path if self.render and self.render.mobile else None


class SiteCrawler:
    """Crawl one prospect website and write its artifacts."""

    def __init__(
        self,
        settings: Settings,
        fetcher: Fetcher | None = None,
        browser_session: BrowserSession | None = None,
        *,
        use_browser: bool | None = None,
    ) -> None:
        self.settings = settings
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        self._browser_session = browser_session
        self.use_browser = settings.browser_enabled if use_browser is None else use_browser

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher(self.settings)
        return self._fetcher

    def close(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    def __enter__(self) -> SiteCrawler:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    def crawl(self, url: str, artifact_dir: Path) -> CrawlOutcome:
        """Crawl ``url``.  Never raises for a broken prospect website."""
        artifact_dir = Path(artifact_dir)
        outcome = CrawlOutcome(requested_url=url, artifact_dir=str(artifact_dir))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pages_dir = artifact_dir / "pages"

        home = self.fetcher.fetch(url)
        outcome.final_url = home.final_url
        outcome.http_status = home.status_code
        outcome.scan_status = home.scan_status
        outcome.error = home.error

        if not home.ok:
            log.info(
                "homepage fetch unusable",
                extra={"url": url, "status": str(home.scan_status), "error": home.error},
            )
            self._write_scan_json(artifact_dir, outcome)
            return outcome

        base_url = home.final_url or url
        site_domain = normalize_domain(base_url)
        outcome.homepage_html = home.text
        outcome.homepage_html_path = str(self._save_html(artifact_dir / "homepage.html", home.text))
        outcome.pages.append(self._page_record(home, outcome.homepage_html_path))
        outcome.page_html.append((base_url, home.text))

        # -- follow a small budget of prioritised same-domain links --------
        budget = max(0, self.settings.crawl_max_pages_per_domain - 1)
        seen = {base_url}
        for candidate in prioritized_same_domain_links(
            home.text, base_url, limit=budget, already_seen=seen
        ):
            result = self.fetcher.fetch(candidate.url, retries=1)
            seen.add(candidate.url)
            if not result.ok:
                outcome.pages.append(self._page_record(result, None))
                continue
            path = self._save_html(pages_dir / f"{url_slug(candidate.url)}.html", result.text)
            outcome.pages.append(self._page_record(result, str(path)))
            outcome.page_html.append((result.final_url or candidate.url, result.text))

        # -- evidence extraction ------------------------------------------
        outcome.contacts = merge_contact_results(
            [extract_contacts_from_html(html, page_url, site_domain)
             for page_url, html in outcome.page_html]
        )
        facts = ExtractedFacts()
        for page_url, html in outcome.page_html:
            extract_facts_from_page(html, page_url, facts)
        facts.emails = [c.normalized_email for c in outcome.contacts.contacts]
        outcome.facts = facts
        outcome.title = facts.titles[0] if facts.titles else None
        outcome.meta_description = facts.meta_descriptions[0] if facts.meta_descriptions else None
        outcome.assets = collect_assets(outcome.page_html)

        # -- render --------------------------------------------------------
        if self.use_browser:
            availability = browser_available() if self._browser_session is None else None
            if availability is not None and not availability.available:
                outcome.browser_available = False
                outcome.notes.append(f"browser unavailable: {availability.reason}")
                log.warning("skipping render", extra={"reason": availability.reason})
            else:
                outcome.browser_available = True
                outcome.render = render_site(
                    base_url, artifact_dir, session=self._browser_session
                )
                if outcome.render.error:
                    outcome.notes.append(outcome.render.error)
        else:
            outcome.notes.append("browser rendering disabled by configuration")

        self._write_scan_json(artifact_dir, outcome)
        log.info(
            "crawl complete",
            extra={
                "url": url,
                "pages": len(outcome.page_html),
                "contacts": len(outcome.contacts.contacts),
                "rendered": bool(outcome.render and outcome.render.ok),
            },
        )
        return outcome

    # ------------------------------------------------------------------
    @staticmethod
    def _save_html(path: Path, html: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8", errors="replace")
        return path

    @staticmethod
    def _page_record(result: FetchResult, html_path: str | None) -> CrawledPage:
        return CrawledPage(
            url=result.requested_url,
            final_url=result.final_url,
            status_code=result.status_code,
            html_path=html_path,
            bytes=len(result.text or ""),
            scan_status=str(result.scan_status),
            error=result.error,
        )

    def _write_scan_json(self, artifact_dir: Path, outcome: CrawlOutcome) -> None:
        """Human-inspectable dump of everything this crawl saw."""
        payload = {
            "requested_url": outcome.requested_url,
            "final_url": outcome.final_url,
            "http_status": outcome.http_status,
            "scan_status": str(outcome.scan_status),
            "error": outcome.error,
            "title": outcome.title,
            "meta_description": outcome.meta_description,
            "pages": [p.to_dict() for p in outcome.pages],
            "contacts": [
                {
                    "email": c.normalized_email,
                    "type": str(c.contact_type),
                    "source_url": c.source_url,
                    "method": c.source_method,
                    "confidence": c.confidence,
                }
                for c in outcome.contacts.contacts
            ],
            "rejected_contacts": outcome.contacts.rejected[:40],
            "facts": outcome.facts.to_dict(),
            "evidence": outcome.facts.evidence,
            "assets": outcome.assets.to_dict(),
            "browser": outcome.render.to_metrics_dict() if outcome.render else None,
            "notes": outcome.notes,
        }
        try:
            (artifact_dir / "scan.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - disk issues
            log.warning("could not write scan.json", extra={"error": str(exc)})

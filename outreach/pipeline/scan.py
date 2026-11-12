"""Crawl + render + deterministic score + public contact extraction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from outreach.crawler.browser import BrowserSession, browser_available
from outreach.crawler.fetch import Fetcher
from outreach.crawler.site import CrawlOutcome, SiteCrawler, business_artifact_dir
from outreach.events import record_event
from outreach.logging import get_logger, safe_extra
from outreach.models.business import Business
from outreach.models.campaign import Campaign
from outreach.models.contact import Contact
from outreach.models.enums import ContactabilityStatus, EventType, ScanStatus
from outreach.models.scan import WebsiteScan
from outreach.pipeline.context import RunContext, StageResult
from outreach.scoring.technical import score_website

log = get_logger(__name__)


def businesses_to_scan(
    session: Session, campaign_id: int | None, *, limit: int, force: bool = False
) -> list[Business]:
    """Businesses with a website that have not been scanned successfully yet."""
    stmt = select(Business).where(Business.website_url.is_not(None))
    if campaign_id is not None:
        stmt = stmt.where(Business.campaign_id == campaign_id)
    candidates = list(session.scalars(stmt.order_by(Business.id)).all())
    if force:
        return candidates[:limit]
    out: list[Business] = []
    for business in candidates:
        latest = latest_scan(session, business.id)
        if latest is None or latest.scan_status != ScanStatus.OK:
            out.append(business)
        if len(out) >= limit:
            break
    return out


def latest_scan(session: Session, business_id: int) -> WebsiteScan | None:
    return session.scalars(
        select(WebsiteScan)
        .where(WebsiteScan.business_id == business_id)
        .order_by(WebsiteScan.id.desc())
        .limit(1)
    ).first()


def persist_contacts(session: Session, business: Business, outcome: CrawlOutcome) -> int:
    """Store published addresses, never guessed ones."""
    existing = {c.normalized_email for c in business.contacts}
    added = 0
    for contact in outcome.contacts.contacts:
        if contact.normalized_email in existing:
            continue
        session.add(
            Contact(
                business_id=business.id,
                email=contact.email,
                normalized_email=contact.normalized_email,
                contact_type=str(contact.contact_type),
                source_url=contact.source_url,
                source_method=contact.source_method,
                confidence=contact.confidence,
            )
        )
        existing.add(contact.normalized_email)
        added += 1
        record_event(
            session, "business", business.id, EventType.CONTACT_FOUND,
            {
                "email": contact.normalized_email,
                "type": str(contact.contact_type),
                "source_url": contact.source_url,
                "method": contact.source_method,
            },
        )
    return added


def scan_business(
    session: Session,
    ctx: RunContext,
    business: Business,
    crawler: SiteCrawler,
) -> WebsiteScan:
    """Crawl, render, score and persist one business's website."""
    settings = ctx.settings
    artifact_dir = business_artifact_dir(settings, business.id)

    if not business.website_url:
        scan = WebsiteScan(
            business_id=business.id, scan_status=ScanStatus.NO_WEBSITE,
            error="no website on record",
        )
        session.add(scan)
        session.flush()
        return scan

    record_event(
        session, "business", business.id, EventType.SCAN_STARTED,
        {"url": business.website_url},
    )
    outcome = crawler.crawl(business.website_url, artifact_dir)

    scan = WebsiteScan(
        business_id=business.id,
        requested_url=outcome.requested_url,
        final_url=outcome.final_url,
        http_status=outcome.http_status,
        scan_status=str(outcome.scan_status),
        desktop_screenshot_path=outcome.desktop_screenshot,
        mobile_screenshot_path=outcome.mobile_screenshot,
        homepage_html_path=outcome.homepage_html_path,
        title=(outcome.title or None),
        meta_description=(outcome.meta_description or None),
        pages_json=[p.to_dict() for p in outcome.pages],
        pages_crawled=len(outcome.page_html),
        browser_metrics_json=outcome.render.to_metrics_dict() if outcome.render else None,
        error=outcome.error,
    )

    if outcome.ok:
        technical = score_website(
            outcome.homepage_html,
            outcome.final_url or business.website_url,
            outcome.render,
            extra_notes=list(outcome.notes),
        )
        scan.technical_site_quality_score = technical.site_quality_score
        scan.technical_findings_json = technical.to_findings_json()

    session.add(scan)
    session.flush()

    if outcome.ok:
        added = persist_contacts(session, business, outcome)
        business.contactability_status = (
            ContactabilityStatus.CONTACTABLE
            if business.contacts
            else ContactabilityStatus.NO_PUBLIC_EMAIL
        )
        record_event(
            session, "business", business.id, EventType.SCAN_COMPLETED,
            {
                "scan_id": scan.id,
                "score": scan.technical_site_quality_score,
                "pages": scan.pages_crawled,
                "contacts_added": added,
            },
        )
    else:
        record_event(
            session, "business", business.id, EventType.SCAN_FAILED,
            {"scan_id": scan.id, "status": str(outcome.scan_status), "error": outcome.error},
        )
    return scan


def run_scan(session: Session, ctx: RunContext) -> StageResult:
    """Scan every unscanned business in the campaign."""
    stage = StageResult(name="scan")
    settings = ctx.settings
    limit = ctx.effective_limit(settings.max_scans_per_run)
    targets = businesses_to_scan(session, ctx.campaign_id, limit=limit, force=ctx.force)
    if not targets:
        stage.notes.append("no businesses need scanning")
        return stage

    availability = browser_available() if settings.browser_enabled else None
    use_browser = bool(availability and availability.available)
    if settings.browser_enabled and not use_browser:
        stage.notes.append(
            f"browser rendering unavailable, scoring from HTML only: "
            f"{availability.reason if availability else 'unknown'}"
        )
    elif not settings.browser_enabled:
        stage.notes.append("browser rendering disabled by configuration")

    browser: BrowserSession | None = None
    fetcher = Fetcher(settings)
    try:
        if use_browser:
            browser = BrowserSession()
            browser.start()
        crawler = SiteCrawler(
            settings, fetcher=fetcher, browser_session=browser, use_browser=use_browser
        )
        for business in targets:
            try:
                scan = scan_business(session, ctx, business, crawler)
            except Exception as exc:  # one broken site never kills the campaign
                log.exception("scan failed", extra={"business_id": business.id})
                stage.errors.append(f"business {business.id}: {exc}")
                stage.bump("errors")
                session.rollback()
                continue
            stage.bump("scanned")
            if scan.scan_status == ScanStatus.OK:
                stage.bump("ok")
                if scan.desktop_screenshot_path:
                    stage.bump("screenshots")
            else:
                stage.bump(f"status_{str(scan.scan_status).lower()}")
            session.commit()
    finally:
        fetcher.close()
        if browser is not None:
            browser.stop()

    contactable = int(
        session.scalar(
            select(func.count(func.distinct(Contact.business_id))).where(
                Contact.business_id.in_([b.id for b in targets])
            )
        )
        or 0
    )
    stage.bump("with_public_email", contactable)
    log.info("scan stage complete", extra=safe_extra(stage.counts))
    return stage


def artifact_paths(settings, business_id: int) -> dict[str, str]:
    """Deterministic artifact locations for one business."""
    base = business_artifact_dir(settings, business_id)
    return {
        "dir": str(base),
        "homepage_html": str(base / "homepage.html"),
        "desktop": str(base / "desktop.png"),
        "mobile": str(base / "mobile.png"),
        "scan_json": str(base / "scan.json"),
        "pages": str(base / "pages"),
        "assets": str(base / "assets"),
    }


def campaign_for(session: Session, campaign_id: int | None) -> Campaign | None:
    return session.get(Campaign, campaign_id) if campaign_id else None


def homepage_text(scan: WebsiteScan, limit: int = 8000) -> str:
    from outreach.crawler.content import visible_text

    if not scan.homepage_html_path:
        return ""
    path = Path(scan.homepage_html_path)
    if not path.is_file():
        return ""
    return visible_text(path.read_text(encoding="utf-8", errors="replace"))[:limit]

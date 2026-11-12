"""Content extraction, redesign generation, QA and preview publishing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.ai.base import AIProvider, get_ai_provider, record_ai_usage
from outreach.builder.content import build_business_content, build_site_content
from outreach.builder.qa import run_qa
from outreach.builder.renderer import RenderedSite, render_site, template_version
from outreach.crawler.assets import DiscoveredAsset, download_asset
from outreach.crawler.browser import BrowserSession, browser_available
from outreach.crawler.fetch import Fetcher
from outreach.crawler.site import business_artifact_dir
from outreach.db import utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.models.build import Build
from outreach.models.business import Business
from outreach.models.business_content import BusinessContent
from outreach.models.enums import BuildStatus, EventType
from outreach.pipeline.context import RunContext, StageResult
from outreach.pipeline.qualify import build_candidates, contact_dicts
from outreach.pipeline.scan import campaign_for
from outreach.previews.manager import PreviewManager
from outreach.previews.publisher import get_publisher
from outreach.state import is_paused

log = get_logger(__name__)


@dataclass
class BuildOutcome:
    business_id: int
    status: str
    build: Build | None = None
    reason: str = ""
    rendered: RenderedSite | None = None


def load_scan_evidence(settings: Any, business_id: int) -> dict[str, Any]:
    """Read back the ``scan.json`` written during the crawl."""
    path = business_artifact_dir(settings, business_id) / "scan.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - disk / corruption
        return {}


def page_texts_for(settings: Any, business_id: int, limit: int = 6) -> list[str]:
    """Visible text of every crawled page -- the evidence corpus for guardrails."""
    from outreach.crawler.content import visible_text

    base = business_artifact_dir(settings, business_id)
    texts: list[str] = []
    homepage = base / "homepage.html"
    if homepage.is_file():
        texts.append(visible_text(homepage.read_text(encoding="utf-8", errors="replace")))
    pages_dir = base / "pages"
    if pages_dir.is_dir():
        for path in sorted(pages_dir.glob("*.html"))[: limit - 1]:
            texts.append(visible_text(path.read_text(encoding="utf-8", errors="replace")))
    return texts


def download_prospect_assets(
    settings: Any, business_id: int, evidence: dict[str, Any], *, limit: int = 4
) -> list[dict[str, Any]]:
    """Fetch the prospect's own logo and photos so the preview can reuse them.

    Only assets already ownership-checked by the crawler are considered, and
    anything that fails to download is simply absent -- the template is
    designed to look good with no photos at all.
    """
    assets_meta = evidence.get("assets") or {}
    dest = business_artifact_dir(settings, business_id) / "assets"
    wanted: list[dict[str, Any]] = []
    if assets_meta.get("logo"):
        wanted.append(assets_meta["logo"])
    if assets_meta.get("favicon"):
        wanted.append(assets_meta["favicon"])
    wanted.extend((assets_meta.get("photos") or [])[:limit])

    downloaded: list[dict[str, Any]] = []
    if not wanted:
        return downloaded
    with Fetcher(settings) as fetcher:
        for meta in wanted:
            if not isinstance(meta, dict) or not meta.get("url"):
                continue
            asset = DiscoveredAsset(
                url=str(meta["url"]),
                kind=str(meta.get("kind") or "photo"),
                source_url=str(meta.get("source_url") or ""),
                alt=str(meta.get("alt") or ""),
            )
            saved = download_asset(fetcher, asset, dest)
            if saved is not None:
                downloaded.append(saved.to_dict())
    return downloaded


def extract_content_for(
    session: Session, ctx: RunContext, business: Business, *, provider: AIProvider | None
) -> tuple[Any, dict[str, Any], list[str]]:
    """Build (and persist) the factual BusinessContent for one business."""
    settings = ctx.settings
    evidence = load_scan_evidence(settings, business.id)
    facts = evidence.get("facts") or {}
    source_evidence = evidence.get("evidence") or {}
    page_texts = page_texts_for(settings, business.id)
    contacts = contact_dicts(business)
    campaign = campaign_for(session, ctx.campaign_id)
    vertical = campaign.vertical if campaign else ctx.vertical

    downloaded = download_prospect_assets(settings, business.id, evidence)
    assets_payload: dict[str, Any] = {
        "logo": next((a for a in downloaded if a.get("kind") == "logo"), None),
        "favicon": next((a for a in downloaded if a.get("kind") == "favicon"), None),
        "photos": [a for a in downloaded if a.get("kind") == "photo"],
    }

    use_ai = settings.features.ai_content_extraction and provider is not None
    content, guard, ai_result = build_business_content(
        provider=provider,
        use_ai=use_ai,
        vertical=vertical,
        business_name=business.name,
        facts=facts,
        evidence=source_evidence,
        contacts=contacts,
        assets=assets_payload,
        website_url=business.website_url,
        page_texts=page_texts,
    )
    if ai_result is not None:
        record_ai_usage(session, ai_result, business_id=business.id)

    row = BusinessContent(
        business_id=business.id,
        content_json=content.model_dump(),
        source_evidence_json={"evidence": source_evidence, "guardrails": guard.to_dict()},
        extraction_method="ai" if (use_ai and not guard.fell_back) else "deterministic",
        provider=provider.name if (use_ai and provider) else None,
    )
    session.add(row)
    session.flush()
    record_event(
        session, "business", business.id, EventType.CONTENT_EXTRACTED,
        {
            "content_id": row.id,
            "method": row.extraction_method,
            "dropped_claims": len(guard.dropped),
        },
    )
    return content, {"downloaded_assets": downloaded, "guard": guard.to_dict()}, page_texts


def build_for_business(
    session: Session,
    ctx: RunContext,
    business: Business,
    *,
    provider: AIProvider | None,
    browser: BrowserSession | None,
    template_name: str = "dentist",
) -> BuildOutcome:
    """Extract content, render the redesign, QA it and record the build."""
    settings = ctx.settings
    manager = PreviewManager(settings)

    build = Build(
        business_id=business.id,
        template_name=template_name,
        template_version=template_version(template_name),
        status=BuildStatus.BUILDING,
    )
    session.add(build)
    session.flush()
    record_event(session, "build", build.id, EventType.BUILD_STARTED, {"business_id": business.id})

    try:
        content, extras, page_texts = extract_content_for(
            session, ctx, business, provider=provider
        )
        site, guard, ai_result = build_site_content(
            provider=provider,
            use_ai=settings.features.ai_content_extraction and provider is not None,
            vertical=(campaign_for(session, ctx.campaign_id) or ctx).vertical,
            content=content,
            page_texts=page_texts,
        )
        if ai_result is not None:
            record_ai_usage(session, ai_result, business_id=business.id)

        output_dir = manager.dir_for(business.id)
        rendered = render_site(
            content, site, output_dir,
            template_name=template_name,
            source_assets=extras["downloaded_assets"],
        )
        build.content_json = {
            "business_content": content.model_dump(),
            "site_content": site.model_dump(),
            "guardrails": guard.to_dict(),
            "sections": rendered.sections,
        }
        build.content_hash = rendered.content_hash
        build.preview_path = str(rendered.output_dir)
        build.built_at = utcnow()
    except Exception as exc:
        log.exception("build failed", extra={"business_id": business.id})
        build.status = BuildStatus.FAILED
        build.error = str(exc)[:2000]
        record_event(
            session, "build", build.id, EventType.BUILD_FAILED,
            {"business_id": business.id, "error": str(exc)[:400]},
        )
        return BuildOutcome(business.id, BuildStatus.FAILED, build, str(exc))

    qa = run_qa(
        Path(build.preview_path),
        session=browser,
        screenshot_dir=business_artifact_dir(settings, business.id) / "qa",
        use_browser=settings.browser_enabled,
    )
    build.qa_json = qa.to_dict()
    build.qa_passed = qa.passed
    build.qa_screenshot_path = qa.screenshot_path

    if not manager.verify_noindex(business.id):
        qa.passed = False
        build.qa_passed = False
        qa.failures.append("preview is missing its noindex directive")
        build.qa_json = qa.to_dict()

    if not build.qa_passed:
        build.status = BuildStatus.QA_FAILED
        build.error = "; ".join(qa.failures)[:2000]
        record_event(
            session, "build", build.id, EventType.QA_FAILED,
            {"business_id": business.id, "failures": qa.failures[:6]},
        )
        return BuildOutcome(business.id, BuildStatus.QA_FAILED, build, build.error)

    build.status = BuildStatus.BUILT
    build.preview_url = manager.public_url(business.id)
    record_event(
        session, "build", build.id, EventType.QA_PASSED, {"business_id": business.id}
    )
    record_event(
        session, "build", build.id, EventType.BUILD_COMPLETED,
        {
            "business_id": business.id,
            "preview_path": build.preview_path,
            "sections": (build.content_json or {}).get("sections"),
        },
    )
    return BuildOutcome(business.id, BuildStatus.BUILT, build, "built")


def run_build(
    session: Session, ctx: RunContext, *, template_name: str = "dentist"
) -> StageResult:
    """Build previews for the highest-priority contactable redesign candidates."""
    stage = ctx.require_feature("build", "FEATURE_SITE_BUILDING")
    if stage is not None:
        # Still report what *would* have been built, so a dry run is useful.
        limit = ctx.effective_limit(ctx.settings.max_builds_per_run)
        planned = build_candidates(session, ctx.campaign_id, limit)
        stage.counts["would_build"] = len(planned)
        stage.items = [
            {"business_id": b.id, "name": b.name, "priority": b.build_priority}
            for b in planned
        ]
        return stage

    stage = StageResult(name="build")
    settings = ctx.settings
    limit = ctx.effective_limit(settings.max_builds_per_run)
    targets = build_candidates(session, ctx.campaign_id, limit)
    if not targets:
        stage.notes.append("no build candidates")
        return stage

    provider = None
    if settings.features.ai_content_extraction:
        provider = get_ai_provider(settings)
        health = provider.health_check()
        if not health.available:
            stage.notes.append(f"AI content extraction unavailable: {health.reason}")
            provider = None

    browser: BrowserSession | None = None
    availability = browser_available() if settings.browser_enabled else None
    try:
        if availability and availability.available:
            browser = BrowserSession()
            browser.start()
        elif settings.browser_enabled:
            stage.notes.append(
                f"browser QA unavailable: {availability.reason if availability else 'unknown'}"
            )
        for business in targets:
            outcome = build_for_business(
                session, ctx, business, provider=provider, browser=browser,
                template_name=template_name,
            )
            stage.bump("attempted")
            stage.bump(str(outcome.status).lower())
            if outcome.reason and outcome.status != BuildStatus.BUILT:
                stage.errors.append(f"business {business.id}: {outcome.reason[:200]}")
            stage.items.append(
                {
                    "business_id": business.id,
                    "name": business.name,
                    "status": str(outcome.status),
                    "preview_path": outcome.build.preview_path if outcome.build else None,
                }
            )
            session.commit()
    finally:
        if browser is not None:
            browser.stop()
    return stage


def run_publish(session: Session, ctx: RunContext) -> StageResult:
    """Publish QA-passed previews through the configured publisher."""
    stage = ctx.require_feature("publish", "FEATURE_PREVIEW_PUBLISHING")
    if stage is not None:
        return stage

    stage = StageResult(name="publish")
    settings = ctx.settings

    if ctx.dry_run:
        stage.executed = False
        stage.skipped_reason = "DRY RUN: previews are never published to a production target"
        return stage
    if is_paused(session):
        stage.executed = False
        stage.skipped_reason = "system is globally paused"
        return stage

    publisher = get_publisher(settings)
    ok, reason = publisher.available()
    if not ok:
        stage.executed = False
        stage.skipped_reason = f"publisher unavailable: {reason}"
        return stage

    stmt = select(Build).where(Build.status == BuildStatus.BUILT, Build.qa_passed.is_(True))
    if ctx.campaign_id is not None:
        stmt = stmt.join(Business, Business.id == Build.business_id).where(
            Business.campaign_id == ctx.campaign_id
        )
    for build in session.scalars(stmt.order_by(Build.id)).all():
        result = publisher.publish(build.business_id, Path(build.preview_path or ""), dry_run=False)
        if result.published:
            build.status = BuildStatus.PUBLISHED
            build.published_at = utcnow()
            build.preview_url = result.url or build.preview_url
            stage.bump("published")
            record_event(
                session, "build", build.id, EventType.PREVIEW_PUBLISHED,
                {"destination": result.destination, "url": build.preview_url},
            )
        else:
            stage.bump("failed")
            stage.errors.append(f"build {build.id}: {result.reason}")
        session.commit()
    return stage

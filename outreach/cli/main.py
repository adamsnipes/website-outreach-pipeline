"""``python -m outreach`` -- the operator interface.

Grouped subcommands where it helps (``gmail``, ``suppress``, ``previews``,
``scheduler``), flat commands where a group would be noise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from outreach import __version__
from outreach.config import FEATURE_FLAG_DOCS, get_settings, reset_settings_cache
from outreach.db import init_db, session_scope
from outreach.logging import configure_logging, get_logger

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="StoopCraft automated prospecting, redesign and cold-outreach system.",
)
gmail_app = typer.Typer(no_args_is_help=True, help="Gmail OAuth and transport.")
suppress_app = typer.Typer(no_args_is_help=True, help="Permanent do-not-contact list.")
previews_app = typer.Typer(no_args_is_help=True, help="Generated preview sites.")
scheduler_app = typer.Typer(no_args_is_help=True, help="Cron / systemd scheduling.")
app.add_typer(gmail_app, name="gmail")
app.add_typer(suppress_app, name="suppress")
app.add_typer(previews_app, name="previews")
app.add_typer(scheduler_app, name="scheduler")

console = Console()
log = get_logger(__name__)


def _settings() -> Any:
    return get_settings()


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        console.print_json(json.dumps(payload, default=str))


@app.callback()
def main_callback(
    log_level: str = typer.Option(None, "--log-level", help="Override LOG_LEVEL."),
    log_format: str = typer.Option(None, "--log-format", help="text or json."),
) -> None:
    """Configure logging before any command runs."""
    configure_logging(log_level, log_format)


# ---------------------------------------------------------------------------
# setup / introspection
# ---------------------------------------------------------------------------
@app.command("version")
def version_command() -> None:
    """Print the application version."""
    console.print(f"stoopcraft-outreach {__version__}")


@app.command("init-db")
def init_db_command(
    force_create_all: bool = typer.Option(
        False, "--create-all", help="Skip Alembic and create tables directly."
    ),
) -> None:
    """Create or migrate the database."""
    settings = _settings()
    method = init_db(settings, use_alembic=not force_create_all)
    console.print(f"[green]Database ready[/green] ({method}) at {settings.sqlalchemy_url()}")


@app.command("config")
def config_command(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the effective configuration (secrets redacted)."""
    settings = _settings()
    data = settings.public_dict()
    if as_json:
        _emit(data, True)
        return
    table = Table(title="Effective configuration", show_lines=False)
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value", style="white", overflow="fold")
    for key in sorted(data):
        table.add_row(key, str(data[key]))
    console.print(table)


@app.command("features")
def features_command(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show every feature flag, its state and what it gates."""
    settings = _settings()
    flags = settings.features.as_env_dict()
    if as_json:
        _emit(
            {
                "flags": flags,
                "OUTREACH_SEND_ENABLED": settings.outreach_send_enabled,
                "docs": FEATURE_FLAG_DOCS,
            },
            True,
        )
        return
    table = Table(title="Feature flags")
    table.add_column("Flag", style="cyan", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("What it gates", overflow="fold")
    for name, value in flags.items():
        state = "[green]ON[/green]" if value else "[dim]off[/dim]"
        table.add_row(name, state, FEATURE_FLAG_DOCS.get(name, ""))
    table.add_row(
        "OUTREACH_SEND_ENABLED",
        "[green]ON[/green]" if settings.outreach_send_enabled else "[dim]off[/dim]",
        "Master switch that must be combined with FEATURE_GMAIL_SEND before anything is sent.",
    )
    console.print(table)
    console.print(
        f"\nDaily send limit: [bold]{settings.effective_daily_send_limit}[/bold] "
        f"(defensive hard maximum {settings.hard_max_daily_send_limit})"
    )


@app.command("doctor")
def doctor_command() -> None:
    """Check every external dependency without touching anything."""
    from outreach.ai.base import get_ai_provider
    from outreach.crawler.browser import browser_available
    from outreach.messaging.gmail import GmailClient

    settings = _settings()
    table = Table(title="Environment check")
    table.add_column("Component", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    browser = browser_available()
    table.add_row(
        "Playwright Chromium",
        "[green]ok[/green]" if browser.available else "[yellow]missing[/yellow]",
        browser.reason,
    )
    health = get_ai_provider(settings).health_check()
    table.add_row(
        f"AI provider ({settings.ai_provider})",
        "[green]ok[/green]" if health.available else "[yellow]unavailable[/yellow]",
        health.reason,
    )
    status = GmailClient(settings).status()
    table.add_row(
        "Gmail OAuth",
        "[green]ok[/green]" if status.authenticated else "[yellow]not authenticated[/yellow]",
        status.reason,
    )
    problems = settings.compliance_problems()
    table.add_row(
        "Sender compliance",
        "[green]ok[/green]" if not problems else "[yellow]incomplete[/yellow]",
        "; ".join(problems) or "sender identity is complete",
    )
    google_ready = bool(settings.google_maps_api_key)
    table.add_row(
        "Google Places key",
        "[green]set[/green]" if google_ready else "[dim]unset[/dim]",
        "GOOGLE_MAPS_API_KEY" + (" is configured" if google_ready else " is not configured"),
    )
    console.print(table)


# ---------------------------------------------------------------------------
# campaigns / pipeline
# ---------------------------------------------------------------------------
campaign_app = typer.Typer(no_args_is_help=True, help="Campaign management.")
app.add_typer(campaign_app, name="campaign")


@campaign_app.command("create")
def campaign_create(
    vertical: str = typer.Option(..., "--vertical"),
    city: str = typer.Option(..., "--city"),
    state: str = typer.Option(..., "--state"),
    country: str = typer.Option("US", "--country"),
) -> None:
    """Create (or resume) a campaign."""
    from outreach.pipeline.discover import get_or_create_campaign

    with session_scope() as session:
        campaign, created = get_or_create_campaign(
            session, vertical=vertical, city=city, state=state, country=country
        )
        session.flush()
        verb = "Created" if created else "Resumed existing"
        console.print(f"[green]{verb}[/green] campaign [bold]{campaign.id}[/bold]: {campaign.label}")


@campaign_app.command("list")
def campaign_list() -> None:
    """List campaigns."""
    from sqlalchemy import select

    from outreach.models.campaign import Campaign

    with session_scope() as session:
        campaigns = list(session.scalars(select(Campaign).order_by(Campaign.id)).all())
        if not campaigns:
            console.print("[dim]No campaigns yet.[/dim]")
            return
        table = Table(title="Campaigns")
        for column in ("ID", "Vertical", "City", "State", "Status", "Businesses"):
            table.add_column(column)
        for campaign in campaigns:
            table.add_row(
                str(campaign.id), campaign.vertical, campaign.city, campaign.state,
                campaign.status, str(len(campaign.businesses)),
            )
        console.print(table)


@app.command("discover")
def discover_command(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    provider: str = typer.Option("fixtures", "--provider", help="fixtures | google"),
    vertical: str = typer.Option("dentist", "--vertical"),
    city: str = typer.Option("Phoenix", "--city"),
    state: str = typer.Option("AZ", "--state"),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Discover businesses for a campaign."""
    from outreach.pipeline.orchestrator import run_pipeline

    result = run_pipeline(
        vertical=vertical, city=city, state=state, campaign_id=campaign_id,
        provider=provider, limit=limit, stages={"discover"},
    )
    _print_stages(result)


@app.command("scan")
def scan_command(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    limit: int = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="Re-scan already-scanned sites."),
    provider: str = typer.Option("fixtures", "--provider"),
) -> None:
    """Crawl, render and deterministically score campaign websites."""
    from outreach.pipeline.orchestrator import run_pipeline

    result = run_pipeline(
        campaign_id=campaign_id, limit=limit, force=force, provider=provider,
        stages={"scan", "qualify"},
    )
    _print_stages(result)


@app.command("scan-url")
def scan_url_command(
    url: str = typer.Argument(..., help="A single website to scan and score."),
    out: Path = typer.Option(None, "--out", help="Directory for artifacts."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Scan one URL ad hoc, without touching the database."""
    import tempfile

    from outreach.crawler.site import SiteCrawler
    from outreach.scoring.technical import score_website

    settings = _settings()
    target = Path(out) if out else Path(tempfile.mkdtemp(prefix="outreach-scan-"))
    with SiteCrawler(settings) as crawler:
        outcome = crawler.crawl(url, target)
    if not outcome.ok:
        console.print(f"[red]Scan failed[/red] ({outcome.scan_status}): {outcome.error}")
        raise typer.Exit(code=1)
    score = score_website(outcome.homepage_html, outcome.final_url or url, outcome.render)
    payload = {
        "url": outcome.final_url,
        "artifacts": str(target),
        "score": score.site_quality_score,
        "findings": score.to_findings_json(),
        "contacts": [c.normalized_email for c in outcome.contacts.contacts],
        "pages_crawled": len(outcome.page_html),
    }
    if as_json:
        _emit(payload, True)
        return
    console.print(
        Panel(
            f"[bold]{outcome.title or outcome.final_url}[/bold]\n"
            f"score [bold]{score.site_quality_score:.1f}[/bold]/100  "
            f"(mobile {score.mobile.score:.0f}, technical {score.technical.score:.0f}, "
            f"conversion {score.conversion.score:.0f}, performance {score.performance.score:.0f})\n"
            f"pages crawled: {len(outcome.page_html)}   "
            f"public emails: {', '.join(c.normalized_email for c in outcome.contacts.contacts) or 'none'}\n"
            f"artifacts: {target}",
            title=outcome.final_url or url,
        )
    )
    table = Table(title="Findings")
    for column in ("Severity", "Code", "Evidence"):
        table.add_column(column, overflow="fold")
    for finding in sorted(score.findings, key=lambda f: -f.weight)[:15]:
        table.add_row(finding.severity, finding.code, finding.evidence)
    console.print(table)


@app.command("prospects")
def prospects_command(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    status: str = typer.Option(None, "--status", help="Filter by qualification status."),
    limit: int = typer.Option(50, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List qualified prospects, best build candidates first."""
    from sqlalchemy import select

    from outreach.models.business import Business
    from outreach.pipeline.qualify import contact_dicts

    with session_scope() as session:
        stmt = select(Business)
        if campaign_id is not None:
            stmt = stmt.where(Business.campaign_id == campaign_id)
        if status:
            stmt = stmt.where(Business.qualification_status == status.upper())
        stmt = stmt.order_by(Business.build_priority.desc().nullslast(), Business.id).limit(limit)
        businesses = list(session.scalars(stmt).all())

        rows: list[dict[str, Any]] = [
            {
                "id": b.id,
                "name": b.name,
                "domain": b.normalized_domain,
                "score": b.combined_score,
                "status": b.qualification_status,
                "contactable": b.contactability_status,
                "priority": b.build_priority,
                # Ranked exactly as the composer ranks them, so the address
                # shown is the one that would actually be written to.
                "emails": [c["normalized_email"] for c in contact_dicts(b)],
            }
            for b in businesses
        ]
    if as_json:
        _emit(rows, True)
        return
    if not rows:
        console.print("[dim]No prospects match.[/dim]")
        return
    table = Table(title="Prospects")
    for column in ("ID", "Name", "Domain", "Score", "Status", "Contact", "Priority", "Email"):
        table.add_column(column, overflow="fold")
    for row in rows:
        emails: Any = row["emails"]
        score: Any = row["score"]
        priority: Any = row["priority"]
        table.add_row(
            str(row["id"]),
            str(row["name"]),
            str(row["domain"] or "-"),
            f"{float(score):.1f}" if score is not None else "-",
            str(row["status"]),
            str(row["contactable"]),
            f"{float(priority):.3f}" if priority is not None else "-",
            str(emails[0]) if isinstance(emails, list) and emails else "-",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------
@app.command("ai-check")
def ai_check_command(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check whether the configured AI CLI is installed and usable."""
    from outreach.ai.base import get_ai_provider

    settings = _settings()
    provider = get_ai_provider(settings)
    health = provider.health_check()
    if as_json:
        _emit({"provider": provider.name, **health.as_dict()}, True)
        return
    colour = "green" if health.available else "yellow"
    console.print(
        Panel(
            f"provider: [bold]{provider.name}[/bold]\n"
            f"available: [{colour}]{health.available}[/{colour}]\n"
            f"reason: {health.reason}\n"
            f"executable: {health.executable or '-'}\n"
            f"version: {health.version or '-'}\n"
            f"non-interactive: {health.supports_non_interactive}\n"
            f"json output: {health.supports_json_output}",
            title="AI provider health",
        )
    )
    if not health.available:
        console.print(
            "[dim]AI features stay disabled until this passes; every AI stage has a "
            "deterministic fallback.[/dim]"
        )


@app.command("ai-score")
def ai_score_command(
    business_id: int = typer.Option(..., "--business-id"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Run AI visual scoring for one business."""
    from outreach.ai.base import get_ai_provider
    from outreach.models.business import Business
    from outreach.pipeline.scan import latest_scan
    from outreach.scoring.visual import latest_assessment, run_visual_scoring, should_ai_score

    settings = _settings()
    with session_scope() as session:
        business = session.get(Business, business_id)
        if business is None:
            console.print(f"[red]No business {business_id}[/red]")
            raise typer.Exit(code=1)
        scan = latest_scan(session, business_id)
        if scan is None:
            console.print("[red]That business has not been scanned yet.[/red]")
            raise typer.Exit(code=1)
        ok, reason = should_ai_score(
            settings, business.qualification_status,
            already_assessed=latest_assessment(session, business_id) is not None,
            force=force,
        )
        if not ok:
            console.print(f"[yellow]Skipped:[/yellow] {reason}")
            return
        provider = get_ai_provider(settings)
        health = provider.health_check()
        if not health.available:
            console.print(f"[red]AI provider unavailable:[/red] {health.reason}")
            raise typer.Exit(code=1)
        campaign = business.campaign
        outcome = run_visual_scoring(
            session, settings, provider, business, scan,
            vertical=campaign.vertical if campaign else "local business",
        )
        if outcome.scored and outcome.ai_score:
            console.print_json(json.dumps(outcome.ai_score.model_dump(), default=str))
        else:
            console.print(f"[red]AI scoring failed:[/red] {outcome.reason}")
            raise typer.Exit(code=1)


@app.command("extract-content")
def extract_content_command(
    business_id: int = typer.Option(..., "--business-id"),
    as_json: bool = typer.Option(True, "--json/--no-json"),
) -> None:
    """Extract factual business content (AI when enabled, deterministic otherwise)."""
    from outreach.ai.base import get_ai_provider
    from outreach.models.business import Business
    from outreach.pipeline.build import extract_content_for
    from outreach.pipeline.context import RunContext

    settings = _settings()
    with session_scope() as session:
        business = session.get(Business, business_id)
        if business is None:
            console.print(f"[red]No business {business_id}[/red]")
            raise typer.Exit(code=1)
        ctx = RunContext(settings=settings, campaign_id=business.campaign_id)
        provider = None
        if settings.features.ai_content_extraction:
            provider = get_ai_provider(settings)
            if not provider.health_check().available:
                provider = None
        content, extras, _ = extract_content_for(session, ctx, business, provider=provider)
        payload = {"content": content.model_dump(), **extras}
    if as_json:
        _emit(payload, True)
    else:
        console.print(payload)


# ---------------------------------------------------------------------------
# builds
# ---------------------------------------------------------------------------
@app.command("build")
def build_command(
    business_id: int = typer.Option(..., "--business-id"),
    template: str = typer.Option("dentist", "--template"),
) -> None:
    """Build a redesign preview for one business."""
    from outreach.ai.base import get_ai_provider
    from outreach.models.business import Business
    from outreach.pipeline.build import build_for_business
    from outreach.pipeline.context import RunContext

    settings = _settings()
    if not settings.features.site_building:
        console.print(
            "[yellow]SKIPPED_FEATURE_DISABLED[/yellow]: FEATURE_SITE_BUILDING is off. "
            "Set FEATURE_SITE_BUILDING=true to write previews."
        )
        raise typer.Exit(code=2)
    with session_scope() as session:
        business = session.get(Business, business_id)
        if business is None:
            console.print(f"[red]No business {business_id}[/red]")
            raise typer.Exit(code=1)
        provider = None
        if settings.features.ai_content_extraction:
            provider = get_ai_provider(settings)
            if not provider.health_check().available:
                provider = None
        ctx = RunContext(settings=settings, campaign_id=business.campaign_id)
        outcome = build_for_business(
            session, ctx, business, provider=provider, browser=None, template_name=template
        )
        colour = "green" if outcome.status == "BUILT" else "red"
        console.print(f"[{colour}]{outcome.status}[/{colour}] {outcome.reason}")
        if outcome.build and outcome.build.preview_path:
            console.print(f"preview: {outcome.build.preview_path}/index.html")
        if outcome.build and outcome.build.qa_json:
            failures = outcome.build.qa_json.get("failures") or []
            for failure in failures:
                console.print(f"  [red]QA[/red] {failure}")


@app.command("build-candidates")
def build_candidates_command(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    limit: int = typer.Option(5, "--limit"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Build previews for the highest-priority candidates."""
    from outreach.pipeline.orchestrator import run_pipeline

    result = run_pipeline(
        campaign_id=campaign_id, limit=limit, dry_run=dry_run, stages={"build"}
    )
    _print_stages(result)


# ---------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------
@previews_app.command("serve")
def previews_serve(
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
) -> None:
    """Serve generated previews locally for review (loopback only)."""
    from outreach.previews.server import PreviewServer

    settings = _settings()
    server = PreviewServer(
        settings.preview_dir,
        host=host or settings.preview_server_host,
        port=port or settings.preview_server_port,
    )
    url = server.start()
    console.print(f"[green]Serving previews[/green] from {settings.preview_dir} at {url}")
    console.print("[dim]Local review only -- these URLs can never be emailed. Ctrl-C to stop.[/dim]")
    try:
        server._thread.join()  # type: ignore[union-attr]
    except KeyboardInterrupt:  # pragma: no cover - interactive
        server.stop()
        console.print("stopped")


@previews_app.command("list")
def previews_list() -> None:
    """List generated previews."""
    from outreach.previews.manager import PreviewManager

    manager = PreviewManager(_settings())
    entries = manager.list_previews()
    if not entries:
        console.print("[dim]No previews generated yet.[/dim]")
        return
    table = Table(title="Previews")
    for column in ("Business", "Path", "noindex", "Public URL"):
        table.add_column(column, overflow="fold")
    for business_id, path in entries:
        table.add_row(
            str(business_id), str(path),
            "[green]yes[/green]" if manager.verify_noindex(business_id) else "[red]NO[/red]",
            manager.public_url(business_id) or "-",
        )
    console.print(table)


@previews_app.command("publish")
def previews_publish(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Publish QA-passed previews through the configured publisher."""
    from outreach.pipeline.orchestrator import run_pipeline

    result = run_pipeline(campaign_id=campaign_id, dry_run=dry_run, stages={"publish"})
    _print_stages(result)


# ---------------------------------------------------------------------------
# gmail
# ---------------------------------------------------------------------------
@gmail_app.command("auth")
def gmail_auth(
    no_browser: bool = typer.Option(False, "--no-browser", help="Use the console flow."),
) -> None:
    """Run the Gmail OAuth flow and store the token outside git."""
    from outreach.messaging.gmail import GmailClient, GmailError

    settings = _settings()
    try:
        status = GmailClient(settings).authorize(open_browser=not no_browser)
    except GmailError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Authenticated[/green] as {status.email_address}")
    console.print(f"token: {status.token_path}")


@gmail_app.command("status")
def gmail_status(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show Gmail authentication status."""
    from outreach.messaging.gmail import GmailClient

    status = GmailClient(_settings()).status()
    if as_json:
        _emit(status.as_dict(), True)
        return
    colour = "green" if status.authenticated else "yellow"
    console.print(
        Panel(
            f"authenticated: [{colour}]{status.authenticated}[/{colour}]\n"
            f"address: {status.email_address or '-'}\n"
            f"reason: {status.reason}\n"
            f"token: {status.token_path}",
            title="Gmail",
        )
    )


@gmail_app.command("test")
def gmail_test(
    to: str = typer.Option(..., "--to", help="Send a single test message to this address."),
    confirm: bool = typer.Option(False, "--confirm", help="Required; nothing is sent without it."),
) -> None:
    """Send one real test email.  Requires --confirm and every send flag."""
    from outreach.messaging.compliance import recipient_problem
    from outreach.messaging.gmail import GmailClient, new_idempotency_key
    from outreach.state import is_paused

    settings = _settings()
    if not confirm:
        console.print("[yellow]Refusing to send without --confirm.[/yellow]")
        raise typer.Exit(code=2)
    if not (settings.features.gmail_send and settings.outreach_send_enabled):
        console.print(
            "[red]Refusing:[/red] FEATURE_GMAIL_SEND and OUTREACH_SEND_ENABLED must both be true."
        )
        raise typer.Exit(code=2)
    problem = recipient_problem(to)
    if problem:
        console.print(f"[red]Refusing:[/red] {problem}")
        raise typer.Exit(code=2)
    with session_scope() as session:
        if is_paused(session):
            console.print("[red]Refusing:[/red] the system is globally paused.")
            raise typer.Exit(code=2)
    client = GmailClient(settings)
    status = client.status()
    if not status.authenticated:
        console.print(f"[red]Refusing:[/red] {status.reason}")
        raise typer.Exit(code=2)
    receipt = client.send(
        to_email=to,
        subject="StoopCraft outreach system test",
        body_text=(
            "This is a configuration test from the StoopCraft outreach system.\n\n"
            f"{settings.sender_name}\n{settings.sender_company}\n"
        ),
        body_html=None,
        idempotency_key=new_idempotency_key(),
        campaign_label="gmail-test",
        dry_run=False,
    )
    if receipt.sent:
        console.print(f"[green]Sent[/green] message {receipt.message_id}")
    else:
        console.print(f"[red]Failed[/red]: {receipt.error} (ambiguous={receipt.ambiguous})")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# queue / send / replies
# ---------------------------------------------------------------------------
@app.command("queue")
def queue_command(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show the send queue."""
    from sqlalchemy import select

    from outreach.messaging.queue import queue_snapshot
    from outreach.models.outreach_message import OutreachMessage

    with session_scope() as session:
        snapshot = queue_snapshot(session)
        messages = list(
            session.scalars(
                select(OutreachMessage).order_by(OutreachMessage.id.desc()).limit(40)
            ).all()
        )
        rows = [
            {
                "id": m.id,
                "business_id": m.business_id,
                "recipient": m.recipient_email,
                "status": m.status,
                "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
                "sent_at": m.sent_at.isoformat() if m.sent_at else None,
                "subject": m.subject,
            }
            for m in messages
        ]
    if as_json:
        _emit({"snapshot": snapshot, "messages": rows}, True)
        return
    console.print(
        Panel(
            "  ".join(f"[bold]{k}[/bold]={v}" for k, v in sorted(snapshot.items())) or "empty",
            title="Queue",
        )
    )
    if rows:
        table = Table()
        for column in ("ID", "Business", "Recipient", "Status", "Scheduled", "Subject"):
            table.add_column(column, overflow="fold")
        for row in rows:
            scheduled = str(row["scheduled_at"] or "-")
            table.add_row(
                str(row["id"]),
                str(row["business_id"]),
                str(row["recipient"]),
                str(row["status"]),
                scheduled[:16],
                str(row["subject"]),
            )
        console.print(table)


@app.command("process-queue")
def process_queue_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="Never sends, whatever the flags say."),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Attempt to send queued messages, obeying every guard."""
    from outreach.messaging.queue import process_queue
    from outreach.messaging.scheduler import QUEUE_LOCK
    from outreach.state import job_lock

    settings = _settings()
    with job_lock(QUEUE_LOCK, required=False) as acquired:
        if not acquired:
            console.print("[yellow]Another process-queue run is in progress; exiting.[/yellow]")
            raise typer.Exit(code=0)
        with session_scope() as session:
            result = process_queue(session, settings, dry_run=dry_run, limit=limit)
    console.print(
        Panel(
            f"considered {result.considered}  sent {result.sent}  deferred {result.deferred}  "
            f"held/skipped {result.skipped}  failed {result.failed}  "
            f"needs review {result.needs_review}",
            title="DRY RUN -- nothing was sent" if dry_run else "Queue processed",
        )
    )
    for detail in result.details[:20]:
        console.print(f"  [dim]{detail['action']}[/dim] message {detail['message_id']}: {detail['reason']}")


@app.command("sync-replies")
def sync_replies_command(
    lookback_days: int = typer.Option(45, "--lookback-days"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Poll Gmail for replies and opt-outs."""
    from outreach.ai.base import get_ai_provider
    from outreach.messaging.replies import sync_replies
    from outreach.messaging.scheduler import REPLY_LOCK
    from outreach.state import job_lock

    settings = _settings()
    provider = None
    if settings.features.ai_scoring:
        candidate = get_ai_provider(settings)
        if candidate.health_check().available:
            provider = candidate
    with job_lock(REPLY_LOCK, required=False) as acquired:
        if not acquired:
            console.print("[yellow]Another sync-replies run is in progress; exiting.[/yellow]")
            raise typer.Exit(code=0)
        with session_scope() as session:
            result = sync_replies(
                session, settings, provider=provider, lookback_days=lookback_days
            )
    payload = {
        "checked": result.checked,
        "replies_found": result.replies_found,
        "opt_outs": result.opt_outs,
        "positive": result.positive,
        "negative": result.negative,
        "bounced": result.bounced,
        "other": result.other,
        "skipped_reason": result.skipped_reason,
    }
    if as_json:
        _emit(payload, True)
        return
    if result.skipped_reason:
        console.print(f"[yellow]{result.skipped_reason}[/yellow]")
        return
    console.print(
        Panel(
            f"checked {result.checked}  replies {result.replies_found}  "
            f"opt-outs {result.opt_outs}  positive {result.positive}  "
            f"negative {result.negative}  bounced {result.bounced}",
            title="Reply sync",
        )
    )


# ---------------------------------------------------------------------------
# suppression
# ---------------------------------------------------------------------------
@suppress_app.command("add")
def suppress_add(
    email: str = typer.Argument(None, help="Email address to suppress."),
    domain: str = typer.Option(None, "--domain", help="Suppress an entire domain."),
    reason: str = typer.Option("requested", "--reason"),
    notes: str = typer.Option(None, "--notes"),
) -> None:
    """Add a permanent do-not-contact entry."""
    from outreach.messaging.suppression import add_suppression

    if not email and not domain:
        console.print("[red]Provide an email address or --domain.[/red]")
        raise typer.Exit(code=2)
    with session_scope() as session:
        entry = add_suppression(
            session, email=email, domain=domain, reason=reason, source="cli", notes=notes
        )
        target = entry.normalized_email or f"@{entry.normalized_domain}" if entry else "?"
    console.print(f"[green]Suppressed[/green] {target} ({reason})")


@suppress_app.command("list")
def suppress_list(limit: int = typer.Option(100, "--limit")) -> None:
    """List suppression entries."""
    from outreach.messaging.suppression import list_suppressions

    with session_scope() as session:
        entries = list_suppressions(session, limit=limit)
        rows = [
            (
                str(e.id), e.normalized_email or "-", e.normalized_domain or "-",
                e.reason, e.source or "-", e.created_at.strftime("%Y-%m-%d"),
            )
            for e in entries
        ]
    if not rows:
        console.print("[dim]Suppression list is empty.[/dim]")
        return
    table = Table(title="Suppression list")
    for column in ("ID", "Email", "Domain", "Reason", "Source", "Added"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)


@suppress_app.command("remove")
def suppress_remove(
    email: str = typer.Argument(None),
    domain: str = typer.Option(None, "--domain"),
    confirm: bool = typer.Option(False, "--confirm", help="Required: removal is deliberate."),
) -> None:
    """Remove a suppression entry.  Requires an explicit --confirm."""
    from outreach.messaging.suppression import remove_suppression

    if not email and not domain:
        console.print("[red]Provide an email address or --domain.[/red]")
        raise typer.Exit(code=2)
    if not confirm:
        console.print(
            "[yellow]Refusing without --confirm.[/yellow] Suppression is permanent by design; "
            "removing an entry means this address can be contacted again."
        )
        raise typer.Exit(code=2)
    with session_scope() as session:
        removed = remove_suppression(session, email=email, domain=domain)
    console.print(f"[green]Removed {removed} entry(ies).[/green]")


# ---------------------------------------------------------------------------
# pause / resume / status
# ---------------------------------------------------------------------------
@app.command("pause")
def pause_command(reason: str = typer.Option("operator requested", "--reason")) -> None:
    """Globally pause all external side effects."""
    from outreach.state import set_pause

    with session_scope() as session:
        state = set_pause(session, True, reason)
    console.print(f"[yellow]{state.summary}[/yellow]")


@app.command("resume")
def resume_command() -> None:
    """Resume after a pause."""
    from outreach.state import set_pause

    with session_scope() as session:
        state = set_pause(session, False, "operator resumed")
    console.print(f"[green]{state.summary}[/green]")


@app.command("status")
def status_command(as_json: bool = typer.Option(False, "--json")) -> None:
    """Pause state, locks and queue health."""
    from outreach.messaging.queue import queue_snapshot
    from outreach.state import get_pause_state, list_locks

    settings = _settings()
    with session_scope() as session:
        pause = get_pause_state(session)
        locks = [
            {"name": lock.name, "holder": lock.holder, "expires_at": lock.expires_at.isoformat()}
            for lock in list_locks(session)
        ]
        snapshot = queue_snapshot(session)
    payload = {
        "paused": pause.paused,
        "pause_summary": pause.summary,
        "locks": locks,
        "queue": snapshot,
        "features": settings.features.as_env_dict(),
        "send_enabled": settings.outreach_send_enabled,
    }
    if as_json:
        _emit(payload, True)
        return
    console.print(
        Panel(
            f"{'[yellow]PAUSED[/yellow]' if pause.paused else '[green]RUNNING[/green]'}  "
            f"{pause.summary}\n"
            f"queue: {snapshot or 'empty'}\n"
            f"locks: {locks or 'none'}",
            title="System status",
        )
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
@app.command("stats")
def stats_command(
    campaign_id: int = typer.Option(None, "--campaign-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Full funnel report."""
    from outreach.reporting import build_funnel_report

    settings = _settings()
    with session_scope() as session:
        report = build_funnel_report(session, settings, campaign_id)
    if as_json:
        _emit(report.to_dict(), True)
        return
    console.print(Panel("\n".join(report.funnel_lines()), title="Funnel"))
    table = Table(title="All counters")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    for key in sorted(report.counts):
        if report.counts[key]:
            table.add_row(key, str(report.counts[key]))
    console.print(table)


@app.command("today")
def today_command(as_json: bool = typer.Option(False, "--json")) -> None:
    """Sending capacity and queue state right now."""
    from outreach.reporting import today_report

    settings = _settings()
    with session_scope() as session:
        payload = today_report(session, settings)
    if as_json:
        _emit(payload, True)
        return
    console.print(
        Panel(
            f"local time: {payload['local_time']} ({payload['timezone']})\n"
            f"paused: {payload['paused']}  |  sending enabled: {payload['sending_enabled']}\n"
            f"in send window: {payload['in_send_window']} -- {payload['send_window_note']}\n"
            f"next window opens: {payload['next_window_open']}\n"
            f"sent today: {payload['sent_today']}/{payload['daily_limit']} "
            f"(hard max {payload['hard_max']}), remaining {payload['remaining_today']}\n"
            f"pending: {payload['pending']}",
            title="Today",
        )
    )


@app.command("failures")
def failures_command(
    limit: int = typer.Option(25, "--limit"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Everything that failed and might need a human."""
    from outreach.reporting import failure_report

    with session_scope() as session:
        payload = failure_report(session, limit=limit)
    if as_json:
        _emit(payload, True)
        return
    for name, rows in payload.items():
        if not rows:
            continue
        table = Table(title=name.replace("_", " "))
        for column in rows[0]:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*[str(v) for v in row.values()])
        console.print(table)
    if not any(payload.values()):
        console.print("[green]No failures recorded.[/green]")


@app.command("provider-usage")
def provider_usage_command(as_json: bool = typer.Option(False, "--json")) -> None:
    """Monthly metered-provider usage, including blocked attempts."""
    from outreach.quota import GOOGLE_PLACES, month_key, monthly_usage, usage_report

    settings = _settings()
    with session_scope() as session:
        rows = usage_report(session)
        google_used = monthly_usage(session, GOOGLE_PLACES)
    limit = settings.effective_google_places_monthly_limit
    payload = {
        "month": month_key(),
        "google_places": {
            "used": google_used,
            "limit": limit,
            "remaining": max(0, limit - google_used),
            "feature_enabled": settings.features.real_discovery,
            "credential_configured": bool(settings.google_maps_api_key),
        },
        "providers": rows,
    }
    if as_json:
        _emit(payload, True)
        return
    console.print(
        Panel(
            f"Google Places this month: [bold]{google_used}[/bold]/{limit} "
            f"(remaining {max(0, limit - google_used)})\n"
            f"FEATURE_REAL_DISCOVERY: {settings.features.real_discovery}   "
            f"API key configured: {bool(settings.google_maps_api_key)}",
            title=f"Provider usage {month_key()}",
        )
    )
    if rows:
        table = Table()
        for column in ("Provider", "Billable units", "By status"):
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(row["provider"], str(row["billable_units"]), str(row["by_status"]))
        console.print(table)


@app.command("ai-usage")
def ai_usage_command(
    days: int = typer.Option(30, "--days"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """AI invocation accounting."""
    from outreach.reporting import ai_usage_report

    with session_scope() as session:
        payload = ai_usage_report(session, days=days)
    if as_json:
        _emit(payload, True)
        return
    console.print(f"[bold]{payload['total_calls']}[/bold] AI calls in the last {days} days")
    if payload["by_purpose"]:
        table = Table()
        for column in ("Purpose", "Provider", "Calls", "OK", "Failed", "Avg ms"):
            table.add_column(column)
        for row in payload["by_purpose"]:
            table.add_row(
                row["purpose"], row["provider"] or "-", str(row["calls"]),
                str(row["successful"]), str(row["failed"]), str(row["avg_duration_ms"]),
            )
        console.print(table)


@app.command("learning-data")
def learning_data_command(
    limit: int = typer.Option(500, "--limit"),
    out: Path = typer.Option(None, "--out", help="Write JSON to this path."),
) -> None:
    """Export the structured problem/outcome dataset (no ML, just the data)."""
    from outreach.reporting import learning_dataset

    with session_scope() as session:
        rows = learning_dataset(session, limit=limit)
    payload = json.dumps(rows, indent=2, default=str)
    if out:
        Path(out).write_text(payload, encoding="utf-8")
        console.print(f"[green]Wrote {len(rows)} rows[/green] to {out}")
    else:
        console.print(payload)


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------
@scheduler_app.command("status")
def scheduler_status(as_json: bool = typer.Option(False, "--json")) -> None:
    """What the scheduler thinks is due right now."""
    from outreach.messaging.scheduler import evaluate_schedule

    settings = _settings()
    with session_scope() as session:
        decision = evaluate_schedule(session, settings)
    if as_json:
        _emit(decision.to_dict(), True)
        return
    console.print(Panel(json.dumps(decision.to_dict(), indent=2), title="Scheduler"))


@scheduler_app.command("tick")
def scheduler_tick(
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """One scheduler tick, intended for cron / systemd timers."""
    from outreach.messaging.queue import process_queue
    from outreach.messaging.replies import sync_replies
    from outreach.messaging.scheduler import QUEUE_LOCK, evaluate_schedule
    from outreach.state import job_lock

    settings = _settings()
    with session_scope() as session:
        decision = evaluate_schedule(session, settings)
    if not decision.enabled:
        console.print("[yellow]SKIPPED_FEATURE_DISABLED[/yellow]: FEATURE_SCHEDULER is off")
        raise typer.Exit(code=0)
    for reason in decision.reasons:
        console.print(f"  [dim]{reason}[/dim]")

    if decision.should_process_queue:
        with job_lock(QUEUE_LOCK, required=False) as acquired:
            if acquired:
                with session_scope() as session:
                    result = process_queue(session, settings, dry_run=dry_run)
                console.print(f"queue: sent={result.sent} deferred={result.deferred}")
            else:
                console.print("[yellow]queue lock held elsewhere; skipping[/yellow]")
    if decision.should_sync_replies and not dry_run:
        with session_scope() as session:
            replies = sync_replies(session, settings)
        console.print(f"replies: found={replies.replies_found} opt-outs={replies.opt_outs}")


@scheduler_app.command("install-help")
def scheduler_install_help() -> None:
    """Print recommended cron and systemd configuration (never installs it)."""
    from outreach.messaging.scheduler import CRON_EXAMPLES, SYSTEMD_EXAMPLES

    console.print(CRON_EXAMPLES)
    console.print(SYSTEMD_EXAMPLES)


# ---------------------------------------------------------------------------
# the whole pipeline
# ---------------------------------------------------------------------------
@app.command("run")
def run_command(
    vertical: str = typer.Option("dentist", "--vertical"),
    city: str = typer.Option("Phoenix", "--city"),
    state: str = typer.Option("AZ", "--state"),
    country: str = typer.Option("US", "--country"),
    campaign_id: int = typer.Option(None, "--campaign-id"),
    provider: str = typer.Option("fixtures", "--provider", help="fixtures | google"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="No email is ever sent and nothing is published."
    ),
    limit: int = typer.Option(None, "--limit"),
    force: bool = typer.Option(False, "--force", help="Re-scan and re-score existing rows."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run the full pipeline end to end."""
    from outreach.pipeline.orchestrator import run_pipeline

    result = run_pipeline(
        vertical=vertical, city=city, state=state, country=country,
        campaign_id=campaign_id, provider=provider, dry_run=dry_run,
        limit=limit, force=force,
    )
    if as_json:
        _emit(result.to_dict(), True)
        return
    if dry_run:
        console.print(
            Panel(
                "[bold]DRY RUN[/bold] -- no email can be sent and nothing is published, "
                "regardless of feature flags.",
                style="yellow",
            )
        )
    _print_stages(result)
    if result.aborted_reason:
        console.print(f"[yellow]{result.aborted_reason}[/yellow]")
        raise typer.Exit(code=0)
    console.print(
        f"\ncampaign [bold]{result.campaign_id}[/bold] finished in "
        f"{result.duration_seconds:.1f}s -- run "
        f"[cyan]python -m outreach stats --campaign-id {result.campaign_id}[/cyan] for the funnel."
    )


def _print_stages(result: Any) -> None:
    table = Table(title="Pipeline stages")
    for column in ("Stage", "Executed", "Counts", "Notes"):
        table.add_column(column, overflow="fold")
    for stage in result.stages:
        executed = "[green]yes[/green]" if stage.executed else "[dim]skipped[/dim]"
        notes = stage.skipped_reason or "; ".join([*stage.notes, *stage.errors])[:220]
        counts = "  ".join(f"{k}={v}" for k, v in sorted(stage.counts.items())) or "-"
        table.add_row(stage.name, executed, counts, notes or "")
    console.print(table)


def main() -> None:  # pragma: no cover - console script entry
    reset_settings_cache()
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())

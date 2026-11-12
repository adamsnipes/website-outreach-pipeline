"""The end-to-end pipeline.

Ordinary Python owns orchestration: no agent framework, no message broker, no
scheduler daemon.  Each stage is bounded, restartable and idempotent, and each
stage obeys its own feature flag.  A disabled stage logs
``SKIPPED_FEATURE_DISABLED`` and the run continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from outreach.config import Settings, get_settings
from outreach.db import session_scope, utcnow
from outreach.events import record_event
from outreach.fixtures.server import FixtureSiteServer
from outreach.logging import get_logger
from outreach.messaging.scheduler import PIPELINE_LOCK
from outreach.models.enums import EventType
from outreach.pipeline.build import run_build, run_publish
from outreach.pipeline.context import RunContext, StageResult
from outreach.pipeline.discover import run_discovery
from outreach.pipeline.qualify import run_ai_scoring, run_qualification
from outreach.pipeline.queue import run_compose_and_queue
from outreach.pipeline.scan import run_scan
from outreach.pipeline.send import run_send
from outreach.state import LockUnavailable, is_paused, job_lock

log = get_logger(__name__)


@dataclass
class PipelineResult:
    campaign_id: int | None
    dry_run: bool
    stages: list[StageResult] = field(default_factory=list)
    started_at: Any = None
    finished_at: Any = None
    aborted_reason: str | None = None

    @property
    def duration_seconds(self) -> float:
        if not (self.started_at and self.finished_at):
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "dry_run": self.dry_run,
            "aborted_reason": self.aborted_reason,
            "duration_seconds": round(self.duration_seconds, 2),
            "stages": [s.to_dict() for s in self.stages],
        }


def run_pipeline(
    *,
    settings: Settings | None = None,
    vertical: str = "dentist",
    city: str = "",
    state: str = "",
    country: str = "US",
    campaign_id: int | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    force: bool = False,
    provider: str = "fixtures",
    stages: set[str] | None = None,
    use_lock: bool = True,
) -> PipelineResult:
    """Run the whole pipeline.

    ``dry_run`` is threaded into every stage and re-checked at the lowest level
    of every external side effect; it can never be overridden by a feature flag.
    """
    settings = settings or get_settings()
    wanted = stages or {
        "discover", "scan", "ai_scoring", "qualify", "build", "publish",
        "compose_queue", "send",
    }
    ctx = RunContext(
        settings=settings,
        campaign_id=campaign_id,
        vertical=vertical,
        city=city,
        state=state,
        country=country,
        dry_run=dry_run,
        limit=limit,
        force=force,
        provider_name=provider,
    )
    result = PipelineResult(campaign_id=campaign_id, dry_run=dry_run, started_at=utcnow())

    fixture_server: FixtureSiteServer | None = None
    if provider in {"fixtures", "fixture"}:
        fixture_server = FixtureSiteServer(
            host=settings.fixture_server_host, port=settings.fixture_server_port
        )
        ctx.fixture_base_url = fixture_server.start()
        log.info("fixture site server ready", extra={"base_url": ctx.fixture_base_url})

    lock_ctx = job_lock(PIPELINE_LOCK, required=False) if use_lock else _no_lock()
    try:
        with lock_ctx as acquired:
            if use_lock and not acquired:
                result.aborted_reason = (
                    "another pipeline run holds the job lock; exiting without doing work"
                )
                log.warning(result.aborted_reason)
                result.finished_at = utcnow()
                return result

            with session_scope(settings) as session:
                record_event(
                    session, "pipeline", None, EventType.PIPELINE_STARTED,
                    {
                        "vertical": vertical, "city": city, "state": state,
                        "dry_run": dry_run, "provider": provider,
                        "features": settings.features.as_env_dict(),
                    },
                )
                paused = is_paused(session)
                if paused:
                    log.warning("system is paused: external side effects are disabled")

                if "discover" in wanted:
                    ctx.add(run_discovery(session, ctx))
                    session.commit()
                if "scan" in wanted:
                    ctx.add(run_scan(session, ctx))
                if "qualify" in wanted:
                    ctx.add(run_qualification(session, ctx))
                if "ai_scoring" in wanted:
                    ctx.add(run_ai_scoring(session, ctx))
                    # Re-qualify so AI scores fold into the combined score.
                    if ctx.settings.features.ai_scoring:
                        ctx.add(run_qualification(session, ctx))
                if "build" in wanted:
                    ctx.add(run_build(session, ctx))
                if "publish" in wanted:
                    ctx.add(run_publish(session, ctx))
                if "compose_queue" in wanted:
                    ctx.add(run_compose_and_queue(session, ctx))
                if "send" in wanted:
                    ctx.add(run_send(session, ctx))

                result.stages = ctx.stages
                result.campaign_id = ctx.campaign_id
                record_event(
                    session, "pipeline", ctx.campaign_id, EventType.PIPELINE_FINISHED,
                    {"dry_run": dry_run, "stages": [s.to_dict() for s in ctx.stages]},
                )
    except LockUnavailable as exc:  # pragma: no cover - required=False never raises
        result.aborted_reason = str(exc)
    finally:
        if fixture_server is not None:
            fixture_server.stop()
        result.finished_at = utcnow()

    log.info(
        "pipeline finished",
        extra={
            "campaign_id": result.campaign_id,
            "dry_run": dry_run,
            "seconds": round(result.duration_seconds, 1),
        },
    )
    return result


class _no_lock:
    """Null context manager used when locking is disabled (tests)."""

    def __enter__(self) -> bool:
        return True

    def __exit__(self, *exc: Any) -> None:
        return None

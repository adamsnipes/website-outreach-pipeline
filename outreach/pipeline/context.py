"""Shared run context and stage bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from outreach.config import Settings
from outreach.logging import get_logger

log = get_logger(__name__)

SKIPPED_FEATURE_DISABLED = "SKIPPED_FEATURE_DISABLED"


@dataclass
class StageResult:
    """Outcome of one pipeline stage."""

    name: str
    executed: bool = True
    skipped_reason: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def skipped(cls, name: str, reason: str) -> StageResult:
        log.info("%s: %s", SKIPPED_FEATURE_DISABLED, reason, extra={"stage": name})
        return cls(name=name, executed=False, skipped_reason=reason)

    def bump(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "executed": self.executed,
            "skipped_reason": self.skipped_reason,
            "counts": self.counts,
            "notes": self.notes[:40],
            "errors": self.errors[:40],
        }


@dataclass
class RunContext:
    """Everything a stage needs to know about the current run.

    ``dry_run`` is sacred: it is threaded through every stage and re-checked at
    the lowest level of every external side effect.
    """

    settings: Settings
    campaign_id: int | None = None
    vertical: str = "dentist"
    city: str = ""
    state: str = ""
    country: str = "US"
    dry_run: bool = False
    limit: int | None = None
    force: bool = False
    provider_name: str = "fixtures"
    fixture_base_url: str | None = None
    stages: list[StageResult] = field(default_factory=list)

    def add(self, stage: StageResult) -> StageResult:
        self.stages.append(stage)
        return stage

    def effective_limit(self, configured: int) -> int:
        return min(configured, self.limit) if self.limit else configured

    def feature(self, name: str) -> bool:
        return self.settings.features.is_enabled(name)

    def require_feature(self, stage_name: str, flag: str) -> StageResult | None:
        """``None`` when the flag is on; a skipped StageResult when it is off."""
        if self.feature(flag):
            return None
        return StageResult.skipped(
            stage_name, f"{SKIPPED_FEATURE_DISABLED}: {flag} is disabled"
        )

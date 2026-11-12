"""AI provider interface + usage accounting.

Providers are swappable.  The V1 provider drives the locally authenticated
Claude Code CLI, so no Anthropic API key is required; a Codex CLI provider can
be added by subclassing :class:`SubprocessAIProvider`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from outreach.config import Settings
from outreach.db import utcnow
from outreach.logging import get_logger
from outreach.models.ai_usage import AIUsage
from outreach.models.enums import AIPurpose

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class AIError(RuntimeError):
    """Base class for AI failures."""


class AIUnavailable(AIError):
    """The provider is not installed, not authenticated, or feature-disabled."""


class AITimeout(AIError):
    """The provider did not answer within the configured timeout."""


class AIValidationError(AIError):
    """The provider answered, but not with output matching the schema."""


@dataclass
class AIResult:
    """One provider invocation."""

    success: bool
    purpose: str
    provider: str
    model: str | None = None
    raw_output: str = ""
    parsed: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    retry_count: int = 0
    prompt_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a provider health check."""

    available: bool
    reason: str
    executable: str | None = None
    version: str | None = None
    supports_non_interactive: bool = False
    supports_json_output: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "executable": self.executable,
            "version": self.version,
            "supports_non_interactive": self.supports_non_interactive,
            "supports_json_output": self.supports_json_output,
            **self.details,
        }


def record_ai_usage(
    session: Session,
    result: AIResult,
    business_id: int | None = None,
    started_at: Any = None,
) -> AIUsage:
    """Persist the audit row for an AI invocation."""
    completed = utcnow()
    usage = AIUsage(
        business_id=business_id,
        purpose=str(result.purpose),
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        started_at=started_at or completed,
        completed_at=completed,
        success=result.success,
        duration_ms=result.duration_ms,
        retry_count=result.retry_count,
        metadata_json=result.metadata or None,
        error=result.error,
    )
    session.add(usage)
    return usage


class AIProvider(ABC):
    """Every AI backend implements this surface."""

    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- capability --------------------------------------------------
    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """Whether this provider can actually run, without doing real work."""

    # -- bounded checkpoints ------------------------------------------
    @abstractmethod
    def score_website(
        self,
        *,
        vertical: str,
        business_name: str,
        technical_findings: dict[str, Any],
        page_text: str,
        desktop_screenshot: str | None,
        mobile_screenshot: str | None,
    ) -> AIResult:
        """Visual/UX scoring of a rendered website."""

    @abstractmethod
    def extract_business_content(
        self,
        *,
        vertical: str,
        business_name: str,
        deterministic_facts: dict[str, Any],
        evidence: dict[str, Any],
        page_text: str,
    ) -> AIResult:
        """Structure factual business information from crawled evidence."""

    @abstractmethod
    def generate_site_content(
        self, *, vertical: str, business_content: dict[str, Any]
    ) -> AIResult:
        """Write template copy from already-validated facts."""

    @abstractmethod
    def compose_email(self, **kwargs: Any) -> AIResult:
        """Personalise one outreach email."""

    @abstractmethod
    def classify_reply(self, *, reply_text: str) -> AIResult:
        """Classify an inbound reply (never respond to it)."""

    # -- helpers -------------------------------------------------------
    @staticmethod
    def validate(result: AIResult, model: type[T]) -> T:
        """Validate an :class:`AIResult` payload against a schema."""
        if not result.success or result.parsed is None:
            raise AIValidationError(result.error or "provider returned no parsed output")
        try:
            return model.model_validate(result.parsed)
        except ValidationError as exc:
            raise AIValidationError(f"output failed schema validation: {exc}") from exc


class NullAIProvider(AIProvider):
    """A provider that always refuses.  Used when AI is disabled or missing."""

    name = "null"

    def __init__(self, settings: Settings, reason: str = "AI provider disabled") -> None:
        super().__init__(settings)
        self.reason = reason

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(False, self.reason)

    def _refuse(self, purpose: str) -> AIResult:
        return AIResult(
            success=False, purpose=purpose, provider=self.name, error=self.reason
        )

    def score_website(self, **kwargs: Any) -> AIResult:
        return self._refuse(AIPurpose.SCORE_WEBSITE)

    def extract_business_content(self, **kwargs: Any) -> AIResult:
        return self._refuse(AIPurpose.EXTRACT_BUSINESS_CONTENT)

    def generate_site_content(self, **kwargs: Any) -> AIResult:
        return self._refuse(AIPurpose.GENERATE_SITE_CONTENT)

    def compose_email(self, **kwargs: Any) -> AIResult:
        return self._refuse(AIPurpose.COMPOSE_EMAIL)

    def classify_reply(self, **kwargs: Any) -> AIResult:
        return self._refuse(AIPurpose.CLASSIFY_REPLY)


def get_ai_provider(settings: Settings, name: str | None = None) -> AIProvider:
    """Resolve the configured AI provider."""
    from outreach.ai.claude_cli import ClaudeCodeProvider
    from outreach.ai.codex_cli import CodexCLIProvider

    key = (name or settings.ai_provider or "claude_cli").strip().lower()
    if key in {"claude", "claude_cli", "claude-code"}:
        return ClaudeCodeProvider(settings)
    if key in {"codex", "codex_cli"}:
        return CodexCLIProvider(settings)
    if key in {"null", "none", "disabled"}:
        return NullAIProvider(settings)
    raise KeyError(f"Unknown AI provider {name!r}")


class _Stopwatch:
    """Small helper so every provider reports duration the same way."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.started_at = utcnow()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

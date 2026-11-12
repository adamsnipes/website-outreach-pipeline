"""Bounded AI checkpoints.

Normal Python code owns orchestration.  AI is called only at a handful of
well-defined checkpoints, always with a validated output schema, always with
untrusted website content clearly fenced off as data.
"""

from outreach.ai.base import (
    AIError,
    AIProvider,
    AIResult,
    AITimeout,
    AIUnavailable,
    AIValidationError,
    ProviderHealth,
    get_ai_provider,
)

__all__ = [
    "AIError",
    "AIProvider",
    "AIResult",
    "AITimeout",
    "AIUnavailable",
    "AIValidationError",
    "ProviderHealth",
    "get_ai_provider",
]

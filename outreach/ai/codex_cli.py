"""Codex CLI provider.

A thin subclass of the same sandboxed subprocess machinery, provided so the AI
provider abstraction is demonstrably swappable.  Enable with
``AI_PROVIDER=codex_cli``; it health-checks exactly like the Claude provider
and stays unavailable until the CLI is installed.
"""

from __future__ import annotations

from typing import Any

from outreach.ai.base import ProviderHealth
from outreach.ai.claude_cli import (
    ClaudeCodeProvider,
    CliCapabilities,
    _probe_cli,
)


class CodexCLIProvider(ClaudeCodeProvider):
    """Drives a Codex-style CLI in non-interactive mode."""

    name = "codex_cli"

    @property
    def executable(self) -> str:
        return self.settings.codex_executable

    def _build_argv(self, resolved: str, caps: CliCapabilities, allow_read: bool) -> list[str]:
        # Codex takes the prompt on stdin in exec/non-interactive mode.
        argv: list[str] = [resolved]
        if "exec" in (caps.help_text or ""):
            argv.append("exec")
        elif caps.print_flag:
            argv.append(caps.print_flag)
        if caps.output_format:
            argv += ["--output-format", "json"]
        return argv

    def health_check(self) -> ProviderHealth:
        resolved, version, caps = _probe_cli(self.executable)
        if resolved is None:
            return ProviderHealth(
                False,
                f"{self.executable!r} was not found on PATH. Install the Codex CLI and "
                "authenticate it, or leave AI_PROVIDER=claude_cli.",
            )
        return ProviderHealth(
            bool(caps.usable or "exec" in (caps.help_text or "")),
            "codex CLI is installed",
            executable=resolved,
            version=version,
            supports_non_interactive=True,
            supports_json_output=caps.output_format,
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<CodexCLIProvider exe={self.executable!r}>"

    def _extra(self) -> dict[str, Any]:  # pragma: no cover - reserved hook
        return {}

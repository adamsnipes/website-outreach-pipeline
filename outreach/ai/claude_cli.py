"""Claude Code CLI provider.

Uses the locally authenticated ``claude`` CLI in non-interactive mode, so no
Anthropic API key is needed.

Safety properties, all enforced here rather than trusted to the prompt:

* ``shell=False`` always; the prompt travels on **stdin** and is never
  interpolated into a command line
* the process runs in a fresh temporary directory containing only the files it
  needs -- never the project repository
* the environment is scrubbed: no project secrets, no ``GOOGLE_MAPS_API_KEY``,
  no Gmail credentials, no ``DATABASE_URL``
* tools are denied by default; scoring jobs get read-only file access at most,
  never Bash, edit/write, network fetch or task spawning
* hard process timeout and a bounded retry count
* output is parsed defensively and validated against a pydantic schema
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from outreach.ai.base import AIProvider, AIResult, ProviderHealth, _Stopwatch
from outreach.ai.prompts import (
    CLASSIFY_REPLY_PROMPT_VERSION,
    COMPOSE_EMAIL_PROMPT_VERSION,
    EXTRACT_CONTENT_PROMPT_VERSION,
    GENERATE_SITE_CONTENT_PROMPT_VERSION,
    SCORE_WEBSITE_PROMPT_VERSION,
    build_classify_reply_prompt,
    build_compose_email_prompt,
    build_extract_content_prompt,
    build_generate_site_content_prompt,
    build_score_website_prompt,
)
from outreach.ai.schemas import JSONExtractionError, extract_json_object
from outreach.config import Settings
from outreach.logging import get_logger
from outreach.models.enums import AIPurpose

log = get_logger(__name__)

#: Tools that must never be granted to an analysis job, in the CLI's own names.
DENIED_TOOLS = (
    "Bash", "Edit", "Write", "NotebookEdit", "MultiEdit", "WebFetch",
    "WebSearch", "Task", "Agent", "TodoWrite", "KillShell", "BashOutput",
)

#: Environment variables that must never reach the model subprocess.
SECRET_ENV_PREFIXES = (
    "GOOGLE_", "GMAIL_", "DATABASE_", "AWS_", "OPENAI_", "SMTP_", "SENDER_",
    "PREVIEW_", "OUTREACH_", "FEATURE_", "CLAUDE_", "AI_", "CODEX_",
)
#: ...except the handful the CLI itself needs to find its own auth and config.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ", "TERM")


@dataclass(frozen=True)
class CliCapabilities:
    """Which flags the installed CLI actually understands."""

    print_flag: str | None
    output_format: bool
    allowed_tools_flag: str | None
    disallowed_tools_flag: str | None
    permission_mode: bool
    model_flag: bool
    add_dir: bool
    help_text: str = ""

    @property
    def usable(self) -> bool:
        return self.print_flag is not None


@lru_cache(maxsize=8)
def _probe_cli(executable: str) -> tuple[str | None, str | None, CliCapabilities]:
    """``(resolved path, version, capabilities)`` for ``executable``."""
    resolved = shutil.which(executable) or (executable if Path(executable).is_file() else None)
    if resolved is None:
        return None, None, CliCapabilities(None, False, None, None, False, False, False)

    version: str | None = None
    try:
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        version = (proc.stdout or proc.stderr or "").strip()[:120] or None
    except (OSError, subprocess.SubprocessError):
        version = None

    help_text = ""
    try:
        proc = subprocess.run(
            [resolved, "--help"], capture_output=True, text=True, timeout=30, check=False
        )
        help_text = (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError):
        help_text = ""

    def has(*flags: str) -> str | None:
        for flag in flags:
            if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text):
                return flag
        return None

    caps = CliCapabilities(
        print_flag=has("--print", "-p") or ("--print" if not help_text else None),
        output_format=bool(has("--output-format")),
        allowed_tools_flag=has("--allowed-tools", "--allowedTools"),
        disallowed_tools_flag=has("--disallowed-tools", "--disallowedTools"),
        permission_mode=bool(has("--permission-mode", "--permissionMode")),
        model_flag=bool(has("--model")),
        add_dir=bool(has("--add-dir")),
        help_text=help_text[:4000],
    )
    return resolved, version, caps


def clear_cli_probe_cache() -> None:
    """Test hook."""
    _probe_cli.cache_clear()


def build_sandbox_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """A scrubbed environment for the model subprocess."""
    source = dict(os.environ if base_env is None else base_env)
    # Allowlist, not denylist: only the variables the CLI needs to find its own
    # installation and credentials survive.  Everything project-specific or
    # secret-shaped is simply never copied across.
    env = {
        key: source[key]
        for key in ENV_ALLOWLIST
        if key in source
        and not key.startswith(SECRET_ENV_PREFIXES)
        and not any(
            token in key.upper()
            for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        )
    }
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["OUTREACH_SANDBOX"] = "1"
    return env


class SubprocessAIProvider(AIProvider):
    """Shared machinery for CLI-driven AI providers."""

    name = "subprocess"

    def __init__(self, settings: Settings, runner: Any = None) -> None:
        super().__init__(settings)
        self._runner = runner or subprocess.run

    # -- to be supplied by concrete providers --------------------------
    @property
    def executable(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _build_argv(self, resolved: str, caps: CliCapabilities, allow_read: bool) -> list[str]:
        raise NotImplementedError  # pragma: no cover - overridden

    # -- execution -----------------------------------------------------
    def _run_prompt(
        self,
        prompt: str,
        *,
        purpose: str,
        prompt_version: str,
        attachments: dict[str, Path] | None = None,
        allow_read: bool = False,
    ) -> AIResult:
        """Run one bounded, sandboxed model call and parse its JSON output."""
        watch = _Stopwatch()
        resolved, version, caps = _probe_cli(self.executable)
        if resolved is None or not caps.usable:
            return AIResult(
                success=False,
                purpose=purpose,
                provider=self.name,
                error=f"{self.executable!r} is not installed or does not support non-interactive use",
                duration_ms=watch.elapsed_ms,
                prompt_version=prompt_version,
            )

        max_retries = max(0, int(self.settings.claude_max_retries))
        last_error = "unknown failure"
        raw_output = ""
        for attempt in range(max_retries + 1):
            workdir = Path(tempfile.mkdtemp(prefix="outreach-ai-"))
            try:
                for name, source in (attachments or {}).items():
                    source = Path(source)
                    if source.is_file():
                        shutil.copy2(source, workdir / name)
                argv = self._build_argv(resolved, caps, allow_read)
                log.debug(
                    "invoking ai provider",
                    extra={
                        "provider": self.name,
                        "purpose": str(purpose),
                        "attempt": attempt,
                        "workdir": str(workdir),
                    },
                )
                try:
                    completed = self._runner(
                        argv,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        timeout=self.settings.claude_timeout_seconds,
                        cwd=str(workdir),
                        env=build_sandbox_env(),
                        check=False,
                        shell=False,
                    )
                except subprocess.TimeoutExpired:
                    last_error = f"timed out after {self.settings.claude_timeout_seconds}s"
                    log.warning("ai provider timed out", extra={"purpose": str(purpose)})
                    continue
                except (OSError, ValueError) as exc:
                    last_error = f"could not start provider: {exc}"
                    break

                raw_output = (getattr(completed, "stdout", "") or "").strip()
                stderr = (getattr(completed, "stderr", "") or "").strip()
                returncode = getattr(completed, "returncode", 1)
                if returncode != 0:
                    last_error = f"provider exited {returncode}: {stderr[:400] or raw_output[:400]}"
                    continue
                if not raw_output:
                    last_error = f"provider produced no output ({stderr[:200]})"
                    continue

                payload_text = self._unwrap_cli_envelope(raw_output)
                try:
                    parsed = extract_json_object(payload_text)
                except JSONExtractionError as exc:
                    last_error = f"unparseable output: {exc}"
                    continue

                return AIResult(
                    success=True,
                    purpose=purpose,
                    provider=self.name,
                    model=self._model_name(parsed_envelope=raw_output),
                    raw_output=raw_output[:20000],
                    parsed=parsed,
                    duration_ms=watch.elapsed_ms,
                    retry_count=attempt,
                    prompt_version=prompt_version,
                    metadata={"executable": resolved, "version": version, "attempts": attempt + 1},
                )
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

        return AIResult(
            success=False,
            purpose=purpose,
            provider=self.name,
            error=last_error,
            raw_output=raw_output[:4000],
            duration_ms=watch.elapsed_ms,
            retry_count=max_retries,
            prompt_version=prompt_version,
            metadata={"executable": resolved, "version": version},
        )

    @staticmethod
    def _unwrap_cli_envelope(raw: str) -> str:
        """Claude Code's ``--output-format json`` wraps the answer in an envelope."""
        try:
            envelope = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(envelope, dict):
            for key in ("result", "text", "content", "response", "output"):
                value = envelope.get(key)
                if isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, list):
                    parts = [
                        block.get("text", "")
                        for block in value
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    if any(parts):
                        return "\n".join(parts)
            # Already the payload itself.
            return raw
        return raw

    def _model_name(self, parsed_envelope: str = "") -> str | None:
        if self.settings.claude_model:
            return self.settings.claude_model
        try:
            envelope = json.loads(parsed_envelope)
        except ValueError:
            return None
        if isinstance(envelope, dict):
            value = envelope.get("model") or envelope.get("modelName")
            return str(value) if value else None
        return None


class ClaudeCodeProvider(SubprocessAIProvider):
    """The V1 provider: the locally authenticated Claude Code CLI."""

    name = "claude_cli"

    @property
    def executable(self) -> str:
        return self.settings.claude_executable

    # -- health --------------------------------------------------------
    def health_check(self) -> ProviderHealth:
        resolved, version, caps = _probe_cli(self.executable)
        if resolved is None:
            return ProviderHealth(
                False,
                f"{self.executable!r} was not found on PATH. Install the Claude Code CLI "
                "and authenticate it (`claude` then `/login`), or set CLAUDE_EXECUTABLE.",
                executable=None,
            )
        if not caps.usable:
            return ProviderHealth(
                False,
                "the installed CLI does not advertise a non-interactive print mode "
                "(--print/-p); AI operations will stay disabled",
                executable=resolved,
                version=version,
            )
        return ProviderHealth(
            True,
            "claude CLI is installed and supports non-interactive JSON output"
            if caps.output_format
            else "claude CLI is installed (no --output-format; falling back to raw text parsing)",
            executable=resolved,
            version=version,
            supports_non_interactive=True,
            supports_json_output=caps.output_format,
            details={
                "supports_allowed_tools": bool(caps.allowed_tools_flag),
                "supports_disallowed_tools": bool(caps.disallowed_tools_flag),
                "supports_permission_mode": caps.permission_mode,
            },
        )

    # -- argv ----------------------------------------------------------
    def _build_argv(self, resolved: str, caps: CliCapabilities, allow_read: bool) -> list[str]:
        argv = [resolved, caps.print_flag or "--print"]
        if caps.output_format:
            argv += ["--output-format", "json"]
        if caps.model_flag and self.settings.claude_model:
            argv += ["--model", self.settings.claude_model]
        if allow_read and caps.allowed_tools_flag:
            # Read-only file access, and only for jobs that must open the
            # screenshots we copied into the sandbox directory.
            argv += [caps.allowed_tools_flag, "Read"]
        if caps.disallowed_tools_flag:
            denied = list(DENIED_TOOLS) if allow_read else [*DENIED_TOOLS, "Read", "Glob", "Grep"]
            argv += [caps.disallowed_tools_flag, ",".join(denied)]
        return argv

    # -- checkpoints ---------------------------------------------------
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
        attachments: dict[str, Path] = {}
        desktop_name = mobile_name = None
        if desktop_screenshot and Path(desktop_screenshot).is_file():
            desktop_name = "desktop.png"
            attachments[desktop_name] = Path(desktop_screenshot)
        if mobile_screenshot and Path(mobile_screenshot).is_file():
            mobile_name = "mobile.png"
            attachments[mobile_name] = Path(mobile_screenshot)

        prompt = build_score_website_prompt(
            vertical=vertical,
            business_name=business_name,
            technical_findings=technical_findings,
            page_text=page_text,
            desktop_screenshot=desktop_name,
            mobile_screenshot=mobile_name,
        )
        return self._run_prompt(
            prompt,
            purpose=AIPurpose.SCORE_WEBSITE,
            prompt_version=SCORE_WEBSITE_PROMPT_VERSION,
            attachments=attachments,
            allow_read=bool(attachments),
        )

    def extract_business_content(
        self,
        *,
        vertical: str,
        business_name: str,
        deterministic_facts: dict[str, Any],
        evidence: dict[str, Any],
        page_text: str,
    ) -> AIResult:
        prompt = build_extract_content_prompt(
            vertical=vertical,
            business_name=business_name,
            deterministic_facts=deterministic_facts,
            evidence=evidence,
            page_text=page_text,
        )
        return self._run_prompt(
            prompt,
            purpose=AIPurpose.EXTRACT_BUSINESS_CONTENT,
            prompt_version=EXTRACT_CONTENT_PROMPT_VERSION,
        )

    def generate_site_content(
        self, *, vertical: str, business_content: dict[str, Any]
    ) -> AIResult:
        prompt = build_generate_site_content_prompt(
            vertical=vertical, business_content=business_content
        )
        return self._run_prompt(
            prompt,
            purpose=AIPurpose.GENERATE_SITE_CONTENT,
            prompt_version=GENERATE_SITE_CONTENT_PROMPT_VERSION,
        )

    def compose_email(self, **kwargs: Any) -> AIResult:
        prompt = build_compose_email_prompt(**kwargs)
        return self._run_prompt(
            prompt,
            purpose=AIPurpose.COMPOSE_EMAIL,
            prompt_version=COMPOSE_EMAIL_PROMPT_VERSION,
        )

    def classify_reply(self, *, reply_text: str) -> AIResult:
        prompt = build_classify_reply_prompt(reply_text=reply_text)
        return self._run_prompt(
            prompt,
            purpose=AIPurpose.CLASSIFY_REPLY,
            prompt_version=CLASSIFY_REPLY_PROMPT_VERSION,
        )

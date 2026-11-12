"""Centralised, typed configuration and feature flags.

Everything expensive, external, destructive or irreversible is gated behind an
explicit feature flag that defaults to ``false``.  The code for every gated
capability is fully implemented; the flag only decides whether it *executes*.

Nothing in here reads secrets from anywhere except the process environment and
an optional ``.env`` file that is never committed.
"""

from __future__ import annotations

import os
from datetime import time as dt_time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Defensive constants that require a deliberate *code* change to raise.
# A configuration mistake must never be able to blast out hundreds of emails.
# ---------------------------------------------------------------------------
CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT = 100
CODE_ABSOLUTE_MAX_GOOGLE_PLACES_MONTHLY_LIMIT = 5000
CODE_MIN_SECONDS_BETWEEN_SENDS_FLOOR = 30

FEATURE_FLAG_NAMES = (
    "FEATURE_REAL_DISCOVERY",
    "FEATURE_AI_SCORING",
    "FEATURE_AI_CONTENT_EXTRACTION",
    "FEATURE_SITE_BUILDING",
    "FEATURE_PREVIEW_PUBLISHING",
    "FEATURE_EMAIL_COMPOSITION",
    "FEATURE_GMAIL_SEND",
    "FEATURE_REPLY_SYNC",
    "FEATURE_SCHEDULER",
)

FEATURE_FLAG_DOCS: dict[str, str] = {
    "FEATURE_REAL_DISCOVERY": (
        "Allows metered discovery providers (Google Places) to issue real, "
        "billable network requests. OFF = fixture discovery only."
    ),
    "FEATURE_AI_SCORING": (
        "Allows the AI provider to be invoked for visual/UX website scoring. "
        "OFF = deterministic technical scoring only."
    ),
    "FEATURE_AI_CONTENT_EXTRACTION": (
        "Allows the AI provider to be invoked to structure factual business "
        "content and site copy. OFF = deterministic extraction only."
    ),
    "FEATURE_SITE_BUILDING": (
        "Allows real prospect redesign builds to be rendered to the previews "
        "directory. OFF = builds are planned and reported but not written."
    ),
    "FEATURE_PREVIEW_PUBLISHING": (
        "Allows a preview to be published through the configured publisher "
        "(copy/rsync to a public location). OFF = previews stay local."
    ),
    "FEATURE_EMAIL_COMPOSITION": (
        "Allows AI personalisation of outreach copy. OFF = deterministic "
        "template composition (which is always available)."
    ),
    "FEATURE_GMAIL_SEND": (
        "Allows the Gmail API transport to transmit. Must be combined with "
        "OUTREACH_SEND_ENABLED=true. OFF = messages stay queued."
    ),
    "FEATURE_REPLY_SYNC": (
        "Allows Gmail to be polled for replies and opt-outs. OFF = no Gmail "
        "read traffic."
    ),
    "FEATURE_SCHEDULER": (
        "Allows the scheduler entry point to actually execute pipeline work "
        "when invoked by cron/systemd. OFF = scheduler reports and exits."
    ),
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


#: Environment variable -> :class:`FeatureFlags` attribute.
FLAG_ENV_MAP: dict[str, str] = {
    "FEATURE_REAL_DISCOVERY": "real_discovery",
    "FEATURE_AI_SCORING": "ai_scoring",
    "FEATURE_AI_CONTENT_EXTRACTION": "ai_content_extraction",
    "FEATURE_SITE_BUILDING": "site_building",
    "FEATURE_PREVIEW_PUBLISHING": "preview_publishing",
    "FEATURE_EMAIL_COMPOSITION": "email_composition",
    "FEATURE_GMAIL_SEND": "gmail_send",
    "FEATURE_REPLY_SYNC": "reply_sync",
    "FEATURE_SCHEDULER": "scheduler",
}


class FeatureFlags(BaseModel):
    """Typed view over the feature flag environment variables."""

    real_discovery: bool = False
    ai_scoring: bool = False
    ai_content_extraction: bool = False
    site_building: bool = False
    preview_publishing: bool = False
    email_composition: bool = False
    gmail_send: bool = False
    reply_sync: bool = False
    scheduler: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FeatureFlags:
        env = dict(os.environ if env is None else env)
        return cls(**{attr: _as_bool(env.get(name)) for name, attr in FLAG_ENV_MAP.items()})

    def as_env_dict(self) -> dict[str, bool]:
        return {name: getattr(self, attr) for name, attr in FLAG_ENV_MAP.items()}

    def is_enabled(self, env_name: str) -> bool:
        attr = FLAG_ENV_MAP.get(env_name.upper())
        if attr is None:
            raise KeyError(f"Unknown feature flag: {env_name}")
        return bool(getattr(self, attr))


class Settings(BaseSettings):
    """Whole-application configuration."""

    model_config = SettingsConfigDict(
        env_file=None,  # .env is loaded explicitly by load_settings()
        case_sensitive=False,
        extra="ignore",
    )

    # -- storage ----------------------------------------------------------
    database_url: str = Field(default="sqlite:///data/outreach.db", alias="DATABASE_URL")
    artifact_root: Path = Field(default=Path("./artifacts"), alias="ARTIFACT_ROOT")
    preview_root: Path = Field(default=Path("./previews"), alias="PREVIEW_ROOT")
    preview_base_url: str = Field(default="", alias="PREVIEW_BASE_URL")

    # -- crawling ---------------------------------------------------------
    crawl_timeout_seconds: float = Field(default=20.0, alias="CRAWL_TIMEOUT_SECONDS", ge=1)
    crawl_max_pages_per_domain: int = Field(
        default=5, alias="CRAWL_MAX_PAGES_PER_DOMAIN", ge=1, le=50
    )
    crawl_delay_seconds: float = Field(default=1.0, alias="CRAWL_DELAY_SECONDS", ge=0)
    crawl_user_agent: str = Field(
        default=(
            "StoopCraftBot/1.0 (+https://stoopcraft.example/bot; website design outreach; "
            "respects robots.txt)"
        ),
        alias="CRAWL_USER_AGENT",
    )
    crawl_respect_robots: bool = Field(default=True, alias="CRAWL_RESPECT_ROBOTS")
    crawl_max_html_bytes: int = Field(default=3_000_000, alias="CRAWL_MAX_HTML_BYTES", ge=10_000)
    browser_enabled: bool = Field(default=True, alias="BROWSER_ENABLED")
    lighthouse_enabled: bool = Field(default=False, alias="LIGHTHOUSE_ENABLED")
    lighthouse_executable: str = Field(default="lighthouse", alias="LIGHTHOUSE_EXECUTABLE")

    # -- discovery / metered providers ------------------------------------
    google_maps_api_key: str = Field(default="", alias="GOOGLE_MAPS_API_KEY")
    google_places_monthly_request_limit: int = Field(
        default=900, alias="GOOGLE_PLACES_MONTHLY_REQUEST_LIMIT", ge=0
    )

    # -- AI ---------------------------------------------------------------
    ai_provider: Literal["claude_cli", "codex_cli", "null"] = Field(
        default="claude_cli", alias="AI_PROVIDER"
    )
    claude_executable: str = Field(default="claude", alias="CLAUDE_EXECUTABLE")
    claude_timeout_seconds: float = Field(default=180.0, alias="CLAUDE_TIMEOUT_SECONDS", ge=5)
    claude_max_retries: int = Field(default=1, alias="CLAUDE_MAX_RETRIES", ge=0, le=5)
    claude_model: str = Field(default="", alias="CLAUDE_MODEL")
    codex_executable: str = Field(default="codex", alias="CODEX_EXECUTABLE")

    # -- scoring thresholds -----------------------------------------------
    site_score_good_threshold: int = Field(default=70, alias="SITE_SCORE_GOOD_THRESHOLD")
    site_score_borderline_threshold: int = Field(
        default=55, alias="SITE_SCORE_BORDERLINE_THRESHOLD"
    )
    site_score_redesign_threshold: int = Field(default=35, alias="SITE_SCORE_REDESIGN_THRESHOLD")
    # Relative weights, normalised at use time -- "0.6/0.4" and "3/2" mean the
    # same thing.  The upper bound only exists to catch typos.
    deterministic_score_weight: float = Field(
        default=0.6, alias="DETERMINISTIC_SCORE_WEIGHT", ge=0, le=100
    )
    ai_score_weight: float = Field(default=0.4, alias="AI_SCORE_WEIGHT", ge=0, le=100)
    ai_score_candidate_statuses: str = Field(
        default="BORDERLINE,REDESIGN_CANDIDATE,STRONG_REDESIGN_CANDIDATE",
        alias="AI_SCORE_CANDIDATE_STATUSES",
    )

    # -- per-run bounds ---------------------------------------------------
    max_discovered_per_run: int = Field(default=100, alias="MAX_DISCOVERED_PER_RUN", ge=1)
    max_scans_per_run: int = Field(default=100, alias="MAX_SCANS_PER_RUN", ge=1)
    max_ai_scores_per_run: int = Field(default=25, alias="MAX_AI_SCORES_PER_RUN", ge=0)
    max_ai_extractions_per_run: int = Field(default=10, alias="MAX_AI_EXTRACTIONS_PER_RUN", ge=0)
    max_builds_per_run: int = Field(default=10, alias="MAX_BUILDS_PER_RUN", ge=0)

    # -- outreach ---------------------------------------------------------
    outreach_send_enabled: bool = Field(default=False, alias="OUTREACH_SEND_ENABLED")
    outreach_daily_send_limit: int = Field(default=15, alias="OUTREACH_DAILY_SEND_LIMIT", ge=0)
    outreach_hard_max_daily_send_limit: int = Field(
        default=50, alias="OUTREACH_HARD_MAX_DAILY_SEND_LIMIT", ge=0
    )
    outreach_min_seconds_between_sends: int = Field(
        default=900, alias="OUTREACH_MIN_SECONDS_BETWEEN_SENDS", ge=0
    )
    outreach_timezone: str = Field(default="America/New_York", alias="OUTREACH_TIMEZONE")
    outreach_send_window_start: str = Field(default="09:00", alias="OUTREACH_SEND_WINDOW_START")
    outreach_send_window_end: str = Field(default="16:30", alias="OUTREACH_SEND_WINDOW_END")
    outreach_send_days: str = Field(default="MON,TUE,WED,THU,FRI", alias="OUTREACH_SEND_DAYS")
    suppress_by_domain: bool = Field(default=True, alias="OUTREACH_SUPPRESS_BY_DOMAIN")

    # -- sender identity / compliance -------------------------------------
    sender_name: str = Field(default="", alias="SENDER_NAME")
    sender_company: str = Field(default="StoopCraft", alias="SENDER_COMPANY")
    sender_postal_address: str = Field(default="", alias="SENDER_POSTAL_ADDRESS")
    sender_email: str = Field(default="", alias="SENDER_EMAIL")
    sender_reply_to: str = Field(default="", alias="SENDER_REPLY_TO")
    unsubscribe_instruction: str = Field(
        default="Reply STOP and I will not contact you again.",
        alias="UNSUBSCRIBE_INSTRUCTION",
    )

    # -- gmail ------------------------------------------------------------
    gmail_client_secret_path: str = Field(default="", alias="GMAIL_CLIENT_SECRET_PATH")
    gmail_token_path: Path = Field(default=Path("./data/gmail-token.json"), alias="GMAIL_TOKEN_PATH")

    # -- previews / publishing --------------------------------------------
    preview_publisher: Literal["local", "rsync", "null"] = Field(
        default="local", alias="PREVIEW_PUBLISHER"
    )
    preview_publish_dir: str = Field(default="", alias="PREVIEW_PUBLISH_DIR")
    preview_rsync_target: str = Field(default="", alias="PREVIEW_RSYNC_TARGET")
    preview_server_host: str = Field(default="127.0.0.1", alias="PREVIEW_SERVER_HOST")
    preview_server_port: int = Field(default=8800, alias="PREVIEW_SERVER_PORT")

    # -- fixtures ---------------------------------------------------------
    fixture_server_host: str = Field(default="127.0.0.1", alias="FIXTURE_SERVER_HOST")
    fixture_server_port: int = Field(default=8765, alias="FIXTURE_SERVER_PORT")

    # -- logging ----------------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["text", "json"] = Field(default="text", alias="LOG_FORMAT")

    features: FeatureFlags = Field(default_factory=FeatureFlags)

    # ------------------------------------------------------------------
    # validators
    # ------------------------------------------------------------------
    @field_validator("artifact_root", "preview_root", "gmail_token_path", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            return Path(os.path.expandvars(value)).expanduser()
        return value

    @field_validator("outreach_timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env specific
            raise ValueError(f"Unknown OUTREACH_TIMEZONE {value!r}") from exc
        return value

    @field_validator("outreach_send_window_start", "outreach_send_window_end")
    @classmethod
    def _valid_time(cls, value: str) -> str:
        parse_hhmm(value)
        return value

    @model_validator(mode="after")
    def _coherence(self) -> Settings:
        if not (
            self.site_score_redesign_threshold
            < self.site_score_borderline_threshold
            < self.site_score_good_threshold
        ):
            raise ValueError(
                "Site score thresholds must satisfy "
                "REDESIGN < BORDERLINE < GOOD "
                f"(got {self.site_score_redesign_threshold}, "
                f"{self.site_score_borderline_threshold}, {self.site_score_good_threshold})"
            )
        total = self.deterministic_score_weight + self.ai_score_weight
        if total <= 0:
            raise ValueError("DETERMINISTIC_SCORE_WEIGHT + AI_SCORE_WEIGHT must be > 0")
        return self

    # ------------------------------------------------------------------
    # derived / defensive accessors
    # ------------------------------------------------------------------
    @property
    def hard_max_daily_send_limit(self) -> int:
        """Hard ceiling, itself clamped by a constant that needs a code change."""
        return min(self.outreach_hard_max_daily_send_limit, CODE_ABSOLUTE_MAX_DAILY_SEND_LIMIT)

    @property
    def effective_daily_send_limit(self) -> int:
        """The configurable limit can never exceed the defensive hard maximum."""
        return max(0, min(self.outreach_daily_send_limit, self.hard_max_daily_send_limit))

    @property
    def effective_min_seconds_between_sends(self) -> int:
        return max(self.outreach_min_seconds_between_sends, CODE_MIN_SECONDS_BETWEEN_SENDS_FLOOR)

    @property
    def effective_google_places_monthly_limit(self) -> int:
        return min(
            self.google_places_monthly_request_limit,
            CODE_ABSOLUTE_MAX_GOOGLE_PLACES_MONTHLY_LIMIT,
        )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.outreach_timezone)

    @property
    def send_window(self) -> tuple[dt_time, dt_time]:
        return parse_hhmm(self.outreach_send_window_start), parse_hhmm(self.outreach_send_window_end)

    @property
    def send_weekdays(self) -> set[int]:
        """Monday=0 ... Sunday=6."""
        names = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
        out: set[int] = set()
        for token in self.outreach_send_days.split(","):
            token = token.strip().upper()[:3]
            if token in names:
                out.add(names[token])
        return out or {0, 1, 2, 3, 4}

    @property
    def ai_candidate_statuses(self) -> set[str]:
        return {
            s.strip().upper()
            for s in self.ai_score_candidate_statuses.split(",")
            if s.strip()
        }

    @property
    def normalized_weights(self) -> tuple[float, float]:
        total = self.deterministic_score_weight + self.ai_score_weight
        return self.deterministic_score_weight / total, self.ai_score_weight / total

    def resolve_path(self, path: Path | str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def artifact_dir(self) -> Path:
        return self.resolve_path(self.artifact_root)

    @property
    def preview_dir(self) -> Path:
        return self.resolve_path(self.preview_root)

    @property
    def gmail_token_file(self) -> Path:
        return self.resolve_path(self.gmail_token_path)

    def sqlalchemy_url(self) -> str:
        """Resolve relative sqlite paths against the project root."""
        url = self.database_url
        prefix = "sqlite:///"
        if url.startswith(prefix):
            raw = url[len(prefix) :]
            if raw and raw != ":memory:" and not raw.startswith("/"):
                return prefix + str(self.resolve_path(raw))
        return url

    def compliance_problems(self) -> list[str]:
        """Missing sender-identity fields that must block production sending."""
        problems: list[str] = []
        if not self.sender_name.strip():
            problems.append("SENDER_NAME is not configured")
        if not self.sender_company.strip():
            problems.append("SENDER_COMPANY is not configured")
        if not self.sender_postal_address.strip():
            problems.append("SENDER_POSTAL_ADDRESS is not configured")
        if not self.unsubscribe_instruction.strip():
            problems.append("UNSUBSCRIBE_INSTRUCTION is not configured")
        return problems

    def public_dict(self) -> dict[str, Any]:
        """Configuration dump with every secret redacted."""
        secret_fields = {"google_maps_api_key"}
        out: dict[str, Any] = {}
        for name, field in type(self).model_fields.items():
            if name == "features":
                continue
            value = getattr(self, name)
            alias = field.alias or name.upper()
            if name in secret_fields:
                value = f"<set:{len(str(value))} chars>" if value else "<unset>"
            out[alias] = str(value) if isinstance(value, Path) else value
        out.update(self.features.as_env_dict())
        out["_effective_daily_send_limit"] = self.effective_daily_send_limit
        out["_hard_max_daily_send_limit"] = self.hard_max_daily_send_limit
        out["_effective_google_places_monthly_limit"] = self.effective_google_places_monthly_limit
        return out


def parse_hhmm(value: str) -> dt_time:
    """Parse ``HH:MM`` into a :class:`datetime.time`."""
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Expected HH:MM, got {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Expected HH:MM in range, got {value!r}")
    return dt_time(hour=hour, minute=minute)


def _apply_legacy_aliases(env: dict[str, str]) -> None:
    """Accept the historical misspelling of the redesign threshold."""
    if "SITE_SCORE_REDESIGN_THRESHOLD" not in env and "SITE_SCORE_REDIGN_THRESHOLD" in env:
        env["SITE_SCORE_REDESIGN_THRESHOLD"] = env["SITE_SCORE_REDIGN_THRESHOLD"]


def build_settings(env: dict[str, str] | None = None) -> Settings:
    """Construct :class:`Settings` from an explicit mapping (used by tests)."""
    source = dict(os.environ if env is None else env)
    _apply_legacy_aliases(source)
    values: dict[str, Any] = {}
    for name, field in Settings.model_fields.items():
        if name == "features":
            continue
        alias = (field.alias or name).upper()
        if alias in source and source[alias] != "":
            values[name] = source[alias]
    # booleans arrive as strings; pydantic handles "true"/"false" fine, but we
    # normalise the loose forms (yes/on/1) ourselves.
    for bool_field in (
        "outreach_send_enabled",
        "browser_enabled",
        "lighthouse_enabled",
        "crawl_respect_robots",
        "suppress_by_domain",
    ):
        alias = (Settings.model_fields[bool_field].alias or bool_field).upper()
        if alias in source and source[alias] != "":
            values[bool_field] = _as_bool(source[alias])
    values["features"] = FeatureFlags.from_env(source)
    return Settings(**values)


@lru_cache(maxsize=1)
def _cached_settings() -> Settings:
    dotenv_path = PROJECT_ROOT / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)
    return build_settings()


def get_settings() -> Settings:
    """Process-wide settings singleton (loads ``.env`` once, never overriding env)."""
    return _cached_settings()


def reset_settings_cache() -> None:
    """Test hook: drop the cached settings singleton."""
    _cached_settings.cache_clear()

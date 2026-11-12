"""Schemas shared by deterministic scoring, AI scoring and qualification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["low", "medium", "high"]


class Finding(BaseModel):
    """One observable, evidence-backed problem with a website."""

    category: str
    code: str
    severity: Severity = "medium"
    evidence: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def weight(self) -> float:
        return {"low": 1.0, "medium": 2.0, "high": 3.0}[self.severity]


class CategoryScore(BaseModel):
    """A 0-100 sub-score plus the raw signals that produced it."""

    score: float = Field(ge=0, le=100)
    signals: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)


class TechnicalScore(BaseModel):
    """The full deterministic assessment of a website."""

    model_config = ConfigDict(extra="forbid")

    site_quality_score: float = Field(ge=0, le=100)
    mobile: CategoryScore
    technical: CategoryScore
    conversion: CategoryScore
    performance: CategoryScore
    weights: dict[str, float] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    problem_codes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    measured_with_browser: bool = False

    def to_findings_json(self) -> dict[str, Any]:
        """The transparent structure persisted in ``technical_findings_json``."""
        return {
            "site_quality_score": round(self.site_quality_score, 2),
            "weights": self.weights,
            "measured_with_browser": self.measured_with_browser,
            "mobile": {
                **self.mobile.signals,
                "score": round(self.mobile.score, 2),
            },
            "technical": {
                **self.technical.signals,
                "score": round(self.technical.score, 2),
            },
            "conversion": {
                **self.conversion.signals,
                "score": round(self.conversion.score, 2),
            },
            "performance": {
                **self.performance.signals,
                "score": round(self.performance.score, 2),
            },
            "findings": [f.model_dump() for f in self.findings],
            "problem_codes": self.problem_codes,
            "notes": self.notes,
        }


class AIIssue(BaseModel):
    """One issue reported by the AI visual scorer."""

    category: str = ""
    severity: Severity = "medium"
    evidence: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: Any) -> Any:
        text = str(value or "medium").strip().lower()
        return text if text in {"low", "medium", "high"} else "medium"


class AIWebsiteScore(BaseModel):
    """Strict schema the AI visual scorer must produce."""

    model_config = ConfigDict(extra="ignore")

    visual_modernity: int = Field(ge=0, le=100)
    mobile_visual_quality: int = Field(ge=0, le=100)
    conversion_quality: int = Field(ge=0, le=100)
    trust_quality: int = Field(ge=0, le=100)
    dated_probability: float = Field(ge=0.0, le=1.0)
    redesign_opportunity: int = Field(ge=0, le=100)
    issues: list[AIIssue] = Field(default_factory=list)
    summary: str = ""

    @field_validator(
        "visual_modernity",
        "mobile_visual_quality",
        "conversion_quality",
        "trust_quality",
        "redesign_opportunity",
        mode="before",
    )
    @classmethod
    def _coerce_int(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        try:
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            raise ValueError(f"expected a 0-100 number, got {value!r}") from None

    @field_validator("dated_probability", mode="before")
    @classmethod
    def _coerce_prob(cls, value: Any) -> Any:
        """Accept ``0.9``, ``"90%"`` and a bare ``90`` as the same probability.

        Models routinely emit percentages here, so anything in ``(1, 100]`` is
        read as a percentage.  Anything beyond that is nonsense and clamps to 1.
        """
        try:
            number = float(str(value).strip().rstrip("%"))
        except (TypeError, ValueError):
            raise ValueError(f"expected a 0-1 probability, got {value!r}") from None
        if 1.0 < number <= 100.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))

    @field_validator("summary", mode="before")
    @classmethod
    def _clip_summary(cls, value: Any) -> str:
        return str(value or "")[:1200]

    @property
    def site_score(self) -> float:
        """Fold the AI opinion onto the same "how good is this site" 0-100 axis."""
        return round(
            (
                self.visual_modernity
                + self.mobile_visual_quality
                + self.conversion_quality
                + self.trust_quality
            )
            / 4.0,
            2,
        )

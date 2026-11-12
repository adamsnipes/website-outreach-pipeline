"""AIWebsiteAssessment: bounded AI opinion about a rendered website."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, JsonText, UtcDateTime, utcnow

if TYPE_CHECKING:
    from outreach.models.business import Business


class AIWebsiteAssessment(Base):
    __tablename__ = "ai_website_assessment"
    __table_args__ = (
        Index("ix_ai_assessment_business", "business_id"),
        Index("ix_ai_assessment_scan", "scan_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    scan_id: Mapped[int | None] = mapped_column(
        ForeignKey("website_scan.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    visual_modernity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mobile_visual_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trust_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dated_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    redesign_opportunity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    issues_json: Mapped[list[Any] | None] = mapped_column(JsonText, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    business: Mapped[Business] = relationship(back_populates="assessments")

    @property
    def ai_site_score(self) -> float | None:
        """AI opinion folded into the same 0-100 "how good is this site" axis."""
        parts = [
            self.visual_modernity,
            self.mobile_visual_quality,
            self.conversion_quality,
            self.trust_quality,
        ]
        present = [p for p in parts if p is not None]
        if not present:
            return None
        return round(sum(present) / len(present), 2)

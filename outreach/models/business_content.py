"""BusinessContent: factual, evidence-backed facts about a business."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, JsonText, UtcDateTime, utcnow

if TYPE_CHECKING:
    from outreach.models.business import Business


class BusinessContent(Base):
    __tablename__ = "business_content"
    __table_args__ = (Index("ix_business_content_business", "business_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    #: Validated BusinessContent schema payload.
    content_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    #: claim -> [{"source_url": ..., "snippet": ...}] so every fact is traceable.
    source_evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    extraction_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic"
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    business: Mapped[Business] = relationship(back_populates="contents")

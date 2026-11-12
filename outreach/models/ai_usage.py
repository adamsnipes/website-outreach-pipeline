"""AIUsage: an audit row for every AI provider invocation attempt."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from outreach.db import Base, JsonText, UtcDateTime, utcnow


class AIUsage(Base):
    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_purpose", "purpose"),
        Index("ix_ai_usage_started", "started_at"),
        Index("ix_ai_usage_business", "business_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int | None] = mapped_column(
        ForeignKey("business.id", ondelete="SET NULL"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

"""Build: one rendered StoopCraft redesign preview for a prospect."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, JsonText, UtcDateTime, utcnow
from outreach.models.enums import BuildStatus

if TYPE_CHECKING:
    from outreach.models.business import Business
    from outreach.models.outreach_message import OutreachMessage


class Build(Base):
    __tablename__ = "build"
    __table_args__ = (
        Index("ix_build_business", "business_id"),
        Index("ix_build_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    template_name: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=BuildStatus.PENDING)

    #: The validated SiteContent that was rendered.
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    #: Hash of (template version + content) -> lets reruns be idempotent.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    preview_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    preview_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    qa_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    qa_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    qa_screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    built_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    business: Mapped[Business] = relationship(back_populates="builds")
    messages: Mapped[list[OutreachMessage]] = relationship(back_populates="build")

    @property
    def usable(self) -> bool:
        return self.status in {BuildStatus.BUILT, BuildStatus.PUBLISHED} and self.qa_passed is True

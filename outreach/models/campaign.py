"""Campaign: one (vertical, city, state) prospecting effort."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, UtcDateTime, utcnow
from outreach.models.enums import CampaignStatus

if TYPE_CHECKING:
    from outreach.models.business import Business, CampaignBusiness


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (
        UniqueConstraint("vertical", "city", "state", "country", name="uq_campaign_scope"),
        Index("ix_campaign_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    country: Mapped[str] = mapped_column(String(8), nullable=False, default="US")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=CampaignStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    businesses: Mapped[list[Business]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", foreign_keys="Business.campaign_id"
    )
    links: Mapped[list[CampaignBusiness]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return f"{self.vertical} / {self.city}, {self.state}"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Campaign id={self.id} {self.label!r} status={self.status}>"

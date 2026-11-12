"""Business + the campaign/business association used for cross-campaign dedupe."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, UtcDateTime, utcnow
from outreach.models.enums import ContactabilityStatus, QualificationStatus

if TYPE_CHECKING:
    from outreach.models.ai_assessment import AIWebsiteAssessment
    from outreach.models.build import Build
    from outreach.models.business_content import BusinessContent
    from outreach.models.campaign import Campaign
    from outreach.models.contact import Contact
    from outreach.models.outreach_message import OutreachMessage
    from outreach.models.scan import WebsiteScan


class Business(Base):
    """A discovered local business.

    Deduplication happens on two axes and both are enforced by the schema:

    ``(external_source, external_id)``
        The provider's stable identifier, when one exists.
    ``normalized_domain``
        Prevents the same practice being rebuilt/contacted because a second
        search happened to surface it again.  SQLite treats NULLs as distinct,
        so businesses without a website do not collide.
    """

    __tablename__ = "business"
    __table_args__ = (
        UniqueConstraint("external_source", "external_id", name="uq_business_external"),
        UniqueConstraint("normalized_domain", name="uq_business_domain"),
        Index("ix_business_campaign", "campaign_id"),
        Index("ix_business_qualification", "qualification_status"),
        Index("ix_business_name_city", "name", "city"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"), nullable=True
    )
    external_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True, default="US")

    website_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    normalized_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    business_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Rolled-up pipeline state (always derivable from child rows, cached here so
    # reporting and candidate selection stay single-query cheap).
    qualification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=QualificationStatus.UNKNOWN
    )
    contactability_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ContactabilityStatus.UNKNOWN
    )
    combined_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    build_priority: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    campaign: Mapped[Campaign | None] = relationship(
        back_populates="businesses", foreign_keys=[campaign_id]
    )
    scans: Mapped[list[WebsiteScan]] = relationship(
        back_populates="business", cascade="all, delete-orphan", order_by="WebsiteScan.id"
    )
    contacts: Mapped[list[Contact]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    assessments: Mapped[list[AIWebsiteAssessment]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    contents: Mapped[list[BusinessContent]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    builds: Mapped[list[Build]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    messages: Mapped[list[OutreachMessage]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    campaign_links: Mapped[list[CampaignBusiness]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )

    @property
    def latest_scan(self) -> WebsiteScan | None:
        return self.scans[-1] if self.scans else None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Business id={self.id} {self.name!r} domain={self.normalized_domain}>"


class CampaignBusiness(Base):
    """Which campaigns have surfaced a business (many-to-many discovery log)."""

    __tablename__ = "campaign_business"
    __table_args__ = (
        UniqueConstraint("campaign_id", "business_id", name="uq_campaign_business"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaign.id", ondelete="CASCADE"), nullable=False
    )
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    campaign: Mapped[Campaign] = relationship(back_populates="links")
    business: Mapped[Business] = relationship(back_populates="campaign_links")

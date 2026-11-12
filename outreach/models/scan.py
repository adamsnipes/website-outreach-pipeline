"""WebsiteScan: one crawl + render + deterministic scoring of a prospect site."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, JsonText, UtcDateTime, utcnow
from outreach.models.enums import ScanStatus

if TYPE_CHECKING:
    from outreach.models.business import Business


class WebsiteScan(Base):
    __tablename__ = "website_scan"
    __table_args__ = (
        Index("ix_scan_business", "business_id"),
        Index("ix_scan_status", "scan_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )

    requested_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    final_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ScanStatus.PENDING
    )

    desktop_screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mobile_screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    homepage_html_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    technical_site_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    technical_findings_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    browser_metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    pages_json: Mapped[list[Any] | None] = mapped_column(JsonText, nullable=True)
    pages_crawled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    business: Mapped[Business] = relationship(back_populates="scans")

    @property
    def succeeded(self) -> bool:
        return self.scan_status == ScanStatus.OK

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<WebsiteScan id={self.id} business={self.business_id} "
            f"status={self.scan_status} score={self.technical_site_quality_score}>"
        )

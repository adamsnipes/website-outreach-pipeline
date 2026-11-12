"""ProviderUsage: one row per metered-provider request *attempt*.

Blocked attempts are recorded too, which is what makes the Google Places
monthly ceiling auditable rather than merely aspirational.  Monthly accounting
is ``SUM(billable_units) WHERE month_key = 'YYYY-MM' AND status = 'ALLOWED'``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from outreach.db import Base, JsonText, UtcDateTime, utcnow
from outreach.models.enums import ProviderUsageStatus


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    __table_args__ = (
        Index("ix_provider_usage_month", "provider", "month_key"),
        Index("ix_provider_usage_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``YYYY-MM`` in UTC; the unit monthly quotas are accounted against.
    month_key: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProviderUsageStatus.ALLOWED
    )
    billable_units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    @staticmethod
    def month_key_for(moment: datetime | None = None) -> str:
        moment = moment or utcnow()
        return moment.strftime("%Y-%m")

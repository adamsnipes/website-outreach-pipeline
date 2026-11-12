"""Suppression: permanent do-not-contact list.

Entries are only ever removed by an explicit operator action.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from outreach.db import Base, UtcDateTime, utcnow
from outreach.models.enums import SuppressionReason


class Suppression(Base):
    __tablename__ = "suppression"
    __table_args__ = (
        UniqueConstraint("normalized_email", name="uq_suppression_email"),
        UniqueConstraint("normalized_domain", name="uq_suppression_domain"),
        Index("ix_suppression_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    normalized_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    normalized_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default=SuppressionReason.MANUAL)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        target = self.normalized_email or f"@{self.normalized_domain}"
        return f"<Suppression {target!r} reason={self.reason}>"

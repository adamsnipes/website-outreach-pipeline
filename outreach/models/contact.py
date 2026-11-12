"""Contact: an email address the business itself published."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, UtcDateTime, utcnow
from outreach.models.enums import ContactType

if TYPE_CHECKING:
    from outreach.models.business import Business


class Contact(Base):
    __tablename__ = "contact"
    __table_args__ = (
        UniqueConstraint("business_id", "normalized_email", name="uq_contact_business_email"),
        Index("ix_contact_normalized_email", "normalized_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ContactType.UNKNOWN
    )
    #: Where the address was literally found.  Never synthesised, never guessed.
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

    business: Mapped[Business] = relationship(back_populates="contacts")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Contact {self.normalized_email!r} type={self.contact_type} c={self.confidence}>"

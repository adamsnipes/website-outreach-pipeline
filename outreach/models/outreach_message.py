"""OutreachMessage: the single initial cold email for a business.

The ``UNIQUE(business_id)`` constraint is the schema-level half of the
"exactly one initial email per business, ever" guarantee.  Retries mutate this
row; they never insert a second one.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from outreach.db import Base, JsonText, UtcDateTime, utcnow
from outreach.models.enums import MessageStatus

if TYPE_CHECKING:
    from outreach.models.build import Build
    from outreach.models.business import Business


class OutreachMessage(Base):
    __tablename__ = "outreach_message"
    __table_args__ = (
        UniqueConstraint("business_id", name="uq_message_one_per_business"),
        UniqueConstraint("gmail_message_id", name="uq_message_gmail_id"),
        Index("ix_message_status", "status"),
        Index("ix_message_scheduled_at", "scheduled_at"),
        Index("ix_message_recipient", "recipient_email"),
        Index("ix_message_thread", "gmail_thread_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("business.id", ondelete="CASCADE"), nullable=False
    )
    build_id: Mapped[int | None] = mapped_column(
        ForeignKey("build.id", ondelete="SET NULL"), nullable=True
    )

    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=MessageStatus.DRAFT)
    composition_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="deterministic"
    )
    #: The one or two genuine, evidence-backed observations cited in the copy.
    observations_json: Mapped[list | None] = mapped_column(JsonText, nullable=True)

    scheduled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sending_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    gmail_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Stable client-side idempotency token embedded in the outgoing headers so
    #: an ambiguous send can be reconciled against the mailbox afterwards.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    send_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    business: Mapped[Business] = relationship(back_populates="messages")
    build: Mapped[Build | None] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<OutreachMessage id={self.id} business={self.business_id} "
            f"to={self.recipient_email!r} status={self.status}>"
        )

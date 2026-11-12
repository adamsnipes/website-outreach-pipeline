"""Event: append-only log of every meaningful pipeline state transition.

Also the substrate for later learning work: which website problems correlate
with replies and sales.  No ML here, just high-quality structured data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from outreach.db import Base, JsonText, UtcDateTime, utcnow


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_entity", "entity_type", "entity_id"),
        Index("ix_event_type_created", "event_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)

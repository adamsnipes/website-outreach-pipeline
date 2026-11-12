"""System-level persistent state: the global pause switch and job locks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from outreach.db import Base, JsonText, UtcDateTime, utcnow

PAUSE_KEY = "system_paused"


class SystemState(Base):
    """Tiny key/value store for durable operator switches."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[Any | None] = mapped_column(JsonText, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class JobLock(Base):
    """Advisory lock row preventing overlapping cron/systemd invocations."""

    __tablename__ = "job_lock"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acquired_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

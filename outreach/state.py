"""Durable operator switches: the global pause and cron overlap locks."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from outreach.db import session_scope, utcnow
from outreach.events import record_event
from outreach.logging import get_logger
from outreach.models.enums import EventType
from outreach.models.system import PAUSE_KEY, JobLock, SystemState

log = get_logger(__name__)


@dataclass(frozen=True)
class PauseState:
    paused: bool
    reason: str | None = None
    since: datetime | None = None

    @property
    def summary(self) -> str:
        if not self.paused:
            return "RUNNING"
        when = self.since.isoformat() if self.since else "unknown"
        return f"PAUSED since {when} ({self.reason or 'no reason given'})"


def get_pause_state(session: Session) -> PauseState:
    row = session.get(SystemState, PAUSE_KEY)
    if row is None or not row.value_json:
        return PauseState(paused=False)
    value = row.value_json
    if not isinstance(value, dict):
        return PauseState(paused=bool(value))
    since = value.get("since")
    parsed: datetime | None = None
    if isinstance(since, str):
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError:
            parsed = None
    return PauseState(
        paused=bool(value.get("paused")), reason=value.get("reason"), since=parsed
    )


def is_paused(session: Session) -> bool:
    return get_pause_state(session).paused


def set_pause(session: Session, paused: bool, reason: str | None = None) -> PauseState:
    """Pause or resume the whole system.  Affects every external side effect."""
    row = session.get(SystemState, PAUSE_KEY)
    payload = {
        "paused": bool(paused),
        "reason": reason,
        "since": utcnow().isoformat(),
    }
    if row is None:
        row = SystemState(key=PAUSE_KEY, value_json=payload)
        session.add(row)
    else:
        row.value_json = payload
        row.updated_at = utcnow()
    record_event(
        session,
        "system",
        None,
        EventType.SYSTEM_PAUSED if paused else EventType.SYSTEM_RESUMED,
        {"reason": reason},
    )
    log.warning("system %s", "paused" if paused else "resumed", extra={"reason": reason})
    return get_pause_state(session)


def pause_state_standalone() -> PauseState:
    with session_scope() as session:
        return get_pause_state(session)


class LockUnavailable(RuntimeError):
    """Raised when another process already holds the named job lock."""


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def acquire_lock(session: Session, name: str, ttl_seconds: int = 3600) -> bool:
    """Try to take a named lock.  Expired locks are reclaimed automatically."""
    now = utcnow()
    existing = session.get(JobLock, name)
    if existing is not None:
        if existing.expires_at > now:
            return False
        log.warning(
            "reclaiming expired job lock",
            extra={"lock": name, "previous_holder": existing.holder},
        )
        session.delete(existing)
        session.flush()
    lock = JobLock(
        name=name,
        holder=_holder(),
        pid=os.getpid(),
        hostname=socket.gethostname(),
        acquired_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    session.add(lock)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True


def release_lock(session: Session, name: str) -> None:
    session.execute(delete(JobLock).where(JobLock.name == name, JobLock.holder == _holder()))


def list_locks(session: Session) -> list[JobLock]:
    return list(session.scalars(select(JobLock).order_by(JobLock.name)).all())


@contextmanager
def job_lock(name: str, ttl_seconds: int = 3600, required: bool = True) -> Iterator[bool]:
    """Context manager guarding against overlapping cron/systemd invocations.

    Uses its own short-lived sessions so the lock is committed immediately and
    is visible to other processes for the whole duration of the work.
    """
    acquired = False
    with session_scope() as session:
        acquired = acquire_lock(session, name, ttl_seconds)
    if not acquired and required:
        raise LockUnavailable(f"Job lock {name!r} is held by another process")
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with session_scope() as session:
                    release_lock(session, name)
            except Exception:  # pragma: no cover - best effort cleanup
                log.exception("failed to release job lock", extra={"lock": name})

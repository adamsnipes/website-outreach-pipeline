"""Append-only event log helper.

Every meaningful pipeline state transition goes through :func:`record_event`
so that a run can be reconstructed after the fact and so that later analysis
can correlate website problems with replies and sales.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach.logging import get_logger
from outreach.models.event import Event

log = get_logger(__name__)


def record_event(
    session: Session,
    entity_type: str,
    entity_id: int | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    flush: bool = False,
) -> Event:
    """Append an event row.  Never raises for a payload that will not serialise."""
    event = Event(
        entity_type=str(entity_type),
        entity_id=entity_id,
        event_type=str(event_type),
        payload_json=payload or None,
    )
    session.add(event)
    if flush:
        session.flush()
    log.debug(
        "event",
        extra={"entity_type": entity_type, "entity_id": entity_id, "event_type": str(event_type)},
    )
    return event


def recent_events(session: Session, limit: int = 50) -> list[Event]:
    return list(
        session.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
    )


def events_for(session: Session, entity_type: str, entity_id: int) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.entity_type == entity_type, Event.entity_id == entity_id)
            .order_by(Event.id)
        ).all()
    )

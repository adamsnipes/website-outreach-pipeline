"""Database engine, session management and the declarative base.

SQLite is the only storage engine.  Everything is designed so that a crash at
any point leaves a consistent, resumable state:

* every persistent timestamp is stored as naive UTC (see :class:`UtcDateTime`)
* uniqueness rules that matter for safety (one outreach message per business,
  one contact per address, one business per domain) are enforced by the schema
  as well as by application code
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, Text, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from outreach.config import Settings, get_settings

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker[Session] | None = None
_ENGINE_URL: str | None = None


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Store timezone-aware datetimes as naive UTC, read them back as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class JsonText(TypeDecorator[Any]):
    """JSON stored as TEXT so the database stays trivially inspectable."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, default=str, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None


class Base(DeclarativeBase):
    """Declarative base for every model."""

    type_annotation_map = {dict: JsonText, list: JsonText}


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


def create_db_engine(url: str | None = None, settings: Settings | None = None) -> Engine:
    """Create a configured engine (does not touch the module-level singleton)."""
    settings = settings or get_settings()
    url = url or settings.sqlalchemy_url()
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///") :]
        if raw and raw != ":memory:":
            Path(raw).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True, echo=False)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    """Process-wide engine singleton, rebuilt if the configured URL changes."""
    global _ENGINE, _SESSION_FACTORY, _ENGINE_URL
    settings = settings or get_settings()
    url = settings.sqlalchemy_url()
    if _ENGINE is None or url != _ENGINE_URL:
        dispose_engine()
        _ENGINE = create_db_engine(url, settings)
        _SESSION_FACTORY = sessionmaker(bind=_ENGINE, expire_on_commit=False, future=True)
        _ENGINE_URL = url
    return _ENGINE


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _SESSION_FACTORY is not None
    return _SESSION_FACTORY


def dispose_engine() -> None:
    """Drop the engine singleton (used by tests and by long-lived daemons)."""
    global _ENGINE, _SESSION_FACTORY, _ENGINE_URL
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None
    _ENGINE_URL = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional session scope: commit on success, roll back on failure."""
    factory = get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(settings: Settings | None = None, use_alembic: bool = True) -> str:
    """Create the schema.

    Prefers stamping/upgrading through Alembic so that migrations remain the
    source of truth; falls back to ``metadata.create_all`` when Alembic is not
    available (for example inside an ephemeral test database).
    """
    settings = settings or get_settings()
    engine = get_engine(settings)
    import outreach.models  # noqa: F401  (registers every mapper)

    if use_alembic:
        try:
            from alembic import command
            from alembic.config import Config

            ini = Path(__file__).resolve().parent.parent / "alembic.ini"
            if ini.exists():
                cfg = Config(str(ini))
                cfg.set_main_option("sqlalchemy.url", settings.sqlalchemy_url())
                os.environ.setdefault("OUTREACH_ALEMBIC_RUNNING", "1")
                command.upgrade(cfg, "head")
                return "alembic-upgrade"
        except Exception as exc:  # pragma: no cover - environment dependent
            from outreach.logging import get_logger

            get_logger(__name__).warning(
                "alembic upgrade failed, falling back to create_all", extra={"error": str(exc)}
            )
    Base.metadata.create_all(engine)
    return "create-all"

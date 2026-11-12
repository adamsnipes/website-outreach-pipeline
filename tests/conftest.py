"""Shared fixtures.

Nothing in the default test run touches Google, Claude, Gmail or the internet.
Every external provider is mocked; the only network traffic is loopback HTTP to
the bundled fixture site server.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("LOG_LEVEL", "WARNING")

import outreach.models  # noqa: F401  (registers mappers)
from outreach.config import Settings, build_settings
from outreach.db import Base, create_db_engine, dispose_engine
from outreach.fixtures.server import FixtureSiteServer

BASE_ENV: dict[str, str] = {
    "DATABASE_URL": "sqlite:///:memory:",
    "CRAWL_DELAY_SECONDS": "0",
    "CRAWL_TIMEOUT_SECONDS": "10",
    "SENDER_NAME": "Test Sender",
    "SENDER_COMPANY": "StoopCraft",
    "SENDER_POSTAL_ADDRESS": "1 Test Street, Testville, NY 10001",
    "SENDER_EMAIL": "outreach@stoopcraft.test",
    "PREVIEW_BASE_URL": "https://previews.stoopcraft-outreach.com/",
}


@pytest.fixture
def env_settings(tmp_path: Path) -> Any:
    """Factory building Settings from BASE_ENV plus overrides."""

    def _make(**overrides: str) -> Settings:
        env = {
            **BASE_ENV,
            "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "PREVIEW_ROOT": str(tmp_path / "previews"),
            "GMAIL_TOKEN_PATH": str(tmp_path / "gmail-token.json"),
            **overrides,
        }
        return build_settings(env)

    return _make


@pytest.fixture
def settings(env_settings: Any) -> Settings:
    return env_settings()


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    """A real SQLite database on disk, so constraints behave as in production."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        dispose_engine()


@pytest.fixture
def db_settings(tmp_path: Path, env_settings: Any) -> Settings:
    """Settings pointed at a real on-disk database (for session_scope users)."""
    return env_settings(DATABASE_URL=f"sqlite:///{tmp_path / 'pipeline.db'}")


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[FixtureSiteServer]:
    """One loopback fixture site server shared by the whole test session."""
    server = FixtureSiteServer(port=0)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fixture_base_url(fixture_server: FixtureSiteServer) -> str:
    return fixture_server.base_url


@pytest.fixture
def sample_html() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "outreach/fixtures/sites/sunrise-family-dental/index.html"
    ).read_text(encoding="utf-8")


@pytest.fixture
def modern_html() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "outreach/fixtures/sites/cactus-modern-dental/index.html"
    ).read_text(encoding="utf-8")


@pytest.fixture
def injection_html() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "outreach/fixtures/sites/whitetooth-dental-injection/index.html"
    ).read_text(encoding="utf-8")


class FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_runner() -> Any:
    """Factory producing a scripted subprocess runner and recording its calls."""

    def _make(*responses: Any) -> Any:
        queue = list(responses)
        calls: list[dict[str, Any]] = []

        def runner(argv: list[str], **kwargs: Any) -> Any:
            calls.append({"argv": argv, **kwargs})
            if not queue:
                return FakeCompleted("{}", "", 0)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        runner.calls = calls  # type: ignore[attr-defined]
        return runner

    return _make

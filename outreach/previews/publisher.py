"""Preview publishing abstraction.

Publishing is an external side effect, so it only ever happens when
``FEATURE_PREVIEW_PUBLISHING`` is on, the system is not paused, and the run is
not a dry run.  No cloud provider is assumed: the shipped publishers copy files
to a directory or rsync them to a host you already own.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outreach.config import Settings
from outreach.logging import get_logger
from outreach.previews.manager import preview_url_for

log = get_logger(__name__)


@dataclass
class PublishResult:
    published: bool
    destination: str | None = None
    url: str | None = None
    reason: str = ""
    files: int = 0
    details: dict[str, Any] = field(default_factory=dict)


class PreviewPublisher(ABC):
    """Interface every publishing backend implements."""

    name = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Whether this publisher is configured well enough to run."""

    @abstractmethod
    def publish(self, business_id: int, source_dir: Path, *, dry_run: bool = False) -> PublishResult:
        """Publish one preview directory."""

    def _url(self, business_id: int) -> str | None:
        return preview_url_for(self.settings, business_id)


class NullPublisher(PreviewPublisher):
    """Publishes nothing.  Used when publishing is disabled."""

    name = "null"

    def available(self) -> tuple[bool, str]:
        return True, "no-op publisher"

    def publish(self, business_id: int, source_dir: Path, *, dry_run: bool = False) -> PublishResult:
        return PublishResult(False, reason="publishing is disabled (null publisher)")


class LocalCopyPublisher(PreviewPublisher):
    """Copies the preview into a directory served by an existing web server."""

    name = "local"

    def available(self) -> tuple[bool, str]:
        if not self.settings.preview_publish_dir:
            return False, "PREVIEW_PUBLISH_DIR is not configured"
        return True, f"copies to {self.settings.preview_publish_dir}"

    def publish(self, business_id: int, source_dir: Path, *, dry_run: bool = False) -> PublishResult:
        ok, reason = self.available()
        if not ok:
            return PublishResult(False, reason=reason)
        source_dir = Path(source_dir)
        if not (source_dir / "index.html").is_file():
            return PublishResult(False, reason=f"nothing to publish in {source_dir}")

        destination = Path(self.settings.preview_publish_dir).expanduser() / str(business_id)
        files = [p for p in source_dir.rglob("*") if p.is_file()]
        if dry_run:
            return PublishResult(
                False,
                destination=str(destination),
                url=self._url(business_id),
                reason="DRY RUN: no files were copied",
                files=len(files),
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_dir, destination)
        log.info(
            "preview published",
            extra={"business_id": business_id, "destination": str(destination)},
        )
        return PublishResult(
            True,
            destination=str(destination),
            url=self._url(business_id),
            reason="copied",
            files=len(files),
        )


class RsyncPublisher(PreviewPublisher):
    """Pushes the preview to a host you already own via rsync over ssh."""

    name = "rsync"

    def __init__(self, settings: Settings, runner: Any = None) -> None:
        super().__init__(settings)
        self._runner = runner or subprocess.run

    def available(self) -> tuple[bool, str]:
        if not self.settings.preview_rsync_target:
            return False, "PREVIEW_RSYNC_TARGET is not configured"
        if shutil.which("rsync") is None:
            return False, "rsync is not installed on this machine"
        return True, f"rsync to {self.settings.preview_rsync_target}"

    def publish(self, business_id: int, source_dir: Path, *, dry_run: bool = False) -> PublishResult:
        ok, reason = self.available()
        if not ok:
            return PublishResult(False, reason=reason)
        source_dir = Path(source_dir)
        if not (source_dir / "index.html").is_file():
            return PublishResult(False, reason=f"nothing to publish in {source_dir}")

        target = self.settings.preview_rsync_target.rstrip("/") + f"/{business_id}/"
        argv = ["rsync", "-az", "--delete", f"{source_dir}/", target]
        if dry_run:
            argv.insert(1, "--dry-run")
        completed = self._runner(argv, capture_output=True, text=True, check=False, shell=False)
        returncode = getattr(completed, "returncode", 1)
        if returncode != 0:
            return PublishResult(
                False,
                destination=target,
                reason=f"rsync failed ({returncode}): {getattr(completed, 'stderr', '')[:300]}",
            )
        if dry_run:
            return PublishResult(
                False, destination=target, url=self._url(business_id),
                reason="DRY RUN: rsync --dry-run only",
            )
        return PublishResult(
            True, destination=target, url=self._url(business_id), reason="rsynced"
        )


def get_publisher(settings: Settings, name: str | None = None) -> PreviewPublisher:
    key = (name or settings.preview_publisher or "local").strip().lower()
    registry: dict[str, type[PreviewPublisher]] = {
        "local": LocalCopyPublisher,
        "copy": LocalCopyPublisher,
        "rsync": RsyncPublisher,
        "null": NullPublisher,
        "none": NullPublisher,
    }
    if key not in registry:
        raise KeyError(f"Unknown preview publisher {name!r}. Known: {sorted(set(registry))}")
    return registry[key](settings)

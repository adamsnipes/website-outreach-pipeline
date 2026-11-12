"""Preview locations and the rules about which URLs may be emailed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from outreach.config import Settings
from outreach.logging import get_logger
from outreach.messaging.compliance import RESERVED_TLDS
from outreach.normalize import is_private_or_local_url, normalize_url

log = get_logger(__name__)


def preview_dir_for(settings: Settings, business_id: int) -> Path:
    """``previews/<business_id>/`` -- deterministic and stable across reruns."""
    return settings.preview_dir / str(business_id)


@dataclass(frozen=True)
class PreviewUrlCheck:
    usable: bool
    reason: str
    url: str | None = None


def preview_url_for(settings: Settings, business_id: int) -> str | None:
    """Public URL of a preview, if a public base URL is configured."""
    base = (settings.preview_base_url or "").strip()
    if not base:
        return None
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, f"{business_id}/")


def validate_preview_url(url: str | None) -> PreviewUrlCheck:
    """A preview URL is only emailable if a stranger could actually open it."""
    if not url:
        return PreviewUrlCheck(False, "no preview URL is configured (PREVIEW_BASE_URL is empty)")
    normalized = normalize_url(url)
    if not normalized:
        return PreviewUrlCheck(False, f"preview URL is not a usable http(s) URL: {url!r}")
    if is_private_or_local_url(normalized):
        return PreviewUrlCheck(
            False,
            "preview URL points at localhost or a private address, which the "
            "recipient could never open",
            normalized,
        )
    host = (urlparse(normalized).hostname or "").lower()
    if host.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return PreviewUrlCheck(
            False,
            f"preview URL uses the reserved TLD .{host.rsplit('.', 1)[-1]}, which never "
            "resolves for a recipient",
            normalized,
        )
    if normalized.lower().startswith("http://"):
        return PreviewUrlCheck(
            False, "preview URL must be https:// before it is emailed", normalized
        )
    return PreviewUrlCheck(True, "ok", normalized)


class PreviewManager:
    """Owns the previews directory."""

    NOINDEX_RE = re.compile(
        r'<meta[^>]+name=["\']robots["\'][^>]*content=["\'][^"\']*noindex', re.I
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def root(self) -> Path:
        return self.settings.preview_dir

    def dir_for(self, business_id: int) -> Path:
        return preview_dir_for(self.settings, business_id)

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def exists(self, business_id: int) -> bool:
        return (self.dir_for(business_id) / "index.html").is_file()

    def index_path(self, business_id: int) -> Path:
        return self.dir_for(business_id) / "index.html"

    def public_url(self, business_id: int) -> str | None:
        return preview_url_for(self.settings, business_id)

    def verify_noindex(self, business_id: int) -> bool:
        """Every generated preview must carry a noindex directive."""
        path = self.index_path(business_id)
        if not path.is_file():
            return False
        return bool(self.NOINDEX_RE.search(path.read_text(encoding="utf-8", errors="replace")))

    def list_previews(self) -> list[tuple[int, Path]]:
        if not self.root.is_dir():
            return []
        out: list[tuple[int, Path]] = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and child.name.isdigit():
                out.append((int(child.name), child))
        return out

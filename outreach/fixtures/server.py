"""A tiny, loopback-only static server for the bundled fixture websites.

Deliberately minimal: it exists so Playwright and httpx have a real origin to
talk to.  It binds to 127.0.0.1, serves only files under the fixture root, and
supports ``*.redirect`` marker files so redirect handling can be exercised.
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any

from outreach.logging import get_logger

log = get_logger(__name__)

FIXTURE_PLACEHOLDER = "{FIXTURE_BASE}"


def fixture_sites_root() -> Path:
    return Path(__file__).resolve().parent / "sites"


class _FixtureRequestHandler(SimpleHTTPRequestHandler):
    """Static handler with redirect markers and a quiet log."""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("fixture-server " + format, *args)

    def _redirect_target(self) -> str | None:
        path = self.translate_path(self.path)
        candidates = [Path(path + ".redirect")]
        if Path(path).is_dir():
            candidates.append(Path(path) / "index.html.redirect")
        for marker in candidates:
            if marker.is_file():
                return marker.read_text(encoding="utf-8").strip()
        return None

    def do_GET(self) -> None:
        target = self._redirect_target()
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        target = self._redirect_target()
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class FixtureSiteServer:
    """Context manager owning a background fixture HTTP server."""

    def __init__(
        self,
        root: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.root = Path(root) if root else fixture_sites_root()
        self.host = host
        self.requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self.requested_port
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        if self._httpd is not None:
            return self.base_url
        self.root.mkdir(parents=True, exist_ok=True)
        handler = partial(_FixtureRequestHandler, directory=str(self.root))
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.requested_port), handler)
        except OSError:
            # Port busy (another run, or a leftover server): take an ephemeral one.
            log.warning(
                "fixture server port busy, using an ephemeral port",
                extra={"requested_port": self.requested_port},
            )
            self._httpd = ThreadingHTTPServer((self.host, 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="fixture-site-server", daemon=True
        )
        self._thread.start()
        log.info("fixture site server started", extra={"base_url": self.base_url})
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FixtureSiteServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

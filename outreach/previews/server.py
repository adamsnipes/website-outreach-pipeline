"""Local static server for reviewing previews before anything is published.

Loopback only.  This is a review tool, never the production host: URLs it
produces can never be emailed (see :func:`validate_preview_url`).
"""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from outreach.logging import get_logger

log = get_logger(__name__)

INDEX_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>StoopCraft previews</title>
<style>
body{{font:16px/1.6 system-ui,sans-serif;margin:0;background:#f7f5f0;color:#10231f}}
main{{max-width:760px;margin:0 auto;padding:48px 24px}}
h1{{font-size:1.6rem;letter-spacing:-.02em}}
ul{{list-style:none;padding:0}}
li{{border:1px solid #dfe7e4;background:#fff;border-radius:12px;margin:.5rem 0}}
a{{display:block;padding:1rem 1.25rem;color:#0e4f47;text-decoration:none;font-weight:600}}
a:hover{{background:#e3efec}}
p.empty{{color:#6b8580}}
</style></head>
<body><main><h1>Generated previews</h1>{body}</main></body></html>
"""


class _PreviewHandler(SimpleHTTPRequestHandler):
    """Static handler that lists previews at the root instead of the raw dirs."""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("preview-server " + format, *args)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            root = Path(self.directory)
            entries = sorted(
                (p for p in root.iterdir() if p.is_dir() and (p / "index.html").is_file()),
                key=lambda p: (not p.name.isdigit(), p.name),
            )
            if entries:
                items = "".join(
                    f'<li><a href="/{p.name}/">Business #{p.name}</a></li>' for p in entries
                )
                body = f"<ul>{items}</ul>"
            else:
                body = (
                    '<p class="empty">No previews have been generated yet. '
                    "Run <code>python -m outreach build-candidates</code> with "
                    "FEATURE_SITE_BUILDING=true.</p>"
                )
            payload = INDEX_TEMPLATE.format(body=body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()  # end_headers adds the X-Robots-Tag
            self.wfile.write(payload)
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class PreviewServer:
    """Background static server over the previews directory."""

    def __init__(self, root: Path, host: str = "127.0.0.1", port: int = 8800) -> None:
        self.root = Path(root)
        self.host = host
        self.requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1]) if self._httpd else self.requested_port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        handler = partial(_PreviewHandler, directory=str(self.root))
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.requested_port), handler)
        except OSError:
            self._httpd = ThreadingHTTPServer((self.host, 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def serve_forever(self) -> None:
        """Blocking mode used by ``python -m outreach previews serve``."""
        self.start()
        assert self._thread is not None
        try:
            self._thread.join()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            self.stop()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> PreviewServer:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

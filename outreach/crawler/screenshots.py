"""Rendering, screenshots and in-browser measurement.

Everything measured here is observable fact about the page as a browser sees
it: layout overflow, tiny text, tap-target sizes, console errors, failed
resource requests and navigation timings.  The deterministic scorer consumes
these; the AI scorer only ever sees them as evidence.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outreach.crawler.browser import (
    DESKTOP_VIEWPORT,
    MOBILE_VIEWPORT,
    BrowserSession,
)
from outreach.logging import get_logger

log = get_logger(__name__)

#: Measurement script.  Pure reads -- it never mutates the page.
MEASURE_JS = r"""
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const body = document.body;
  const out = {
    viewport_width: vw,
    viewport_height: window.innerHeight,
    scroll_width: Math.max(doc ? doc.scrollWidth : 0, body ? body.scrollWidth : 0),
    document_height: Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0),
  };
  out.horizontal_overflow_px = Math.max(0, out.scroll_width - vw);

  const all = Array.from(document.querySelectorAll('body *')).slice(0, 4000);
  let wide = 0, tinyText = 0, textNodes = 0, smallTargets = 0, targets = 0;
  let minFont = 99, fixedWidthContainers = 0;
  for (const el of all) {
    let rect;
    try { rect = el.getBoundingClientRect(); } catch (e) { continue; }
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.width > vw + 8 && rect.left < vw) wide++;
    const style = window.getComputedStyle(el);
    if (style.position !== 'fixed' && style.position !== 'absolute') {
      const w = style.width || '';
      if (w.endsWith('px')) {
        const px = parseFloat(w);
        if (px >= 900) fixedWidthContainers++;
      }
    }
    const ownText = Array.from(el.childNodes)
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim())
      .join(' ');
    if (ownText.length > 12) {
      textNodes++;
      const fs = parseFloat(style.fontSize) || 16;
      if (fs < minFont) minFont = fs;
      if (fs < 12) tinyText++;
    }
    const tag = el.tagName.toLowerCase();
    const clickable = tag === 'a' || tag === 'button' ||
      (tag === 'input' && ['submit','button','checkbox','radio'].includes((el.type||'').toLowerCase()));
    if (clickable && rect.width > 0 && rect.height > 0) {
      targets++;
      if (rect.height < 32 || rect.width < 32) smallTargets++;
    }
  }
  out.elements_wider_than_viewport = wide;
  out.fixed_wide_containers = fixedWidthContainers;
  out.tiny_text_blocks = tinyText;
  out.text_blocks = textNodes;
  out.min_font_size_px = minFont === 99 ? null : Math.round(minFont * 10) / 10;
  out.tap_targets = targets;
  out.small_tap_targets = smallTargets;

  const vp = document.querySelector('meta[name="viewport" i]');
  out.viewport_meta = !!vp;
  out.viewport_meta_content = vp ? (vp.getAttribute('content') || '') : null;
  out.viewport_meta_responsive = !!(vp && /width\s*=\s*device-width/i.test(vp.getAttribute('content') || ''));

  out.has_media_queries = (() => {
    let count = 0;
    for (const sheet of Array.from(document.styleSheets)) {
      try {
        for (const rule of Array.from(sheet.cssRules || [])) {
          if (rule.type === CSSRule.MEDIA_RULE) count++;
        }
      } catch (e) { /* cross-origin sheet */ }
    }
    return count;
  })();

  const foldHeight = window.innerHeight;
  const ctaWords = /(book|schedule|appointment|request|call|contact|consult|get started|new patient)/i;
  let ctaAboveFold = false, ctaAnywhere = false, ctaLinkAboveFold = false;
  let telLinks = 0, mailtoLinks = 0;
  for (const a of Array.from(document.querySelectorAll('a,button,input[type=submit]')).slice(0, 1500)) {
    const label = (a.innerText || a.value || a.getAttribute('aria-label') || '').trim();
    const href = (a.getAttribute && a.getAttribute('href')) || '';
    if (href.toLowerCase().startsWith('tel:')) telLinks++;
    if (href.toLowerCase().startsWith('mailto:')) mailtoLinks++;
    if (!ctaWords.test(label) && !ctaWords.test(href)) continue;
    ctaAnywhere = true;
    let r; try { r = a.getBoundingClientRect(); } catch (e) { continue; }
    if (!(r.top < foldHeight && r.top >= -10 && r.width > 0)) continue;
    ctaLinkAboveFold = true;
    // "Prominent" means it reads as a button, not as one more nav link.
    const st = window.getComputedStyle(a);
    const tag = a.tagName.toLowerCase();
    const bg = st.backgroundColor || '';
    const hasFill = bg && bg !== 'transparent' && !/rgba\(0,\s*0,\s*0,\s*0\)/.test(bg);
    const radius = parseFloat(st.borderRadius) || 0;
    const padded = (parseFloat(st.paddingTop) || 0) + (parseFloat(st.paddingBottom) || 0) >= 12;
    const buttonish = tag === 'button' || tag === 'input' || hasFill || radius >= 4 || padded;
    if (buttonish && r.height >= 30) ctaAboveFold = true;
  }
  out.cta_above_fold = ctaAboveFold;
  out.cta_link_above_fold = ctaLinkAboveFold;
  out.cta_anywhere = ctaAnywhere;
  out.tel_links = telLinks;
  out.mailto_links = mailtoLinks;
  out.forms = document.querySelectorAll('form').length;
  out.inputs = document.querySelectorAll('input,textarea,select').length;
  out.h1_count = document.querySelectorAll('h1').length;
  out.h2_count = document.querySelectorAll('h2').length;
  out.images = document.querySelectorAll('img').length;
  out.images_missing_alt = Array.from(document.querySelectorAll('img'))
    .filter(i => !i.getAttribute('alt')).length;
  out.broken_images = Array.from(document.querySelectorAll('img'))
    .filter(i => i.complete && i.naturalWidth === 0).length;
  out.tables = document.querySelectorAll('table').length;
  out.layout_tables = Array.from(document.querySelectorAll('table')).filter(t => {
    const r = t.getBoundingClientRect();
    return r.width > vw * 0.6 && !t.querySelector('th');
  }).length;
  out.legacy_tags = ['font','center','marquee','blink','frameset','frame','big','strike'].reduce(
    (acc, t) => acc + document.getElementsByTagName(t).length, 0);
  out.inline_styles = document.querySelectorAll('[style]').length;
  out.flex_or_grid = Array.from(document.querySelectorAll('body *')).slice(0, 1500)
    .filter(el => { const d = getComputedStyle(el).display; return d === 'flex' || d === 'grid'; }).length;

  const nav = document.querySelector('nav, .nav, #nav, .menu, #menu, header ul');
  if (nav) {
    const r = nav.getBoundingClientRect();
    out.nav_width = Math.round(r.width);
    out.nav_overflows = r.width > vw + 8;
  } else {
    out.nav_width = null;
    out.nav_overflows = false;
  }

  const t = performance.getEntriesByType('navigation')[0];
  if (t) {
    out.timing = {
      dom_content_loaded_ms: Math.round(t.domContentLoadedEventEnd),
      load_event_ms: Math.round(t.loadEventEnd),
      response_end_ms: Math.round(t.responseEnd),
      transfer_size_bytes: t.transferSize || null,
    };
  }
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = Math.round(p.startTime);
  out.paint = paints;
  out.resource_count = performance.getEntriesByType('resource').length;
  out.total_transfer_bytes = performance.getEntriesByType('resource')
    .reduce((a, r) => a + (r.transferSize || 0), 0);
  out.title = document.title || null;
  const md = document.querySelector('meta[name="description" i]');
  out.meta_description = md ? md.getAttribute('content') : null;
  out.lang = document.documentElement.getAttribute('lang') || null;
  out.text_length = (document.body ? document.body.innerText || '' : '').length;
  return out;
}
"""


@dataclass
class ViewportRender:
    """One viewport's worth of rendered evidence."""

    label: str
    screenshot_path: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    http_status: int | None = None
    final_url: str | None = None
    rendered_html: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.metrics)


@dataclass
class RenderResult:
    """Desktop + mobile evidence for one website."""

    desktop: ViewportRender | None = None
    mobile: ViewportRender | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.desktop and self.desktop.ok)

    def to_metrics_dict(self) -> dict[str, Any]:
        return {
            "desktop": {
                "metrics": self.desktop.metrics if self.desktop else {},
                "console_errors": self.desktop.console_errors if self.desktop else [],
                "page_errors": self.desktop.page_errors if self.desktop else [],
                "failed_requests": self.desktop.failed_requests if self.desktop else [],
                "error": self.desktop.error if self.desktop else "not rendered",
            },
            "mobile": {
                "metrics": self.mobile.metrics if self.mobile else {},
                "console_errors": self.mobile.console_errors if self.mobile else [],
                "page_errors": self.mobile.page_errors if self.mobile else [],
                "failed_requests": self.mobile.failed_requests if self.mobile else [],
                "error": self.mobile.error if self.mobile else "not rendered",
            },
        }


def capture_viewport(
    session: BrowserSession,
    url: str,
    screenshot_path: Path | None,
    *,
    mobile: bool,
    full_page: bool = True,
    wait_until: str = "load",
    settle_ms: int = 700,
    capture_html: bool = False,
) -> ViewportRender:
    """Render ``url`` in one viewport and collect screenshots + measurements."""
    label = "mobile" if mobile else "desktop"
    render = ViewportRender(label=label)
    try:
        with session.context(mobile=mobile) as ctx:
            page = ctx.new_page()
            page.on(
                "console",
                lambda msg: render.console_errors.append(f"{msg.type}: {msg.text}"[:400])
                if msg.type in ("error",)
                else None,
            )
            page.on("pageerror", lambda err: render.page_errors.append(str(err)[:400]))
            page.on(
                "requestfailed",
                lambda req: render.failed_requests.append(
                    {
                        "url": req.url[:400],
                        "type": req.resource_type,
                        "failure": (req.failure or "")[:200],
                    }
                ),
            )

            def _on_response(response: Any) -> None:
                try:
                    if response.status >= 400:
                        render.failed_requests.append(
                            {
                                "url": response.url[:400],
                                "type": "http",
                                "failure": f"HTTP {response.status}",
                            }
                        )
                except Exception:  # pragma: no cover - defensive
                    pass

            page.on("response", _on_response)

            try:
                response = page.goto(url, wait_until=wait_until)
                render.http_status = response.status if response else None
            except Exception as exc:
                # A load-event timeout still leaves a usable DOM to measure.
                render.error = f"navigation issue: {exc}"[:300]
            page.wait_for_timeout(settle_ms)
            render.final_url = page.url

            try:
                render.metrics = page.evaluate(MEASURE_JS) or {}
                render.error = None if render.metrics else render.error
            except Exception as exc:
                render.error = (render.error or "") + f" | measure failed: {exc}"

            if render.metrics:
                # A page with no responsive viewport meta gets Chrome's legacy
                # 980px layout viewport on a phone: the page is *rendered
                # zoomed out*, which is the real "not mobile friendly" signal
                # and is invisible to a plain scrollWidth/innerWidth compare.
                device_width = (MOBILE_VIEWPORT if mobile else DESKTOP_VIEWPORT)["width"]
                render.metrics["device_width"] = device_width
                layout_width = int(render.metrics.get("viewport_width") or device_width)
                render.metrics["layout_viewport_width"] = layout_width
                render.metrics["layout_viewport_overflow_px"] = max(
                    0, int(render.metrics.get("scroll_width") or 0) - device_width
                )
                render.metrics["zoomed_out_on_device"] = layout_width > device_width + 8
                if layout_width > device_width:
                    scale = device_width / layout_width
                    min_font = render.metrics.get("min_font_size_px")
                    if min_font:
                        render.metrics["effective_min_font_size_px"] = round(min_font * scale, 1)

            if capture_html:
                try:
                    render.rendered_html = page.content()
                except Exception:  # pragma: no cover - defensive
                    render.rendered_html = None

            if screenshot_path is not None:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(screenshot_path), full_page=full_page)
                except Exception:
                    try:
                        page.screenshot(path=str(screenshot_path), full_page=False)
                    except Exception as exc:  # pragma: no cover - defensive
                        render.error = (render.error or "") + f" | screenshot failed: {exc}"
                if screenshot_path.exists():
                    render.screenshot_path = str(screenshot_path)
            page.close()
    except Exception as exc:
        render.error = f"render failed: {exc}"[:300]
        log.warning("viewport render failed", extra={"url": url, "viewport": label, "error": str(exc)})
    return render


def render_site(
    url: str,
    out_dir: Path,
    *,
    session: BrowserSession | None = None,
    full_page: bool = True,
    capture_html: bool = False,
) -> RenderResult:
    """Desktop (1440x1000) + mobile (390x844) render of a single URL."""
    out_dir = Path(out_dir)
    owns_session = session is None
    result = RenderResult()
    try:
        session = session or BrowserSession()
        if owns_session:
            session.start()
        result.desktop = capture_viewport(
            session, url, out_dir / "desktop.png", mobile=False,
            full_page=full_page, capture_html=capture_html,
        )
        result.mobile = capture_viewport(
            session, url, out_dir / "mobile.png", mobile=True, full_page=full_page,
        )
    except Exception as exc:
        result.error = f"browser session failed: {exc}"[:300]
        log.warning("render_site failed", extra={"url": url, "error": str(exc)})
    finally:
        if owns_session and session is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                session.stop()
    return result


__all__ = [
    "DESKTOP_VIEWPORT",
    "MEASURE_JS",
    "MOBILE_VIEWPORT",
    "RenderResult",
    "ViewportRender",
    "capture_viewport",
    "render_site",
]

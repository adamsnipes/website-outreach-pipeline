"""Automated QA of a rendered preview.

A build that fails QA is never published and never emailed.  The checks are the
ones that would embarrass us in front of a prospect: broken assets, mobile
overflow, a missing title, a missing noindex, dead phone links, empty sections
and any placeholder text that slipped through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from outreach.crawler.browser import BrowserSession, browser_available
from outreach.crawler.screenshots import capture_viewport
from outreach.logging import get_logger

log = get_logger(__name__)

#: Anything matching these must never reach a prospect.
PLACEHOLDER_PATTERNS = [
    re.compile(r"\bDr\.?\s*(Name|Firstname|Lastname|Doe)\b", re.I),
    re.compile(r"\blorem ipsum\b", re.I),
    re.compile(r"\byour (business|practice|company|name|logo|text) here\b", re.I),
    re.compile(r"\b(TBD|TODO|FIXME|XXX|PLACEHOLDER|COMING SOON)\b"),
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"\[(insert|add|your)[^\]]{0,40}\]", re.I),
    re.compile(r"\bexample\.com\b", re.I),
    re.compile(r"\bJohn Doe\b|\bJane Doe\b"),
]

QA_JS = r"""
() => {
  const out = {};
  out.title = document.title || '';
  const robots = document.querySelector('meta[name="robots" i]');
  out.robots = robots ? (robots.getAttribute('content') || '') : '';
  const vp = document.querySelector('meta[name="viewport" i]');
  out.viewport = vp ? (vp.getAttribute('content') || '') : '';
  out.scroll_width = document.documentElement.scrollWidth;
  out.viewport_width = window.innerWidth;
  out.overflow_px = Math.max(0, document.documentElement.scrollWidth - window.innerWidth);
  out.broken_images = Array.from(document.querySelectorAll('img'))
    .filter(i => i.complete && i.naturalWidth === 0)
    .map(i => i.getAttribute('src'));
  out.images = document.querySelectorAll('img').length;
  out.tel_links = Array.from(document.querySelectorAll('a[href^="tel:"]')).map(a => a.getAttribute('href'));
  out.mailto_links = Array.from(document.querySelectorAll('a[href^="mailto:"]')).map(a => a.getAttribute('href'));
  out.internal_anchors = Array.from(document.querySelectorAll('a[href^="#"]')).map(a => a.getAttribute('href'));
  out.headings = { h1: document.querySelectorAll('h1').length, h2: document.querySelectorAll('h2').length };
  out.empty_sections = Array.from(document.querySelectorAll('section'))
    .filter(s => (s.innerText || '').trim().length < 12)
    .map(s => s.id || s.className || 'section');
  out.stylesheet_applied = (() => {
    const b = document.querySelector('.btn');
    if (!b) return null;
    const bg = getComputedStyle(b).backgroundColor;
    return !!bg && bg !== 'rgba(0, 0, 0, 0)';
  })();
  out.nav_links = Array.from(document.querySelectorAll('.primary-nav a')).map(a => a.getAttribute('href'));
  out.text_length = (document.body.innerText || '').length;
  out.body_text = (document.body.innerText || '').slice(0, 20000);
  return out;
}
"""


@dataclass
class QAResult:
    passed: bool = False
    checks: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    mobile_screenshot_path: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "failures": self.failures,
            "warnings": self.warnings,
            "checks": self.checks,
            "screenshot": self.screenshot_path,
            "mobile_screenshot": self.mobile_screenshot_path,
        }


def static_checks(html: str, output_dir: Path) -> tuple[list[str], list[str], dict[str, Any]]:
    """Checks that need no browser, so QA still says something without one."""
    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    checks["has_title"] = bool(re.search(r"<title>\s*\S", html, re.I))
    if not checks["has_title"]:
        failures.append("rendered page has no <title>")

    noindex = re.search(
        r'<meta[^>]+name=["\']robots["\'][^>]*content=["\'][^"\']*noindex', html, re.I
    )
    checks["has_noindex"] = bool(noindex)
    if not noindex:
        failures.append("rendered page is missing <meta name=robots content=noindex>")

    checks["has_viewport"] = bool(
        re.search(r'name=["\']viewport["\'][^>]*width=device-width', html, re.I)
    )
    if not checks["has_viewport"]:
        failures.append("rendered page has no responsive viewport meta tag")

    found_placeholders = [
        match.group(0)[:60]
        for pattern in PLACEHOLDER_PATTERNS
        for match in [pattern.search(html)]
        if match
    ]
    checks["placeholders"] = found_placeholders
    if found_placeholders:
        failures.append(f"placeholder content in output: {found_placeholders}")

    # Every local asset the page references must exist on disk.
    missing: list[str] = []
    for match in re.finditer(r'(?:src|href)=["\']([^"\'#?]+)["\']', html):
        ref = match.group(1)
        if ref.startswith(("http://", "https://", "//", "mailto:", "tel:", "data:", "#")):
            continue
        if not (output_dir / ref.lstrip("/")).exists():
            missing.append(ref)
    checks["missing_local_assets"] = missing
    if missing:
        failures.append(f"broken local asset references: {missing[:5]}")

    for tel in re.findall(r'href=["\']tel:([^"\']*)["\']', html):
        digits = re.sub(r"\D", "", tel)
        if len(digits) < 10:
            failures.append(f"invalid tel: link {tel!r}")
    return failures, warnings, checks


def run_qa(
    output_dir: Path,
    *,
    session: BrowserSession | None = None,
    screenshot_dir: Path | None = None,
    use_browser: bool = True,
) -> QAResult:
    """Open the generated preview and verify it is fit to send."""
    output_dir = Path(output_dir)
    index_path = output_dir / "index.html"
    result = QAResult()

    if not index_path.is_file():
        result.failures.append(f"no index.html was rendered in {output_dir}")
        return result

    html = index_path.read_text(encoding="utf-8", errors="replace")
    failures, warnings, checks = static_checks(html, output_dir)
    result.failures.extend(failures)
    result.warnings.extend(warnings)
    result.checks.update(checks)

    if not use_browser:
        result.skipped = True
        result.skip_reason = "browser QA disabled by configuration"
        result.passed = not result.failures
        return result

    owns_session = session is None
    if owns_session:
        availability = browser_available()
        if not availability.available:
            result.skipped = True
            result.skip_reason = availability.reason
            result.warnings.append(f"browser QA skipped: {availability.reason}")
            result.passed = not result.failures
            return result
        session = BrowserSession()
        session.start()

    screenshot_dir = Path(screenshot_dir or output_dir)
    try:
        assert session is not None
        url = index_path.resolve().as_uri()
        desktop = capture_viewport(
            session, url, screenshot_dir / "qa-desktop.png", mobile=False, full_page=True
        )
        mobile = capture_viewport(
            session, url, screenshot_dir / "qa-mobile.png", mobile=True, full_page=True
        )
        result.screenshot_path = desktop.screenshot_path
        result.mobile_screenshot_path = mobile.screenshot_path

        for label, render in (("desktop", desktop), ("mobile", mobile)):
            try:
                with session.context(mobile=(label == "mobile")) as ctx:
                    page = ctx.new_page()
                    page.goto(url, wait_until="load")
                    page.wait_for_timeout(300)
                    data = page.evaluate(QA_JS) or {}
                    page.close()
            except Exception as exc:
                result.failures.append(f"{label} QA render failed: {exc}")
                continue

            body_text = str(data.pop("body_text", ""))
            result.checks[label] = data

            if label == "mobile" and int(data.get("overflow_px") or 0) > 4:
                result.failures.append(
                    f"mobile horizontal overflow of {data['overflow_px']}px in the generated site"
                )
            if data.get("broken_images"):
                result.failures.append(f"{label}: broken images {data['broken_images'][:4]}")
            if data.get("stylesheet_applied") is False:
                result.failures.append(f"{label}: stylesheet did not apply")
            if int(data.get("headings", {}).get("h1", 0)) != 1:
                result.failures.append(
                    f"{label}: expected exactly one <h1>, found {data.get('headings', {}).get('h1')}"
                )
            if data.get("empty_sections"):
                result.failures.append(f"{label}: empty sections rendered {data['empty_sections']}")
            if int(data.get("text_length") or 0) < 400:
                result.failures.append(f"{label}: page has almost no text content")

            for placeholder in PLACEHOLDER_PATTERNS:
                match = placeholder.search(body_text)
                if match:
                    result.failures.append(f"{label}: placeholder text rendered {match.group(0)[:50]!r}")
                    break

            anchors = {a.lstrip("#") for a in data.get("nav_links") or [] if a}
            if anchors:
                missing_targets = [
                    a for a in anchors if a and f'id="{a}"' not in html
                ]
                if missing_targets:
                    result.failures.append(f"{label}: navigation points at missing sections {missing_targets}")

            for tel in data.get("tel_links") or []:
                if len(re.sub(r"\D", "", tel or "")) < 10:
                    result.failures.append(f"{label}: invalid tel link {tel!r}")

            errors = list(render.page_errors)
            if errors:
                result.failures.append(f"{label}: JavaScript errors {errors[:2]}")
    finally:
        if owns_session and session is not None:
            session.stop()

    result.passed = not result.failures
    log.info(
        "build QA complete",
        extra={"dir": str(output_dir), "passed": result.passed, "failures": len(result.failures)},
    )
    return result

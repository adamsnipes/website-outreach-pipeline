"""Deterministic website quality scoring.

0 = terrible/broken/outdated, 100 = excellent/modern.  No AI is involved: every
point comes from something a browser or an HTML parser actually observed, and
every deduction is recorded as a :class:`Finding` with its evidence.

Notably *not* used as evidence: the copyright year in the footer.  Plenty of
excellent sites forget to bump it and plenty of dead ones auto-generate it.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from outreach.crawler.screenshots import RenderResult
from outreach.logging import get_logger
from outreach.normalize import is_private_or_local_url
from outreach.scoring.schemas import CategoryScore, Finding, TechnicalScore

log = get_logger(__name__)

CATEGORY_WEIGHTS = {
    "mobile": 0.35,
    "technical": 0.25,
    "conversion": 0.28,
    "performance": 0.12,
}

_OBSOLETE_TAGS = ("font", "center", "marquee", "blink", "frameset", "frame", "big", "strike", "tt")
_OBSOLETE_ATTRS = ("bgcolor", "cellpadding", "cellspacing", "valign", "align", "border", "hspace")
_LEGACY_LIBRARY_RE = re.compile(
    r"(jquery[.-]?1\.\d|jquery[.-]?2\.\d|prototype\.js|mootools|scriptaculous|"
    r"swfobject|flash|\.swf|silverlight|dojo-1\.[0-5])",
    re.I,
)
_BUILDER_RE = re.compile(
    r"(FrontPage|Dreamweaver|GoLive|Microsoft Word|Publisher|iWeb|NetObjects)", re.I
)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


class _Scorer:
    """Accumulates deductions and findings for one category."""

    def __init__(self, category: str, start: float = 100.0) -> None:
        self.category = category
        self.score = start
        self.signals: dict[str, Any] = {}
        self.findings: list[Finding] = []

    def signal(self, name: str, value: Any) -> None:
        self.signals[name] = value

    def deduct(
        self,
        points: float,
        code: str,
        severity: str,
        evidence: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.score -= points
        self.findings.append(
            Finding(
                category=self.category,
                code=code,
                severity=severity,  # type: ignore[arg-type]
                evidence=evidence,
                detail=detail or {},
            )
        )

    def credit(self, points: float) -> None:
        self.score += points

    def result(self) -> CategoryScore:
        return CategoryScore(
            score=_clamp(self.score), signals=self.signals, findings=self.findings
        )


def score_website(
    html: str,
    final_url: str,
    render: RenderResult | None = None,
    *,
    lighthouse: dict[str, Any] | None = None,
    extra_notes: list[str] | None = None,
) -> TechnicalScore:
    """Score a website from its HTML plus (optionally) browser measurements."""
    soup = BeautifulSoup(html or "", "lxml")
    desktop = render.desktop.metrics if (render and render.desktop) else {}
    mobile = render.mobile.metrics if (render and render.mobile) else {}
    has_browser = bool(desktop or mobile)
    notes = list(extra_notes or [])

    mobile_score = _score_mobile(soup, mobile, desktop, has_browser)
    technical_score = _score_technical(soup, html, final_url, render, has_browser)
    conversion_score = _score_conversion(soup, desktop, mobile, has_browser)
    performance_score = _score_performance(desktop, lighthouse, has_browser, notes)

    total = (
        mobile_score.score * CATEGORY_WEIGHTS["mobile"]
        + technical_score.score * CATEGORY_WEIGHTS["technical"]
        + conversion_score.score * CATEGORY_WEIGHTS["conversion"]
        + performance_score.score * CATEGORY_WEIGHTS["performance"]
    )

    findings = [
        *mobile_score.findings,
        *technical_score.findings,
        *conversion_score.findings,
        *performance_score.findings,
    ]
    if not has_browser:
        notes.append(
            "scored from HTML only (no browser measurements) -- mobile and "
            "performance confidence is reduced"
        )

    return TechnicalScore(
        site_quality_score=_clamp(total),
        mobile=mobile_score,
        technical=technical_score,
        conversion=conversion_score,
        performance=performance_score,
        weights=dict(CATEGORY_WEIGHTS),
        findings=findings,
        problem_codes=sorted({f.code for f in findings}),
        notes=notes,
        measured_with_browser=has_browser,
    )


# ---------------------------------------------------------------------------
# MOBILE
# ---------------------------------------------------------------------------
def _score_mobile(
    soup: BeautifulSoup,
    mobile: dict[str, Any],
    desktop: dict[str, Any],
    has_browser: bool,
) -> CategoryScore:
    s = _Scorer("mobile")

    viewport_tag = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    viewport_content = str(viewport_tag.get("content") or "") if viewport_tag else ""
    responsive_viewport = bool(re.search(r"width\s*=\s*device-width", viewport_content, re.I))
    s.signal("viewport_meta", bool(viewport_tag))
    s.signal("viewport_meta_content", viewport_content or None)

    if not viewport_tag:
        s.deduct(
            45, "no_viewport_meta", "high",
            "The page has no <meta name=\"viewport\"> tag, so phones render it "
            "zoomed out at desktop width.",
        )
    elif not responsive_viewport:
        s.deduct(
            25, "fixed_width_viewport", "high",
            f'The viewport meta tag is "{viewport_content}" rather than '
            "width=device-width, so the layout does not adapt to phone screens.",
            {"content": viewport_content},
        )

    if has_browser and mobile:
        overflow = int(mobile.get("layout_viewport_overflow_px") or 0)
        zoomed = bool(mobile.get("zoomed_out_on_device"))
        s.signal("horizontal_overflow_px", overflow)
        s.signal("zoomed_out_on_device", zoomed)
        s.signal("layout_viewport_width", mobile.get("layout_viewport_width"))
        if zoomed and overflow > 0:
            severity = "high" if overflow > 300 else "medium"
            s.deduct(
                min(30.0, 8 + overflow / 25),
                "mobile_horizontal_overflow", severity,
                f"On a 390px-wide phone the page still lays out at "
                f"{mobile.get('layout_viewport_width')}px, {overflow}px wider than the screen.",
                {"overflow_px": overflow},
            )
        wide_elements = int(mobile.get("elements_wider_than_viewport") or 0)
        s.signal("elements_wider_than_viewport", wide_elements)
        if wide_elements:
            s.deduct(
                min(12.0, wide_elements * 1.5), "elements_wider_than_viewport", "medium",
                f"{wide_elements} element(s) are wider than the phone viewport.",
                {"count": wide_elements},
            )
        fixed_containers = int(desktop.get("fixed_wide_containers") or 0)
        s.signal("fixed_desktop_width_containers", fixed_containers)
        if fixed_containers:
            s.deduct(
                min(12.0, 4 + fixed_containers * 1.5), "fixed_desktop_layout", "high",
                f"{fixed_containers} container(s) use a hard-coded desktop pixel width.",
                {"count": fixed_containers},
            )
        effective_font = mobile.get("effective_min_font_size_px") or mobile.get("min_font_size_px")
        s.signal("min_font_size_px", mobile.get("min_font_size_px"))
        s.signal("effective_min_font_size_px", effective_font)
        if effective_font is not None and float(effective_font) < 9:
            s.deduct(
                14, "unreadable_mobile_text", "high",
                f"Body text renders at roughly {effective_font}px on a phone, well "
                "below a readable size.",
                {"effective_px": effective_font},
            )
        elif effective_font is not None and float(effective_font) < 12:
            s.deduct(
                7, "small_mobile_text", "medium",
                f"Some text renders at roughly {effective_font}px on a phone.",
                {"effective_px": effective_font},
            )
        if mobile.get("nav_overflows"):
            s.deduct(
                8, "mobile_nav_overflow", "medium",
                "The navigation is wider than the phone viewport.",
                {"nav_width": mobile.get("nav_width")},
            )
        s.signal("nav_overflows", bool(mobile.get("nav_overflows")))

        targets = int(mobile.get("tap_targets") or 0)
        small = int(mobile.get("small_tap_targets") or 0)
        s.signal("tap_targets", targets)
        s.signal("small_tap_targets", small)
        if targets and small / targets > 0.5:
            s.deduct(
                10, "small_tap_targets", "medium",
                f"{small} of {targets} links/buttons are smaller than a 32px touch target.",
                {"small": small, "total": targets},
            )
        media_queries = int(desktop.get("has_media_queries") or 0)
        s.signal("css_media_queries", media_queries)
        if media_queries == 0 and not responsive_viewport:
            s.deduct(
                10, "no_responsive_css", "high",
                "No CSS media queries were found, so there is no responsive layout at all.",
            )
        if mobile.get("tel_links"):
            s.credit(4)
        s.signal("mobile_tel_link", bool(mobile.get("tel_links")))
        if mobile.get("cta_above_fold"):
            s.credit(4)
        s.signal("mobile_cta_above_fold", bool(mobile.get("cta_above_fold")))
    else:
        # HTML-only fallback: infer from markup alone, conservatively.
        style_blob = " ".join(
            (tag.string or "") for tag in soup.find_all("style") if tag.string
        )
        inline_widths = re.findall(r"width\s*:\s*(\d{3,4})px", style_blob, re.I)
        wide = [int(w) for w in inline_widths if int(w) >= 900]
        s.signal("fixed_desktop_width_containers", len(wide))
        if wide:
            s.deduct(
                20, "fixed_desktop_layout", "high",
                f"Stylesheet declares fixed desktop widths ({sorted(set(wide))[:3]}px).",
                {"widths": sorted(set(wide))[:5]},
            )
        if "@media" not in style_blob and not responsive_viewport:
            s.deduct(15, "no_responsive_css", "high", "No @media rules found in inline CSS.")
        s.signal("css_media_queries", style_blob.count("@media"))

    return s.result()


# ---------------------------------------------------------------------------
# TECHNICAL
# ---------------------------------------------------------------------------
def _score_technical(
    soup: BeautifulSoup,
    html: str,
    final_url: str,
    render: RenderResult | None,
    has_browser: bool,
) -> CategoryScore:
    s = _Scorer("technical")
    url = final_url or ""
    is_local = is_private_or_local_url(url)
    is_https = url.lower().startswith("https://")
    s.signal("https", is_https)
    if is_local:
        # Fixture / LAN targets are served over plain HTTP by design.
        s.signal("https_check_skipped", True)
    elif not is_https:
        s.deduct(
            22, "no_https", "high",
            "The site is served over plain HTTP; browsers label it \"Not secure\".",
        )

    if is_https:
        mixed = re.findall(r'(?:src|href)\s*=\s*["\']http://[^"\']+', html or "", re.I)
        mixed = [m for m in mixed if "http://www.w3.org" not in m and "http://schema.org" not in m]
        s.signal("mixed_content_refs", len(mixed))
        if mixed:
            s.deduct(
                min(15.0, 5 + len(mixed)), "mixed_content", "medium",
                f"{len(mixed)} asset(s) are loaded over http:// on an https:// page.",
                {"examples": mixed[:3]},
            )

    obsolete_counts = {tag: len(soup.find_all(tag)) for tag in _OBSOLETE_TAGS}
    obsolete_total = sum(obsolete_counts.values())
    s.signal("obsolete_tags", {k: v for k, v in obsolete_counts.items() if v})
    if obsolete_total:
        s.deduct(
            min(18.0, 6 + obsolete_total * 0.8), "obsolete_html_tags", "medium",
            f"{obsolete_total} obsolete HTML tag(s) in use "
            f"({', '.join(k for k, v in obsolete_counts.items() if v)}).",
            {"counts": {k: v for k, v in obsolete_counts.items() if v}},
        )

    tables = soup.find_all("table")
    layout_tables = [t for t in tables if not t.find("th") and len(t.find_all("tr")) >= 2]
    s.signal("tables", len(tables))
    s.signal("probable_layout_tables", len(layout_tables))
    if layout_tables:
        s.deduct(
            min(16.0, 6 + len(layout_tables) * 2.5), "table_layout", "high",
            f"{len(layout_tables)} table(s) appear to be used for page layout rather than data.",
            {"count": len(layout_tables)},
        )

    attr_hits = sum(len(soup.select(f"[{attr}]")) for attr in _OBSOLETE_ATTRS)
    s.signal("presentational_attributes", attr_hits)
    if attr_hits > 8:
        s.deduct(
            8, "presentational_attributes", "low",
            f"{attr_hits} presentational HTML attributes (bgcolor/align/cellpadding...).",
            {"count": attr_hits},
        )

    legacy_libs = sorted({m.group(0) for m in _LEGACY_LIBRARY_RE.finditer(html or "")})
    s.signal("legacy_libraries", legacy_libs)
    if legacy_libs:
        s.deduct(
            min(12.0, 4 + len(legacy_libs) * 3), "legacy_frontend_libraries", "medium",
            f"Legacy front-end dependencies detected: {', '.join(legacy_libs[:4])}.",
            {"libraries": legacy_libs[:6]},
        )

    generator = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    generator_value = str(generator.get("content") or "") if generator else ""
    if generator_value and _BUILDER_RE.search(generator_value):
        s.signal("legacy_generator", generator_value)
        s.deduct(
            8, "legacy_site_builder", "medium",
            f"Page was produced by a legacy site builder ({generator_value}).",
            {"generator": generator_value},
        )

    if has_browser and render is not None:
        failures: list[dict[str, Any]] = []
        console_errors: list[str] = []
        page_errors: list[str] = []
        for viewport in (render.desktop, render.mobile):
            if viewport is None:
                continue
            failures.extend(viewport.failed_requests)
            console_errors.extend(viewport.console_errors)
            page_errors.extend(viewport.page_errors)
        unique_failures = {f["url"]: f for f in failures}
        s.signal("failed_requests", len(unique_failures))
        s.signal("console_errors", len(console_errors))
        s.signal("javascript_errors", len(page_errors))
        if unique_failures:
            s.deduct(
                min(20.0, 5 + len(unique_failures) * 2.0), "broken_assets", "high",
                f"{len(unique_failures)} resource request(s) failed while loading the page.",
                {"examples": [f["url"] for f in list(unique_failures.values())[:4]]},
            )
        if page_errors:
            s.deduct(
                min(12.0, 4 + len(page_errors) * 2.0), "javascript_errors", "medium",
                f"{len(page_errors)} uncaught JavaScript error(s) on load.",
                {"examples": page_errors[:3]},
            )
        desktop_metrics = render.desktop.metrics if render.desktop else {}
        broken_images = int(desktop_metrics.get("broken_images") or 0)
        s.signal("broken_images", broken_images)
        if broken_images:
            s.deduct(
                min(12.0, 3 + broken_images * 2.5), "broken_images", "high",
                f"{broken_images} image(s) fail to load.",
                {"count": broken_images},
            )
        s.signal("flex_or_grid_elements", desktop_metrics.get("flex_or_grid"))
        if int(desktop_metrics.get("flex_or_grid") or 0) > 3:
            s.credit(5)

    if not soup.find("html", attrs={"lang": True}):
        s.deduct(3, "no_lang_attribute", "low", "The <html> element has no lang attribute.")
    if not soup.find("meta", attrs={"charset": True}) and not soup.find(
        "meta", attrs={"http-equiv": re.compile(r"content-type", re.I)}
    ):
        s.deduct(3, "no_charset", "low", "No character encoding is declared.")
    doctype_ok = (html or "").lstrip()[:120].lower().startswith("<!doctype html>")
    s.signal("html5_doctype", doctype_ok)
    if not doctype_ok:
        s.deduct(4, "no_html5_doctype", "low", "The page does not use the HTML5 doctype.")

    return s.result()


# ---------------------------------------------------------------------------
# CONVERSION / LOCAL BUSINESS UX
# ---------------------------------------------------------------------------
def _score_conversion(
    soup: BeautifulSoup,
    desktop: dict[str, Any],
    mobile: dict[str, Any],
    has_browser: bool,
) -> CategoryScore:
    s = _Scorer("conversion", start=100.0)
    text = soup.get_text(" ", strip=True)

    tel_links = len(soup.select('a[href^="tel:"]'))
    if has_browser:
        tel_links = max(tel_links, int(desktop.get("tel_links") or 0))
    phone_visible = bool(
        re.search(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\d)", text)
    )
    s.signal("phone_visible", phone_visible)
    s.signal("tel_link", bool(tel_links))
    if not phone_visible:
        s.deduct(18, "no_visible_phone", "high", "No phone number appears anywhere on the page.")
    if not tel_links:
        s.deduct(
            16, "no_click_to_call", "high",
            "The phone number is not a tel: link, so phone users cannot tap to call.",
        )

    cta_above_fold = bool(desktop.get("cta_above_fold")) if has_browser else None
    cta_anywhere = bool(desktop.get("cta_anywhere")) if has_browser else None
    if not has_browser:
        cta_words = re.compile(
            r"(book|schedule|appointment|request|contact|consult|new patient)", re.I
        )
        cta_anywhere = any(
            cta_words.search(a.get_text(" ", strip=True) or "") for a in soup.find_all("a")
        )
        cta_above_fold = None
    s.signal("cta_above_fold", cta_above_fold)
    s.signal("cta_anywhere", cta_anywhere)
    if cta_above_fold is False:
        s.deduct(
            15, "no_cta_above_fold", "high",
            "No prominent booking/contact call to action is visible without scrolling.",
        )
    if cta_anywhere is False:
        s.deduct(10, "no_cta", "high", "No booking or contact call to action was found at all.")

    forms = len(soup.find_all("form"))
    if has_browser:
        forms = max(forms, int(desktop.get("forms") or 0))
    s.signal("contact_form", bool(forms))
    if not forms:
        s.deduct(
            10, "no_contact_form", "medium",
            "There is no contact or appointment-request form.",
        )

    mailto = len(soup.select('a[href^="mailto:"]'))
    s.signal("mailto_link", bool(mailto))

    h1_count = len(soup.find_all("h1"))
    h2_count = len(soup.find_all("h2"))
    s.signal("h1_count", h1_count)
    s.signal("h2_count", h2_count)
    if h1_count == 0:
        s.deduct(8, "no_h1", "medium", "The page has no <h1>, so its main heading is unclear.")
    elif h1_count > 3:
        s.deduct(4, "multiple_h1", "low", f"The page has {h1_count} <h1> elements.")
    if h2_count == 0:
        s.deduct(4, "flat_heading_structure", "low", "No <h2> subheadings: hierarchy is flat.")

    address_like = bool(
        re.search(r"\d{1,6}\s+[A-Z][A-Za-z0-9.'\- ]{3,40}(St|Ave|Rd|Blvd|Dr|Ln|Way|Ct|Pkwy|Hwy)\b", text)
        or re.search(r"[A-Za-z]+,\s*[A-Z]{2}\s*\d{5}", text)
    )
    s.signal("address_visible", address_like)
    if not address_like:
        s.deduct(8, "no_address", "medium", "No street address or city/state/ZIP is shown.")

    hours_like = bool(re.search(r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*[-–—:]", text, re.I))
    s.signal("hours_visible", hours_like)
    if not hours_like:
        s.deduct(5, "no_hours", "low", "Opening hours are not published on the page.")

    services_like = bool(
        re.search(
            r"(clean|exam|filling|crown|implant|whiten|veneer|root canal|denture|"
            r"orthodont|invisalign|extraction|hygien)",
            text,
            re.I,
        )
    )
    s.signal("services_described", services_like)
    if not services_like:
        s.deduct(6, "no_services_described", "medium", "No services are described on the page.")

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    s.signal("title_length", len(title))
    if not title:
        s.deduct(6, "no_title", "medium", "The page has no <title>.")
    elif len(title) < 15:
        s.deduct(3, "thin_title", "low", f"The page title is very short ({title!r}).")

    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    s.signal("meta_description", bool(meta_desc and meta_desc.get("content")))
    if not (meta_desc and meta_desc.get("content")):
        s.deduct(4, "no_meta_description", "low", "There is no meta description for search results.")

    if has_browser and mobile:
        if not mobile.get("tel_links"):
            s.deduct(
                6, "no_mobile_click_to_call", "medium",
                "No tap-to-call link is present in the mobile rendering.",
            )
        s.signal("mobile_cta_above_fold", bool(mobile.get("cta_above_fold")))

    semantic = sum(len(soup.find_all(tag)) for tag in ("header", "nav", "main", "section", "footer", "article"))
    s.signal("semantic_landmarks", semantic)
    if semantic == 0:
        s.deduct(5, "no_semantic_landmarks", "low", "No semantic landmark elements are used.")

    return s.result()


# ---------------------------------------------------------------------------
# PERFORMANCE
# ---------------------------------------------------------------------------
def _score_performance(
    desktop: dict[str, Any],
    lighthouse: dict[str, Any] | None,
    has_browser: bool,
    notes: list[str],
) -> CategoryScore:
    s = _Scorer("performance", start=100.0)

    if lighthouse and isinstance(lighthouse.get("performance_score"), int | float):
        value = float(lighthouse["performance_score"])
        value = value * 100 if value <= 1 else value
        s.signal("lighthouse_performance", round(value, 1))
        s.score = _clamp(value)
        if value < 50:
            s.findings.append(
                Finding(
                    category="performance",
                    code="slow_lighthouse",
                    severity="high",
                    evidence=f"Lighthouse performance score is {value:.0f}/100.",
                    detail={"lighthouse": value},
                )
            )
        return s.result()

    if not has_browser or not desktop:
        s.signal("measured", False)
        notes.append("performance not measured: no browser metrics available")
        s.score = 60.0  # neutral, so an unmeasured site is neither rewarded nor punished
        return s.result()

    timing = desktop.get("timing") or {}
    paint = desktop.get("paint") or {}
    load_ms = float(timing.get("load_event_ms") or 0)
    dcl_ms = float(timing.get("dom_content_loaded_ms") or 0)
    fcp_ms = float(paint.get("first-contentful-paint") or 0)
    transfer = int(desktop.get("total_transfer_bytes") or 0)
    resources = int(desktop.get("resource_count") or 0)

    s.signal("load_event_ms", round(load_ms))
    s.signal("dom_content_loaded_ms", round(dcl_ms))
    s.signal("first_contentful_paint_ms", round(fcp_ms))
    s.signal("resource_count", resources)
    s.signal("total_transfer_bytes", transfer)
    s.signal("measured", True)

    if fcp_ms > 4000:
        s.deduct(30, "very_slow_first_paint", "high",
                 f"First contentful paint took {fcp_ms / 1000:.1f}s.", {"fcp_ms": fcp_ms})
    elif fcp_ms > 2500:
        s.deduct(15, "slow_first_paint", "medium",
                 f"First contentful paint took {fcp_ms / 1000:.1f}s.", {"fcp_ms": fcp_ms})
    if load_ms > 8000:
        s.deduct(20, "very_slow_load", "high",
                 f"The load event fired after {load_ms / 1000:.1f}s.", {"load_ms": load_ms})
    elif load_ms > 4000:
        s.deduct(10, "slow_load", "medium",
                 f"The load event fired after {load_ms / 1000:.1f}s.", {"load_ms": load_ms})
    if dcl_ms > 3000:
        s.deduct(8, "slow_dom_ready", "medium", f"DOM ready took {dcl_ms / 1000:.1f}s.")
    if transfer > 5_000_000:
        s.deduct(15, "very_heavy_page", "high",
                 f"The page transfers {transfer / 1_000_000:.1f}MB.", {"bytes": transfer})
    elif transfer > 2_500_000:
        s.deduct(8, "heavy_page", "medium", f"The page transfers {transfer / 1_000_000:.1f}MB.")
    if resources > 120:
        s.deduct(8, "many_requests", "medium", f"The page makes {resources} resource requests.")

    return s.result()

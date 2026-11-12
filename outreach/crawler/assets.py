"""Logo and image discovery, restricted to assets the prospect owns.

Rules:

* only same-registrable-domain assets are ever reused
* the prospect's own logo may come from the header, a favicon, structured
  metadata or a conventional logo selector
* if ownership is uncertain, the asset is not used
* nothing is ever pulled from image search, stock sites or third parties
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, Tag

from outreach.logging import get_logger
from outreach.normalize import join_url, same_site

if TYPE_CHECKING:
    from outreach.crawler.fetch import Fetcher

log = get_logger(__name__)

MAX_ASSET_BYTES = 3_000_000
MIN_USABLE_IMAGE_BYTES = 2_000

_LOGO_SELECTORS: list[tuple[int, str]] = [
    (100, 'header img[class*="logo" i]'),
    (98, 'img[id*="logo" i]'),
    (96, 'img[class*="logo" i]'),
    (94, 'a[class*="logo" i] img'),
    (92, '[class*="brand" i] img'),
    (90, "header a img"),
    (85, "header img"),
    (80, '#header img'),
    (78, '.header img'),
]

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".avif")
_SKIP_IMAGE_PATTERNS = re.compile(
    r"(spacer|pixel|1x1|blank|tracking|analytics|facebook|twitter|instagram|yelp|"
    r"linkedin|youtube|badge|award|visa|mastercard|amex|paypal|powered|cookie|"
    r"sprite|arrow|bullet|icon-)",
    re.I,
)
_STOCK_HOST_PATTERN = re.compile(
    r"(shutterstock|gettyimages|istockphoto|unsplash|pexels|pixabay|adobestock|"
    r"stock\.adobe|dreamstime|123rf|depositphotos)",
    re.I,
)


@dataclass
class DiscoveredAsset:
    url: str
    kind: str  # "logo" | "photo" | "favicon"
    source_url: str
    selector: str = ""
    alt: str = ""
    width: int | None = None
    height: int | None = None
    local_path: str | None = None
    content_type: str | None = None
    bytes: int | None = None
    ownership: str = "prospect_site"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "source_url": self.source_url,
            "selector": self.selector,
            "alt": self.alt,
            "local_path": self.local_path,
            "content_type": self.content_type,
            "bytes": self.bytes,
            "ownership": self.ownership,
        }


@dataclass
class AssetBundle:
    logo: DiscoveredAsset | None = None
    favicon: DiscoveredAsset | None = None
    photos: list[DiscoveredAsset] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logo": self.logo.to_dict() if self.logo else None,
            "favicon": self.favicon.to_dict() if self.favicon else None,
            "photos": [p.to_dict() for p in self.photos],
            "skipped": self.skipped[:40],
        }


def _int_attr(tag: Tag, name: str) -> int | None:
    value = tag.get(name)
    if value is None:
        return None
    try:
        return int(re.sub(r"[^0-9]", "", str(value)) or 0) or None
    except ValueError:
        return None


def find_logo(html: str, page_url: str) -> DiscoveredAsset | None:
    """Best-effort logo detection restricted to the prospect's own domain."""
    soup = BeautifulSoup(html or "", "lxml")

    # 1. Structured metadata (schema.org logo / og:image on the homepage).
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"^og:logo$", re.I)}):
        resolved = join_url(page_url, str(meta.get("content") or ""))
        if resolved and same_site(resolved, page_url):
            return DiscoveredAsset(resolved, "logo", page_url, "meta[og:logo]")
    for element in soup.select('[itemprop="logo" i]'):
        candidate = element.get("content") or element.get("src") or element.get("href")
        resolved = join_url(page_url, str(candidate or ""))
        if resolved and same_site(resolved, page_url):
            return DiscoveredAsset(resolved, "logo", page_url, "itemprop=logo")

    # 2. Conventional header/logo selectors.
    for _weight, selector in _LOGO_SELECTORS:
        try:
            found: Tag | None = soup.select_one(selector)
        except Exception:  # pragma: no cover - malformed selector guard
            continue
        if found is None:
            continue
        element = found
        src = element.get("src") or element.get("data-src") or ""
        resolved = join_url(page_url, str(src))
        if not resolved or not same_site(resolved, page_url):
            continue
        if _SKIP_IMAGE_PATTERNS.search(resolved):
            continue
        return DiscoveredAsset(
            resolved,
            "logo",
            page_url,
            selector,
            alt=str(element.get("alt") or "")[:120],
            width=_int_attr(element, "width"),
            height=_int_attr(element, "height"),
        )

    # 3. Any image whose filename says "logo".
    for img in soup.find_all("img", src=True):
        src = str(img["src"])
        if "logo" not in src.lower():
            continue
        resolved = join_url(page_url, src)
        if resolved and same_site(resolved, page_url):
            return DiscoveredAsset(
                resolved, "logo", page_url, "img[src*=logo]", alt=str(img.get("alt") or "")[:120]
            )
    return None


def find_favicon(html: str, page_url: str) -> DiscoveredAsset | None:
    soup = BeautifulSoup(html or "", "lxml")
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower() if link.get("rel") else ""
        if "icon" not in rel:
            continue
        resolved = join_url(page_url, str(link["href"]))
        if resolved and same_site(resolved, page_url):
            return DiscoveredAsset(resolved, "favicon", page_url, f"link[rel={rel}]")
    default = join_url(page_url, "/favicon.ico")
    return DiscoveredAsset(default, "favicon", page_url, "default") if default else None


def find_business_photos(
    html: str, page_url: str, *, limit: int = 12
) -> tuple[list[DiscoveredAsset], list[dict[str, str]]]:
    """Photographs plausibly owned by the business, plus why others were skipped."""
    soup = BeautifulSoup(html or "", "lxml")
    photos: list[DiscoveredAsset] = []
    skipped: list[dict[str, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        resolved = join_url(page_url, str(src))
        if not resolved:
            continue
        if _STOCK_HOST_PATTERN.search(resolved):
            skipped.append({"url": resolved[:200], "reason": "stock photo host"})
            continue
        if not same_site(resolved, page_url):
            skipped.append({"url": resolved[:200], "reason": "not owned by the prospect"})
            continue
        if _SKIP_IMAGE_PATTERNS.search(resolved):
            skipped.append({"url": resolved[:200], "reason": "decorative or third-party badge"})
            continue
        if not resolved.lower().split("?")[0].endswith(_IMAGE_EXTENSIONS):
            skipped.append({"url": resolved[:200], "reason": "not a recognised image type"})
            continue
        width, height = _int_attr(img, "width"), _int_attr(img, "height")
        if width and height and (width < 200 or height < 150):
            skipped.append({"url": resolved[:200], "reason": "too small to be a hero photo"})
            continue
        if any(p.url == resolved for p in photos):
            continue
        photos.append(
            DiscoveredAsset(
                resolved,
                "photo",
                page_url,
                "img",
                alt=str(img.get("alt") or "")[:160],
                width=width,
                height=height,
            )
        )
        if len(photos) >= limit:
            break
    return photos, skipped


def collect_assets(pages: list[tuple[str, str]], *, photo_limit: int = 12) -> AssetBundle:
    """Run logo/favicon/photo discovery across every crawled page."""
    bundle = AssetBundle()
    for url, html in pages:
        if bundle.logo is None:
            bundle.logo = find_logo(html, url)
        if bundle.favicon is None:
            found = find_favicon(html, url)
            if found is not None and found.selector != "default":
                bundle.favicon = found
        photos, skipped = find_business_photos(html, url, limit=photo_limit)
        bundle.skipped.extend(skipped)
        for photo in photos:
            if len(bundle.photos) >= photo_limit:
                break
            if bundle.logo and photo.url == bundle.logo.url:
                continue
            if not any(existing.url == photo.url for existing in bundle.photos):
                bundle.photos.append(photo)
    return bundle


def download_asset(
    fetcher: Fetcher, asset: DiscoveredAsset, dest_dir: Path
) -> DiscoveredAsset | None:
    """Download one prospect-owned asset into the artifact tree."""
    client = fetcher._client  # deliberate: reuse the polite, throttled client
    try:
        response = client.get(asset.url, timeout=15.0)
    except Exception as exc:
        log.debug("asset download failed", extra={"url": asset.url, "error": str(exc)})
        return None
    if response.status_code >= 400:
        return None
    content = response.content or b""
    if not content or len(content) > MAX_ASSET_BYTES:
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type and not content_type.startswith("image/"):
        return None
    suffix = Path(asset.url.split("?")[0]).suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        suffix = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
            "image/svg+xml": ".svg", "image/gif": ".gif", "image/x-icon": ".ico",
        }.get(content_type, ".img")
    digest = hashlib.sha256(asset.url.encode("utf-8")).hexdigest()[:12]
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{asset.kind}-{digest}{suffix}"
    path.write_bytes(content)
    asset.local_path = str(path)
    asset.content_type = content_type or None
    asset.bytes = len(content)
    return asset

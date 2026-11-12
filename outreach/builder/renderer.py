"""Deterministic Jinja rendering of the StoopCraft templates.

Given a validated ``BusinessContent`` and ``SiteContent`` the renderer produces
a self-contained static site: ``index.html``, ``styles.css``, ``script.js`` and
any prospect-owned images copied into ``assets/``.  No network requests, no
build step, no framework -- so thousands of previews can be hosted for nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from outreach.ai.schemas import BusinessContentSchema, SiteContentSchema
from outreach.builder.content import format_phone_display
from outreach.logging import get_logger
from outreach.normalize import normalize_phone

log = get_logger(__name__)

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_VERSIONS = {"dentist": "1.0.0"}
AGENCY_NAME = "StoopCraft"


@dataclass
class RenderedSite:
    output_dir: Path
    index_path: Path
    template_name: str
    template_version: str
    content_hash: str
    assets_copied: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def index_url(self) -> str:
        return self.index_path.as_uri()


def template_version(template_name: str) -> str:
    return TEMPLATE_VERSIONS.get(template_name, "0.0.0")


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def _monogram(name: str) -> str:
    words = [w for w in re.split(r"[^A-Za-z]+", name or "") if w]
    if not words:
        return "SC"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _format_phone(raw: str) -> str:
    return format_phone_display(raw)


def _address_lines(content: BusinessContentSchema) -> list[str]:
    if not content.address:
        return []
    parts = [p.strip() for p in content.address.split(",") if p.strip()]
    tail = " ".join(p for p in [content.city, content.state, content.postal_code] if p).strip()
    lines = parts[:2] if len(parts) > 1 else parts
    if tail and (not lines or tail.lower() not in " ".join(lines).lower()):
        lines = [*lines, tail]
    return [line for line in lines if line][:3]


def build_context(
    content: BusinessContentSchema,
    site: SiteContentSchema,
    *,
    template_name: str = "dentist",
    asset_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the render context.

    Sections with no factual backing are simply absent from the context, which
    makes them absent from the output -- there is no placeholder path.
    """
    asset_map = asset_map or {}
    phone_display = _format_phone(content.phone) if content.phone else ""
    phone_normalized = normalize_phone(content.phone) if content.phone else None
    address_lines = _address_lines(content)
    locality = ", ".join([p for p in [content.city, content.state] if p])

    services = [
        {
            "name": service.name,
            "description": site.service_blurbs.get(service.name, service.description or ""),
        }
        for service in content.services
        if service.name
    ][:9]

    team = [
        {"name": p.name, "role": p.role, "credentials": p.credentials, "bio": p.bio}
        for p in content.team
        if p.name
    ][:6]

    hours = [{"days": h.days, "hours": h.hours} for h in content.hours if h.days][:8]

    info_strip: list[dict[str, str]] = []
    if phone_normalized:
        info_strip.append(
            {"label": "Call the office", "value": phone_display, "href": f"tel:{phone_normalized}"}
        )
    if hours:
        info_strip.append({"label": "Office hours", "value": f"{hours[0]['days']} {hours[0]['hours']}".strip(), "href": ""})
    if address_lines:
        info_strip.append({"label": "Find us", "value": address_lines[0], "href": ""})
    if content.email and len(info_strip) < 4:
        info_strip.append({"label": "Email", "value": content.email, "href": f"mailto:{content.email}"})

    nav_items: list[dict[str, str]] = []
    if services:
        nav_items.append({"id": "services", "label": site.services_heading or "Services"})
    if site.intro.body:
        nav_items.append({"id": "practice", "label": "The practice"})
    if team:
        nav_items.append({"id": "team", "label": site.team_heading or "Team"})
    if address_lines or hours:
        nav_items.append({"id": "visit", "label": "Visit"})
    nav_items.append({"id": "contact", "label": "Contact"})

    intro_paragraphs = [p.strip() for p in re.split(r"\n{2,}", site.intro.body or "") if p.strip()][:3]

    images = [asset_map.get(u, u) for u in content.image_urls if asset_map.get(u)]
    hero_image = images[0] if images else None
    secondary_image = images[1] if len(images) > 1 else None
    logo = asset_map.get(content.logo_url) if content.logo_url else None

    schema_payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Dentist",
        "name": content.business_name,
    }
    if phone_normalized:
        schema_payload["telephone"] = phone_normalized
    if content.email:
        schema_payload["email"] = content.email
    if address_lines:
        schema_payload["address"] = {
            "@type": "PostalAddress",
            "streetAddress": address_lines[0],
            "addressLocality": content.city or "",
            "addressRegion": content.state or "",
            "postalCode": content.postal_code or "",
        }

    sections = [
        name
        for name, present in (
            ("hero", True),
            ("info_strip", bool(info_strip)),
            ("services", bool(services)),
            ("practice", bool(site.intro.body)),
            ("team", bool(team)),
            ("why", bool(site.why_choose_points)),
            ("contact", True),
            ("visit", bool(address_lines or hours)),
        )
        if present
    ]

    return {
        "template_name": template_name,
        "template_version": template_version(template_name),
        "agency_name": AGENCY_NAME,
        "meta_title": site.meta_title or content.business_name,
        "meta_description": site.meta_description,
        "monogram": _monogram(content.business_name),
        "logo": logo,
        "favicon": asset_map.get("__favicon__"),
        "hero": {
            "headline": site.hero.headline,
            "subheadline": site.hero.subheadline,
            "primary_cta_label": site.hero.primary_cta_label or "Call the office",
            "secondary_cta_label": site.hero.secondary_cta_label,
        },
        "hero_image": hero_image,
        "secondary_image": secondary_image,
        "business": {
            "name": content.business_name,
            "phone_display": phone_display,
            "phone_href": f"tel:{phone_normalized}" if phone_normalized else "",
            "email": content.email,
            "address_lines": address_lines,
            "locality": locality,
        },
        "info_strip": info_strip,
        "nav_items": nav_items,
        "contact_anchor": "services" if services else "contact",
        "services": services,
        "services_heading": site.services_heading or "What we offer",
        "intro": {"heading": site.intro.heading, "body": site.intro.body, "paragraphs": intro_paragraphs},
        "team": team,
        "team_heading": site.team_heading or "Meet the team",
        "why_points": site.why_choose_points,
        "credentials": content.credentials,
        "hours": hours,
        "cta": {"heading": site.cta_section.heading, "body": site.cta_section.body},
        "schema_json": json.dumps(schema_payload, ensure_ascii=False),
        "_sections": sections,
    }


def content_hash(context: dict[str, Any], template_name: str) -> str:
    """Stable hash of (template version + rendered context) for idempotency."""
    payload = json.dumps(
        {k: v for k, v in context.items() if not k.startswith("_")},
        sort_keys=True,
        default=str,
    )
    seed = f"{template_name}:{template_version(template_name)}:{payload}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def render_site(
    content: BusinessContentSchema,
    site: SiteContentSchema,
    output_dir: Path,
    *,
    template_name: str = "dentist",
    source_assets: list[dict[str, Any]] | None = None,
) -> RenderedSite:
    """Render the template into ``output_dir`` as a self-contained static site."""
    output_dir = Path(output_dir)
    template_dir = TEMPLATE_ROOT / template_name
    if not (template_dir / "template.html").is_file():
        raise FileNotFoundError(f"No template named {template_name!r} in {TEMPLATE_ROOT}")

    warnings: list[str] = []
    asset_map, copied = _copy_assets(source_assets or [], output_dir / "assets", warnings)
    context = build_context(content, site, template_name=template_name, asset_map=asset_map)

    env = _env()
    template = env.get_template(f"{template_name}/template.html")
    html = template.render(**context)

    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    for static_name in ("styles.css", "script.js"):
        source = template_dir / static_name
        if source.is_file():
            shutil.copy2(source, output_dir / static_name)
    (output_dir / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8"
    )

    return RenderedSite(
        output_dir=output_dir,
        index_path=index_path,
        template_name=template_name,
        template_version=template_version(template_name),
        content_hash=content_hash(context, template_name),
        assets_copied=copied,
        sections=context["_sections"],
        warnings=warnings,
    )


def _copy_assets(
    source_assets: list[dict[str, Any]], dest_dir: Path, warnings: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Copy prospect-owned assets into the preview, keyed by their source URL.

    Only assets that were already downloaded (and therefore already ownership
    checked) are copied; anything with uncertain provenance is skipped.
    """
    mapping: dict[str, str] = {}
    copied: list[str] = []
    for asset in source_assets:
        if not isinstance(asset, dict):
            continue
        if asset.get("ownership") not in (None, "", "prospect_site"):
            warnings.append(f"skipped asset with uncertain ownership: {asset.get('url')}")
            continue
        local = asset.get("local_path")
        if not local or not Path(local).is_file():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = Path(local).name
        shutil.copy2(local, dest_dir / name)
        relative = f"assets/{name}"
        mapping[str(asset.get("url") or local)] = relative
        mapping[str(local)] = relative
        copied.append(relative)
        if asset.get("kind") == "favicon":
            mapping["__favicon__"] = relative
    return mapping, copied

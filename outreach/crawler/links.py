"""Same-domain link discovery and prioritisation.

We only ever crawl the domain we were pointed at, and we spend the small page
budget on the pages that actually carry business facts and contact details.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from outreach.normalize import join_url, same_site

#: Path fragments worth spending the page budget on, best first.
PRIORITY_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (100, re.compile(r"^/?(contact|contact-us|contactus|get-in-touch|reach-us)/?$", re.I)),
    (95, re.compile(r"(contact|contact-us|contactus)", re.I)),
    (90, re.compile(r"^/?(about|about-us|aboutus|our-practice|the-practice|our-office)/?$", re.I)),
    (85, re.compile(r"(about|our-practice|our-story|who-we-are)", re.I)),
    (80, re.compile(r"^/?(team|our-team|meet-the-team|staff|doctors|our-doctors|dentists)/?$", re.I)),
    (75, re.compile(r"(team|staff|doctor|dentist|provider|meet-)", re.I)),
    (70, re.compile(r"^/?(services|our-services|treatments|procedures)/?$", re.I)),
    (65, re.compile(r"(service|treatment|procedure|dental-care)", re.I)),
    (55, re.compile(r"(appointment|book|schedule|request)", re.I)),
    (50, re.compile(r"(new-patient|patients|insurance|financing)", re.I)),
    (40, re.compile(r"(location|hours|directions|office)", re.I)),
]

#: Never worth a page-budget slot.
SKIP_PATTERNS = re.compile(
    r"(\.pdf$|\.jpe?g$|\.png$|\.gif$|\.svg$|\.webp$|\.zip$|\.docx?$|\.xlsx?$|\.mp4$|\.mp3$"
    r"|/wp-admin|/wp-login|/wp-json|/feed/?$|/tag/|/category/|/author/|/cart|/checkout"
    r"|/privacy|/terms|/sitemap|/blog/20\d\d|/\d{4}/\d{2}/)",
    re.I,
)


@dataclass(frozen=True, order=True)
class CandidateLink:
    priority: int
    url: str
    text: str = ""

    def __post_init__(self) -> None:  # pragma: no cover - dataclass plumbing
        pass


def score_link(url: str, anchor_text: str = "") -> int:
    """Higher is more worth crawling."""
    from urllib.parse import urlparse

    path = urlparse(url).path or "/"
    if path in {"", "/"}:
        return 120
    blob = f"{path} {anchor_text}"
    for weight, pattern in PRIORITY_PATTERNS:
        if pattern.search(path) or pattern.search(anchor_text or ""):
            return weight
    # Shallow paths beat deep ones when nothing else distinguishes them.
    depth = len([p for p in path.split("/") if p])
    return max(5, 30 - depth * 5) if blob else 5


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """All resolvable ``(url, anchor text)`` pairs in a document."""
    soup = BeautifulSoup(html or "", "lxml")
    out: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        resolved = join_url(base_url, str(anchor.get("href") or ""))
        if resolved:
            out.append((resolved, anchor.get_text(" ", strip=True)[:120]))
    return out


def prioritized_same_domain_links(
    html: str,
    base_url: str,
    *,
    limit: int,
    already_seen: set[str] | None = None,
) -> list[CandidateLink]:
    """The next best pages to crawl, restricted to the same registrable domain."""
    seen = set(already_seen or set())
    best: dict[str, CandidateLink] = {}
    for url, text in extract_links(html, base_url):
        if url in seen or url in best:
            continue
        if not same_site(url, base_url):
            continue
        if SKIP_PATTERNS.search(url):
            continue
        best[url] = CandidateLink(priority=score_link(url, text), url=url, text=text)
    ranked = sorted(best.values(), key=lambda c: (-c.priority, len(c.url), c.url))
    return ranked[:limit]

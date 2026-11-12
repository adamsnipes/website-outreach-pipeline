"""URL, domain, email and phone normalisation.

These functions decide what counts as "the same business", "the same page" and
"the same mailbox", so they are deliberately conservative and heavily tested.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

__all__ = [
    "domains_match",
    "extract_registrable_domain",
    "is_private_or_local_url",
    "join_url",
    "normalize_domain",
    "normalize_email",
    "normalize_phone",
    "normalize_site_identity",
    "normalize_url",
    "phone_to_tel_href",
    "same_site",
    "strip_tracking_params",
    "url_slug",
]

_TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "pk_", "_hs")
_TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "igshid", "ref", "referrer",
    "mkt_tok", "vero_id", "s_kwcid", "twclid", "wickedid",
}

# Multi-part public suffixes we actually meet in local-business work.  A full
# PSL would be a dependency for very little extra accuracy here.
_MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "net.nz", "org.nz",
    "co.za", "org.za",
    "com.br", "com.mx", "com.ar", "com.sg", "com.hk", "com.tr", "com.tw",
    "co.jp", "or.jp", "ne.jp", "co.kr", "co.in", "net.in", "org.in",
    "co.il", "com.cn", "net.cn", "org.cn",
}

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9]"
    r"(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

_PRIVATE_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".test")

#: A host is either a dotted name of DNS labels or a bare label (localhost).
_HOSTNAME_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def _ensure_scheme(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        # Reject things that were never URLs (e.g. "info@example.com").
        if "@" in url.split("/")[0]:
            return ""
        return "https://" + url
    return url


def strip_tracking_params(query: str) -> str:
    """Drop analytics parameters so the same page is not crawled twice."""
    if not query:
        return ""
    kept: list[str] = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key in _TRACKING_PARAMS or key.startswith(_TRACKING_PARAM_PREFIXES):
            continue
        kept.append(part)
    return "&".join(kept)


def normalize_url(url: str | None, *, keep_query: bool = True) -> str | None:
    """Canonicalise a URL for storage and comparison.

    Returns ``None`` when the input cannot be understood as an http(s) URL.
    """
    if not url:
        return None
    candidate = _ensure_scheme(str(url))
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            return None
        # .hostname and .port parse lazily and raise on malformed authorities
        # such as "javascript:alert(1)", so they must be inside the guard.
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    with contextlib.suppress(UnicodeError, UnicodeDecodeError):
        host = host.encode("idna").decode("ascii")
    if not _HOSTNAME_RE.match(host):
        return None

    default_port = 443 if parsed.scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"

    # NOTE: the trailing slash is load-bearing.  "/practice/" and "/practice"
    # resolve relative links against different bases, so it is never stripped.
    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)

    query = strip_tracking_params(parsed.query) if keep_query else ""
    return urlunparse(ParseResult(parsed.scheme, netloc, path, "", query, ""))


def join_url(base: str, href: str) -> str | None:
    """Resolve ``href`` against ``base`` and normalise the result."""
    if not href:
        return None
    href = href.strip()
    lowered = href.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "sms:", "data:", "#")):
        return None
    try:
        return normalize_url(urljoin(base, href))
    except ValueError:
        return None


def normalize_domain(value: str | None) -> str | None:
    """Reduce a URL, host or bare domain to a comparable registrable-ish host.

    Drops ``www.`` and any port, lowercases, and keeps the rest intact so that
    ``clinic.example.com`` and ``example.com`` remain distinct businesses.
    """
    if not value:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    if "@" in raw and "://" not in raw:
        raw = raw.rsplit("@", 1)[1]
    if "://" in raw or "/" in raw or raw.startswith("www."):
        try:
            host = urlparse(_ensure_scheme(raw)).hostname or ""
        except ValueError:
            return None
    else:
        host = raw.split("/")[0]
    host = host.split(":")[0].strip().rstrip(".")
    if not host:
        return None
    with contextlib.suppress(UnicodeError, UnicodeDecodeError):
        host = host.encode("idna").decode("ascii")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        # Bare hostnames such as "localhost" are still meaningful for fixtures.
        return host or None
    return host


def extract_registrable_domain(value: str | None) -> str | None:
    """``https://www.blog.example.co.uk/x`` -> ``example.co.uk``."""
    host = normalize_domain(value)
    if not host or "." not in host:
        return host
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host  # a bare IP (fixtures, LAN) has no registrable domain
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_site_identity(url: str | None) -> str | None:
    """The key used to decide "is this the same business's website?".

    For any public host this is just the normalised domain, which is what we
    want: two listings pointing at ``smiledental.com`` are one business.

    Loopback and private hosts are the exception.  Fixture sites, LAN staging
    servers and local previews all live on one host and are distinguished only
    by their first path segment, so for those the segment is folded into the
    key -- otherwise every fixture business would dedupe into a single row.
    """
    if not url:
        return None
    host = normalize_domain(url)
    if not host:
        return None
    if not is_private_or_local_url(url):
        return host
    try:
        parsed = urlparse(_ensure_scheme(str(url)))
    except ValueError:  # pragma: no cover - already filtered by normalize_domain
        return host
    segments = [s for s in (parsed.path or "").split("/") if s]
    return f"{host}/{segments[0]}" if segments else host


def domains_match(a: str | None, b: str | None) -> bool:
    da, db = normalize_domain(a), normalize_domain(b)
    return bool(da and db and da == db)


def same_site(a: str | None, b: str | None) -> bool:
    """True when both URLs belong to the same registrable domain."""
    ra, rb = extract_registrable_domain(a), extract_registrable_domain(b)
    return bool(ra and rb and ra == rb)


def is_private_or_local_url(url: str | None) -> bool:
    """True for anything that must never appear in an outgoing email."""
    if not url:
        return True
    try:
        host = (urlparse(_ensure_scheme(str(url))).hostname or "").lower().rstrip(".")
    except ValueError:
        return True  # unparseable: treat as unsafe to put in an email
    if not host:
        return True
    if host in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    if host.endswith(_PRIVATE_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def normalize_email(value: str | None) -> str | None:
    """Lowercase and validate an email address; ``None`` when not plausible."""
    if not value:
        return None
    raw = str(value).strip()
    if raw.lower().startswith("mailto:"):
        raw = raw[7:]
    raw = raw.split("?", 1)[0].strip().strip("<>").strip().strip(".,;:")
    if not raw or raw.count("@") != 1:
        return None
    local, _, domain = raw.partition("@")
    # Lowercase the whole address, local part included.  RFC 5321 allows
    # case-sensitive local parts, but no mail system anyone will ever prospect
    # actually uses them -- and a case-sensitive suppression list would let an
    # opt-out from "Info@x.com" be defeated by writing to "info@x.com".
    normalized = f"{local.strip().lower()}@{domain.strip().lower().rstrip('.')}"
    if len(normalized) > 320 or len(local) > 64:
        return None
    if not _EMAIL_RE.match(normalized):
        return None
    if ".." in normalized or normalized.startswith(".") or local.endswith("."):
        return None
    return normalized


def normalize_phone(value: str | None, default_country: str = "US") -> str | None:
    """Reduce a phone number to digits (plus leading ``+`` for E.164-ish)."""
    if not value:
        return None
    raw = str(value).strip()
    plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if plus:
        return "+" + digits if 8 <= len(digits) <= 15 else None
    if default_country == "US":
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        return "+1" + digits
    return digits if 8 <= len(digits) <= 15 else None


def phone_to_tel_href(value: str | None) -> str | None:
    normalized = normalize_phone(value)
    return f"tel:{normalized}" if normalized else None


def url_slug(url: str | None) -> str:
    """Filesystem-safe slug for an URL (used for deterministic artifact paths)."""
    if not url:
        return "index"
    try:
        parsed = urlparse(_ensure_scheme(str(url)))
    except ValueError:
        return "index"
    path = (parsed.path or "/").strip("/")
    if not path:
        return "index"
    # Drop a trailing document extension so callers appending their own do not
    # produce "contact.html.html".
    path = re.sub(r"\.(html?|php|aspx?|jsp|cfm)$", "", path, flags=re.I)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-").lower()
    return (slug or "index")[:80]

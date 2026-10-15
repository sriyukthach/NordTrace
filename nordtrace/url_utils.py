"""URL normalization, domain extraction, and categorization utilities."""

import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

NON_HTTP_SCHEMES = ("tel:", "mailto:", "javascript:", "data:", "file:", "ftp:", "about:", "#")


def normalize_url(url: str) -> Optional[str]:
    """Safely normalizes a URL by adding scheme if missing, lowercasing host, removing default ports, trailing slashes, and fragments.

    Args:
        url: Raw URL string.

    Returns:
        Normalized URL string or None if invalid.
    """
    if not url or not isinstance(url, str):
        return None

    cleaned = url.strip()
    if not cleaned:
        return None

    cleaned_lower = cleaned.lower()
    if any(cleaned_lower.startswith(prefix) for prefix in NON_HTTP_SCHEMES):
        return None

    # Case-insensitive scheme check before prepending default scheme
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
        cleaned = "https://" + cleaned

    try:
        parsed = urlparse(cleaned)
    except Exception:
        return None

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None

    if not hostname:
        return None

    hostname = hostname.lower()

    # Handle default ports
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = hostname
    elif port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    # Preserve userinfo if any
    if parsed.username:
        user_info = parsed.username
        if parsed.password:
            user_info += f":{parsed.password}"
        netloc = f"{user_info}@{netloc}"

    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")

    # Strip fragments
    try:
        normalized = urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))
        return normalized
    except Exception:
        return None


def extract_domain(url: str) -> Optional[str]:
    """Extracts the lowercased hostname from a URL."""
    normalized = normalize_url(url)
    if not normalized:
        return None
    try:
        return urlparse(normalized).hostname.lower()
    except Exception:
        return None


def extract_apex_domain(hostname: str) -> str:
    """Extracts base apex domain from hostname (e.g., 'www.equinor.com' -> 'equinor.com')."""
    parts = hostname.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname.lower()


def is_same_domain(url: str, base_url_or_domain: str) -> bool:
    """Checks if url belongs to the same domain or subdomain as base_url_or_domain."""
    candidate_host = extract_domain(url)
    if not candidate_host:
        return False

    if "://" in base_url_or_domain:
        base_host = extract_domain(base_url_or_domain)
    else:
        base_host = base_url_or_domain.lower()

    if not base_host:
        return False

    if candidate_host == base_host:
        return True

    candidate_apex = extract_apex_domain(candidate_host)
    base_apex = extract_apex_domain(base_host)

    return candidate_apex == base_apex


def categorize_url(url: str, title: str = "", text: str = "") -> str:
    """Categorizes a web page into a high-value source category based on URL path semantics, title, and metadata.

    Supported categories:
        - homepage
        - about
        - contact
        - leadership
        - products_services
        - investor
        - annual_reports
        - careers
        - news
        - legal
        - other
    """
    if not url:
        return "other"

    try:
        parsed = urlparse(url)
        path = (parsed.path or "").lower().rstrip("/")
    except Exception:
        path = ""

    # Priority 1: Explicit URL Path Semantics
    if path in ("", "/", "/en", "/no", "/home", "/index.html", "/index.htm", "/default.aspx"):
        return "homepage"

    segments = [s for s in path.split("/") if s]
    last_segment = segments[-1] if segments else ""

    # Exact or prefix segment matching
    if any(k in path for k in ["/careers", "/jobs", "/karriere", "/jobb", "/vacancies", "/work-with-us"]):
        return "careers"

    if any(k in path for k in ["/contact", "/contact-us", "/kontakt", "/kontakt-oss", "/locations", "/offices"]):
        return "contact"

    if any(k in path for k in [
        "/privacy", "/privacy-policy", "/terms", "/terms-and-conditions", "/legal",
        "/cookie-policy", "/cookies", "/site-info", "/accessibility", "/accessibility-statement",
        "/personvern", "/vilkar", "/betingelser"
    ]):
        return "legal"

    if any(k in path for k in ["/leadership", "/board", "/ledelse", "/styre", "/executive", "/management", "/governance"]):
        return "leadership"

    if any(k in path for k in ["/investors", "/investor", "/investor-relations", "/aksjonar", "/shareholders"]):
        return "investor"

    if any(k in path for k in ["/annual-report", "/annual-reports", "/arsrapport", "/financial-reports", "/reports"]):
        return "annual_reports"

    if any(k in path for k in ["/news", "/press", "/presse", "/nyheter", "/media", "/newsroom", "/stories"]):
        return "news"

    if any(k in path for k in ["/products", "/services", "/tjenester", "/what-we-do", "/virksomhet", "/solutions"]):
        return "products_services"

    if any(k in path for k in ["/about", "/about-us", "/om-oss", "/company", "/who-we-are", "/our-story", "/in-brief"]):
        return "about"

    # Priority 2: Page-specific Title
    title_lower = (title or "").lower()
    if title_lower:
        if any(k in title_lower for k in ["privacy policy", "terms and conditions", "legal notice", "cookie policy", "accessibility statement", "site info"]):
            return "legal"
        if any(k in title_lower for k in ["contact us", "contact information", "kontakt oss", "kontaktinformasjon"]):
            return "contact"
        if any(k in title_lower for k in ["career", "careers", "jobs", "jobb", "karriere"]):
            return "careers"
        if any(k in title_lower for k in ["board of directors", "executive committee", "corporate assembly", "management team", "ledelse", "styre"]):
            return "leadership"
        if any(k in title_lower for k in ["investors", "investor relations", "shareholder"]):
            return "investor"
        if any(k in title_lower for k in ["annual report", "financial report", "årsrapport"]):
            return "annual_reports"
        if any(k in title_lower for k in ["about us", "about equinor", "equinor in brief", "om oss", "who we are"]):
            return "about"
        if any(k in title_lower for k in ["press release", "newsroom", "nyheter", "pressemelding"]):
            return "news"
        if any(k in title_lower for k in ["products", "services", "tjenester", "what we do"]):
            return "products_services"

    return "other"

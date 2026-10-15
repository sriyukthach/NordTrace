"""HTML metadata extraction, JSON-LD parsing, regex utilities, and evidence snippet extraction."""

from html.parser import HTMLParser
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

BLACKLIST_EMAILS = {
    "example@example.com", "user@domain.com", "name@domain.com",
    "info@example.com", "email@example.com", "domain@example.com"
}

SOCIAL_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "youtube.com", "instagram.com", "vimeo.com"
)


class MetadataHTMLParser(HTMLParser):
    """HTML parser extracting JSON-LD scripts, meta tags, headings, paragraphs, and social links."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.json_ld_data: List[Dict[str, Any]] = []
        self.meta_tags: Dict[str, str] = {}
        self.headings: List[Tuple[str, str]] = []  # (tag_name, text)
        self.paragraphs: List[str] = []
        self.social_links: List[Tuple[str, str]] = []  # (url, platform)

        self._in_script_ld = False
        self._current_script_text: List[str] = []
        self._current_tag: Optional[str] = None
        self._current_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag_lower = tag.lower()
        attr_dict = dict(attrs)

        if tag_lower == "script" and attr_dict.get("type") == "application/ld+json":
            self._in_script_ld = True
            self._current_script_text = []

        elif tag_lower == "meta":
            name = (attr_dict.get("name") or attr_dict.get("property") or "").lower()
            content = attr_dict.get("content")
            if name and content:
                self.meta_tags[name] = content.strip()

        elif tag_lower in ("h1", "h2", "h3", "p", "a"):
            self._current_tag = tag_lower
            self._current_text = []

            if tag_lower == "a":
                href = attr_dict.get("href")
                if href:
                    href_lower = href.lower()
                    for domain in SOCIAL_DOMAINS:
                        if domain in href_lower:
                            platform = domain.split(".")[0]
                            self.social_links.append((href, platform))
                            break

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower == "script" and self._in_script_ld:
            self._in_script_ld = False
            raw_json = "".join(self._current_script_text).strip()
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    if isinstance(parsed, dict):
                        self.json_ld_data.append(parsed)
                    elif isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                self.json_ld_data.append(item)
                except Exception:
                    pass

        elif tag_lower in ("h1", "h2", "h3", "p"):
            text = " ".join(self._current_text).strip()
            if text:
                if tag_lower in ("h1", "h2", "h3"):
                    self.headings.append((tag_lower, text))
                elif tag_lower == "p":
                    self.paragraphs.append(text)
            self._current_tag = None
            self._current_text = []

    def handle_data(self, data: str):
        if self._in_script_ld:
            self._current_script_text.append(data)
        elif self._current_tag:
            cleaned = data.strip()
            if cleaned:
                self._current_text.append(cleaned)


def extract_metadata(html_text: str, base_url: str) -> MetadataHTMLParser:
    """Parses HTML text and returns MetadataHTMLParser with extracted structured attributes."""
    parser = MetadataHTMLParser(base_url)
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser


def find_evidence_snippet(text: str, keyword: str, max_length: int = 180) -> str:
    """Extracts a concise, supporting evidence text snippet around a keyword match."""
    if not text or not keyword:
        return text[:max_length] if text else ""

    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return text[:max_length].strip()

    start = max(0, idx - 40)
    end = min(len(text), idx + len(keyword) + 120)

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def extract_emails(text: str) -> List[Tuple[str, str]]:
    """Extracts and normalizes valid email addresses with surrounding evidence text."""
    pattern = r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
    matches = re.finditer(pattern, text)

    results: List[Tuple[str, str]] = []
    seen = set()

    for match in matches:
        email = match.group(0).lower().strip()
        if email.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")):
            continue
        if email in BLACKLIST_EMAILS:
            continue
        if email not in seen:
            seen.add(email)
            evidence = find_evidence_snippet(text, email)
            results.append((email, evidence))

    return results


def extract_phone_numbers(text: str) -> List[Tuple[str, str]]:
    """Extracts and normalizes Norwegian phone numbers with surrounding evidence text."""
    # Matches +47 XX XX XX XX or +47XXXXXXXX or 8-digit Norwegian numbers with spacing
    pattern = r"(?:\+47\s?)?(?:\(?\d{2,3}\)?[\s.-]?)?\d{2,3}[\s.-]?\d{2}[\s.-]?\d{2}"
    matches = re.finditer(pattern, text)

    results: List[Tuple[str, str]] = []
    seen = set()

    for match in matches:
        raw_phone = match.group(0).strip()
        digits = re.sub(r"\D", "", raw_phone)

        # Filter out 9-digit org numbers, dates, 4-digit postal codes
        if len(digits) in (4, 9):
            continue
        if len(digits) == 8 or (len(digits) == 10 and digits.startswith("47")):
            # Format normalized phone number
            if digits.startswith("47") and len(digits) == 10:
                norm_phone = f"+47 {digits[2:4]} {digits[4:6]} {digits[6:8]} {digits[8:10]}"
            elif len(digits) == 8:
                norm_phone = f"+47 {digits[0:2]} {digits[2:4]} {digits[4:6]} {digits[6:8]}"
            else:
                norm_phone = raw_phone

            if norm_phone not in seen:
                seen.add(norm_phone)
                evidence = find_evidence_snippet(text, raw_phone)
                results.append((norm_phone, evidence))

    return results

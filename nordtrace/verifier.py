"""HTML parsing, link extraction, and entity matching verifier engine."""

from html.parser import HTMLParser
import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from nordtrace.url_utils import is_same_domain, normalize_url
from nordtrace.validator import WEIGHTS, validate_org_nr


class SimpleHTMLParser(HTMLParser):
    """Lightweight HTML parser extracting page title, hyperlinks, and plain text content."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title: Optional[str] = None
        self.links: List[Tuple[str, str]] = []  # (href, text)
        self.text_parts: List[str] = []

        self._in_title = False
        self._in_script_or_style = False
        self._current_link_href: Optional[str] = None
        self._current_link_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        tag_lower = tag.lower()
        if tag_lower in ("script", "style"):
            self._in_script_or_style = True
        elif tag_lower == "title":
            self._in_title = True
        elif tag_lower == "a":
            attr_dict = dict(attrs)
            href = attr_dict.get("href")
            if href:
                self._current_link_href = href
                self._current_link_text = []

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if tag_lower in ("script", "style"):
            self._in_script_or_style = False
        elif tag_lower == "title":
            self._in_title = False
        elif tag_lower == "a":
            if self._current_link_href:
                full_url = urljoin(self.base_url, self._current_link_href)
                link_text = " ".join(self._current_link_text).strip()
                self.links.append((full_url, link_text))
                self._current_link_href = None
                self._current_link_text = []

    def handle_data(self, data: str):
        if self._in_script_or_style:
            return
        cleaned = data.strip()
        if not cleaned:
            return
        if self._in_title:
            if self.title is None:
                self.title = cleaned
            else:
                self.title += " " + cleaned
        else:
            self.text_parts.append(cleaned)
            if self._current_link_href is not None:
                self._current_link_text.append(cleaned)

    def get_full_text(self) -> str:
        return " ".join(self.text_parts)


def parse_html(html_text: str, base_url: str) -> Tuple[Optional[str], str, List[Tuple[str, str]]]:
    """Parses HTML string into title, plain text, and extracted hyperlinks.

    Returns:
        Tuple of (title, full_text, list_of_links_with_text)
    """
    parser = SimpleHTMLParser(base_url)
    try:
        parser.feed(html_text)
    except Exception:
        pass
    title = parser.title.strip() if parser.title else None
    return title, parser.get_full_text(), parser.links


def evaluate_page(
    target_org_nr: str,
    legal_name: str,
    official_domain: str,
    url: str,
    status_code: int,
    page_title: Optional[str],
    page_text: str,
    is_seed_url: bool = False,
) -> Tuple[str, str]:
    """Evaluates page content and URL signals to assign a verification status and rationale.

    Statuses:
        VERIFIED: Strong official-source evidence.
        PROBABLE: Likely relevant but not conclusively verified.
        REJECTED: Wrong company, external domain, or non-200 HTTP response.

    Returns:
        Tuple of (verification_status, explanation)
    """
    # Reject non-200 HTTP responses
    if status_code != 200:
        return "REJECTED", f"HTTP request returned non-200 status code ({status_code})."

    # Reject external domains
    if not is_same_domain(url, official_domain):
        return "REJECTED", f"URL domain does not match verified official domain '{official_domain}'."

    combined_text = f"{page_title or ''} {page_text}".lower()
    norm_org_nr = target_org_nr.replace(" ", "").replace("-", "")

    # Check for conflicting valid Norwegian org numbers
    found_org_nrs = re.findall(r"\b\d{9}\b", combined_text)
    competing_valid_org_nrs = []
    for candidate in found_org_nrs:
        if candidate != norm_org_nr:
            try:
                validate_org_nr(candidate)
                competing_valid_org_nrs.append(candidate)
            except Exception:
                pass

    if competing_valid_org_nrs and norm_org_nr not in found_org_nrs:
        return "REJECTED", f"Page references a competing valid Norwegian organisation number ({competing_valid_org_nrs[0]})."

    # Strong evidence checks
    if norm_org_nr in combined_text or norm_org_nr in url:
        return "VERIFIED", f"Page explicitly includes target organisation number ({target_org_nr}) on official domain."

    legal_name_clean = legal_name.lower().strip()
    if legal_name_clean in combined_text:
        return "VERIFIED", f"Page contains official legal company name '{legal_name}' on official domain."

    # First word of legal name (e.g. 'equinor' in 'EQUINOR ASA')
    legal_name_base = legal_name_clean.split()[0] if legal_name_clean else ""
    if len(legal_name_base) >= 4 and legal_name_base in combined_text:
        return "VERIFIED", f"Page contains brand name '{legal_name_base.upper()}' on official domain."

    if is_seed_url:
        return "VERIFIED", f"Official website root entrypoint verified by Brønnøysund Register Centre."

    # Probable evidence
    return "PROBABLE", f"Page hosted on verified official domain '{official_domain}' but lacks explicit organisation number/name text match."

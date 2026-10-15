"""Safe, bounded PDF retriever and parser using PyMuPDF and SSRF protections."""

from datetime import datetime, timezone
import hashlib
from typing import Dict, List, Optional
import pymupdf as fitz
import httpx
from pydantic import BaseModel, Field

from nordtrace.budget import RequestBudget
from nordtrace.exceptions import (
    BrregAPIError,
    BudgetExhaustedError,
    OversizedResponseError,
    SSRFError,
)
from nordtrace.security import validate_url_security

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB
DEFAULT_MAX_PAGES = 35
DEFAULT_USER_AGENT = "NordTrace-Crawler/0.1 (+https://github.com/nordtrace)"


class PDFPageText(BaseModel):
    """Extracted text and structural content from a single PDF page."""

    page_number: int  # 1-indexed
    text: str


class PDFDocument(BaseModel):
    """Result container for a fetched and parsed PDF document."""

    url: str
    final_url: str
    content_hash: str
    num_pages: int
    pages: List[PDFPageText] = Field(default_factory=list)
    retrieved_at: str


class SafePDFRetriever:
    """Bounded, SSRF-safe PDF fetcher and parser enforcing size limits, magic bytes, and page caps."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_PDF_BYTES,
        max_pages: int = DEFAULT_MAX_PAGES,
        user_agent: str = DEFAULT_USER_AGENT,
        budget: Optional[RequestBudget] = None,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.budget = budget
        self._cache: Dict[str, PDFDocument] = {}

    def fetch_and_parse_pdf(self, url: str) -> PDFDocument:
        """Fetches PDF safely, validates PDF magic header, and extracts text up to max_pages.

        Args:
            url: Target PDF URL.

        Returns:
            PDFDocument instance.

        Raises:
            SSRFError: If SSRF check fails.
            BrregAPIError: If HTTP fetch fails or file is not a valid PDF.
            OversizedResponseError: If file size exceeds max_bytes.
        """
        if url in self._cache:
            return self._cache[url]

        current_url = validate_url_security(url)
        retrieved_at = datetime.now(timezone.utc).isoformat()
        headers = {"User-Agent": self.user_agent}

        event_hooks = {"request": [self.budget.create_httpx_hook()]} if self.budget else None
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True, verify=True, event_hooks=event_hooks) as client:
                resp = client.get(current_url, headers=headers)
        except BudgetExhaustedError:
            raise
        except Exception as exc:
            raise BrregAPIError(f"HTTP failure fetching PDF from '{current_url}': {exc}") from exc

        if resp.status_code != 200:
            raise BrregAPIError(f"HTTP status {resp.status_code} returned when fetching PDF from '{current_url}'.")

        raw_bytes = resp.content
        if len(raw_bytes) > self.max_bytes:
            raise OversizedResponseError(
                f"PDF file size ({len(raw_bytes)} bytes) exceeds maximum limit ({self.max_bytes} bytes)."
            )

        # Verify PDF magic header b"%PDF-"
        if not raw_bytes.startswith(b"%PDF-"):
            raise BrregAPIError(f"Target URL '{current_url}' did not return a valid PDF signature (magic bytes mismatch).")

        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Parse with PyMuPDF
        try:
            doc = fitz.open(stream=raw_bytes, filetype="pdf")
            total_pages = doc.page_count
            parsed_pages: List[PDFPageText] = []

            pages_to_process = min(total_pages, self.max_pages)
            for i in range(pages_to_process):
                page = doc.load_page(i)
                page_text = page.get_text("text") or ""
                parsed_pages.append(PDFPageText(page_number=i + 1, text=page_text))

            doc.close()
        except Exception as exc:
            raise BrregAPIError(f"Failed to parse PDF document structure with PyMuPDF: {exc}") from exc

        result = PDFDocument(
            url=url,
            final_url=current_url,
            content_hash=content_hash,
            num_pages=total_pages,
            pages=parsed_pages,
            retrieved_at=retrieved_at,
        )

        self._cache[url] = result
        return result

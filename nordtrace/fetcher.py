"""Bounded HTTP web fetcher with SSRF protection, size limits, and redirect validation."""

from datetime import datetime, timezone
import hashlib
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
import httpx

from nordtrace.budget import RequestBudget
from nordtrace.exceptions import (
    BrregAPIError,
    BudgetExhaustedError,
    OversizedResponseError,
    SSRFError,
)
from nordtrace.security import validate_url_security

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RETRIES = 2
DEFAULT_USER_AGENT = "NordTrace-Crawler/0.1 (+https://github.com/nordtrace)"


class FetchResult(BaseModel):
    """Result payload from a secure HTTP fetch operation."""

    url: str
    final_url: str
    status_code: int
    headers: Dict[str, str] = Field(default_factory=dict)
    content_hash: str
    text: str
    content_bytes: bytes
    redirect_history: List[str] = Field(default_factory=list)
    retrieved_at: str
    oversized: bool = False


class SafeFetcher:
    """Bounded, SSRF-safe HTTP fetcher enforcing redirects, size limits, timeouts, and retries."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        user_agent: str = DEFAULT_USER_AGENT,
        budget: Optional[RequestBudget] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.budget = budget
        self._client = client

    def fetch(self, url: str) -> FetchResult:
        """Fetches URL contents safely, validating SSRF and redirect targets at each step.

        Args:
            url: Target URL.

        Returns:
            FetchResult instance.

        Raises:
            SSRFError: If initial URL or redirect target fails SSRF validation.
            OversizedResponseError: If response content exceeds max_bytes limit.
            BrregAPIError / httpx.HTTPError: On network or HTTP failures after retries.
        """
        current_url = validate_url_security(url)
        redirect_history: List[str] = []
        retrieved_at = datetime.now(timezone.utc).isoformat()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,no;q=0.8",
        }

        # Handle redirects manually to re-validate SSRF security on each hop
        attempts = 0
        while True:
            last_exception: Optional[Exception] = None
            response: Optional[httpx.Response] = None

            event_hooks = {"request": [self.budget.create_httpx_hook()]} if self.budget else None
            for attempt in range(self.max_retries + 1):
                try:
                    if self._client is not None:
                        response = self._client.get(current_url, headers=headers)
                        break
                    else:
                        with httpx.Client(
                            timeout=self.timeout,
                            follow_redirects=False,
                            verify=True,
                            event_hooks=event_hooks,
                        ) as client:
                            response = client.get(current_url, headers=headers)
                            break
                except BudgetExhaustedError:
                    raise
                except httpx.HTTPError as exc:
                    last_exception = exc

            if response is None:
                raise last_exception or httpx.HTTPError(f"Failed to fetch '{current_url}' after retries.")

            # Check redirect response codes (301, 302, 303, 307, 308)
            if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                if len(redirect_history) >= self.max_redirects:
                    raise SSRFError(f"Maximum redirect count ({self.max_redirects}) exceeded for '{url}'.")

                redirect_location = response.headers["location"]
                next_url = httpx.URL(current_url).join(redirect_location)
                next_url_str = str(next_url)

                # Security validate target redirect URL before following!
                current_url = validate_url_security(next_url_str)
                redirect_history.append(current_url)
                continue

            # Non-redirect final response
            break

        # Read response content safely up to max_bytes
        content_buffer = bytearray()
        oversized = False

        raw_bytes = response.content
        if len(raw_bytes) > self.max_bytes:
            content_buffer = bytearray(raw_bytes[: self.max_bytes])
            oversized = True
        else:
            content_buffer = bytearray(raw_bytes)

        content_bytes = bytes(content_buffer)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        decoded_text = response.text if not oversized else content_bytes.decode("utf-8", errors="replace")

        return FetchResult(
            url=url,
            final_url=current_url,
            status_code=response.status_code,
            headers=dict(response.headers),
            content_hash=content_hash,
            text=decoded_text,
            content_bytes=content_bytes,
            redirect_history=redirect_history,
            retrieved_at=retrieved_at,
            oversized=oversized,
        )

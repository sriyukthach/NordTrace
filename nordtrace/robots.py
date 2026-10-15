"""Robots.txt parser and compliance engine."""

import threading
from typing import Dict, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import httpx

from nordtrace.budget import RequestBudget
from nordtrace.exceptions import BudgetExhaustedError
from nordtrace.security import validate_url_security

DEFAULT_USER_AGENT = "NordTrace-Crawler/0.1 (+https://github.com/nordtrace)"


class RobotsCache:
    """Cache and evaluator for domain robots.txt rules."""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 5.0,
        budget: Optional[RequestBudget] = None,
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.budget = budget
        self._parsers: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, url: str) -> bool:
        """Checks whether the given URL is allowed to be crawled according to domain robots.txt rules.

        Args:
            url: Target URL string.

        Returns:
            True if allowed, False if disallowed by robots.txt.
        """
        try:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname.lower()
            port = parsed.port
        except Exception:
            return True

        if not hostname:
            return True

        netloc = hostname if not port else f"{hostname}:{port}"
        origin = f"{scheme}://{netloc}"

        with self._lock:
            if origin in self._parsers:
                parser = self._parsers[origin]
            else:
                robots_url = f"{origin}/robots.txt"
                parser = self._fetch_and_parse_robots(robots_url)
                self._parsers[origin] = parser

        if parser is None:
            return True  # If robots.txt missing or unparseable, default to allowed

        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    def _fetch_and_parse_robots(self, robots_url: str) -> Optional[RobotFileParser]:
        event_hooks = {"request": [self.budget.create_httpx_hook()]} if self.budget else None
        try:
            validate_url_security(robots_url)
            headers = {"User-Agent": self.user_agent}
            with httpx.Client(timeout=self.timeout, follow_redirects=True, event_hooks=event_hooks) as client:
                resp = client.get(robots_url, headers=headers)
                if resp.status_code == 200:
                    rfp = RobotFileParser()
                    rfp.parse(resp.text.splitlines())
                    return rfp
        except BudgetExhaustedError:
            raise
        except Exception:
            pass  # Default to None if robots.txt cannot be fetched or fails validation
        return None

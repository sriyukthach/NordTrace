"""Global HTTP request budget manager enforcing strict outbound request limits across batch operations."""

import threading
from typing import Callable, Optional
import httpx

from nordtrace.exceptions import BudgetExhaustedError

DEFAULT_MAX_REQUESTS = 2000


class RequestBudget:
    """Thread-safe request budget manager tracking and enforcing global outbound HTTP limits.

    Ensures that every outbound HTTP attempt (including API lookups, redirects,
    retries, robots.txt evaluations, and PDF downloads) consumes a budget unit.
    """

    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS, initial_used: int = 0):
        if max_requests <= 0:
            raise ValueError("max_requests must be a positive integer.")
        if initial_used < 0:
            raise ValueError("initial_used cannot be negative.")

        self.max_requests = max_requests
        self.requests_used = initial_used
        self._lock = threading.Lock()

    def consume(self, count: int = 1) -> None:
        """Consumes the specified number of request budget units.

        Args:
            count: Number of requests to account for (defaults to 1).

        Raises:
            BudgetExhaustedError: If consuming exceeds max_requests.
        """
        with self._lock:
            if self.requests_used + count > self.max_requests:
                raise BudgetExhaustedError(
                    f"Global request budget exhausted: attempted {self.requests_used + count} requests (limit {self.max_requests})."
                )
            self.requests_used += count

    def can_request(self, count: int = 1) -> bool:
        """Checks whether the requested number of requests can be made within budget.

        Args:
            count: Number of requests to check.

        Returns:
            True if budget allows, False otherwise.
        """
        with self._lock:
            return (self.requests_used + count) <= self.max_requests

    @property
    def remaining(self) -> int:
        """Returns the number of remaining request budget units."""
        with self._lock:
            return max(0, self.max_requests - self.requests_used)

    @property
    def total_used(self) -> int:
        """Returns the total number of outbound requests consumed so far."""
        with self._lock:
            return self.requests_used

    @property
    def is_exhausted(self) -> bool:
        """Returns True if the budget has reached or exceeded max_requests."""
        with self._lock:
            return self.requests_used >= self.max_requests

    def create_httpx_hook(self) -> Callable[[httpx.Request], None]:
        """Creates an HTTPX request event hook that accounts for every outbound request.

        Returns:
            A hook function suitable for event_hooks={"request": [...]}.
        """
        def _hook(request: httpx.Request) -> None:
            self.consume(1)

        return _hook

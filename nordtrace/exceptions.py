"""Custom exceptions for NordTrace."""


class NordTraceError(Exception):
    """Base exception for all NordTrace errors."""

    pass


class InvalidOrgNumberError(NordTraceError):
    """Raised when a Norwegian organisation number fails validation or checksum check."""

    pass


class CompanyNotFoundError(NordTraceError):
    """Raised when a company is not found in the official register."""

    pass


class BrregAPIError(NordTraceError):
    """Raised when an HTTP error or communication failure occurs with the Brønnøysund API."""

    pass


class MalformedAPIResponseError(NordTraceError):
    """Raised when the API returns unexpected or unparseable payload structure."""

    pass


class SSRFError(NordTraceError):
    """Raised when a URL fails security validation or attempts SSRF (private/loopback/cloud IP)."""

    pass


class RobotsForbiddenError(NordTraceError):
    """Raised when crawling a path disallowed by robots.txt."""

    pass


class OversizedResponseError(NordTraceError):
    """Raised when an HTTP response exceeds configured maximum byte limit."""

    pass


class BudgetExhaustedError(NordTraceError):
    """Raised when the global outbound HTTP request budget is exhausted."""

    pass

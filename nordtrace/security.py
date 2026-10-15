"""Security module for SSRF prevention, IP validation, and safe URL resolution."""

import ipaddress
import socket
from urllib.parse import urlparse

from nordtrace.exceptions import SSRFError

BLOCKED_HOSTNAMES = {
    "localhost",
    "0.0.0.0",
    "127.0.0.1",
    "::1",
    "169.254.169.254",
    "metadata.google.internal",
    "instance-data",
}

# Additional custom CIDR blocks to block if not fully covered by ipaddress built-ins
BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),     # Carrier-grade NAT
    ipaddress.ip_network("192.0.0.0/24"),      # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / AWS / GCP Metadata
]


def is_ip_address(addr_str: str) -> bool:
    """Returns True if string is a valid IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(addr_str)
        return True
    except ValueError:
        return False


def is_private_or_restricted_ip(ip_str: str) -> bool:
    """Checks whether an IP address string belongs to a private, loopback, link-local, or restricted network range."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        raise ValueError(f"String '{ip_str}' is not a valid IP address.")

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return True

    for net in BLOCKED_NETWORKS:
        if ip in net:
            return True

    return False


def validate_url_security(url: str) -> str:
    """Validates URL scheme, hostname, and resolves DNS to ensure target IP is public and safe.

    Args:
        url: Target URL string.

    Returns:
        Validated URL string.

    Raises:
        SSRFError: If scheme is non-http(s), hostname is restricted, or resolved IP is private/internal.
    """
    if not url or not isinstance(url, str):
        raise SSRFError("URL must be a non-empty string.")

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise SSRFError(f"Invalid URL format '{url}': {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFError(f"Rejected URL scheme '{scheme}'. Only HTTP and HTTPS are permitted.")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise SSRFError("URL must specify a hostname.")

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise SSRFError(f"Host '{hostname}' is blocked due to SSRF protection rules.")

    # Direct IP string check if hostname is already an IP address
    if is_ip_address(hostname):
        if is_private_or_restricted_ip(hostname):
            raise SSRFError(f"IP address '{hostname}' is private or restricted.")

    # Resolve DNS and check all returned IP addresses
    port = parsed.port or (80 if scheme == "http" else 443)
    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFError(f"DNS resolution failed for host '{hostname}': {exc}") from exc
    except Exception as exc:
        raise SSRFError(f"Error during DNS resolution for host '{hostname}': {exc}") from exc

    if not addr_info:
        raise SSRFError(f"No IP addresses resolved for host '{hostname}'.")

    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip = sockaddr[0]
        if is_ip_address(ip) and is_private_or_restricted_ip(ip):
            raise SSRFError(
                f"Host '{hostname}' resolved to private or restricted IP address '{ip}' (SSRF blocked)."
            )

    return url

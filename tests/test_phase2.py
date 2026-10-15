"""Comprehensive Phase 2 tests for URL normalization, SSRF protection, Robots.txt, Bounded Fetching, and Source Discovery."""

from unittest.mock import MagicMock, patch
import httpx
import pytest

from nordtrace.discovery import SourceDiscoverer
from nordtrace.exceptions import SSRFError
from nordtrace.fetcher import FetchResult, SafeFetcher
from nordtrace.models import Address, CompanyProfile, CompanyStatus, OrgForm, SourceMetadata
from nordtrace.robots import RobotsCache
from nordtrace.security import validate_url_security
from nordtrace.url_utils import is_same_domain, normalize_url
from nordtrace.verifier import evaluate_page


def test_registry_website_normalization():
    """Test URL normalization across various formats."""
    assert normalize_url("equinor.com") == "https://equinor.com"
    assert normalize_url("http://www.equinor.com/") == "http://www.equinor.com"
    assert normalize_url("HTTPS://WWW.EQUINOR.COM:443/about#team") == "https://www.equinor.com/about"
    assert normalize_url("invalid-url-schema://test") is None
    assert normalize_url("") is None


def test_domain_matching():
    """Test domain and subdomain matching logic."""
    assert is_same_domain("https://www.equinor.com/about", "equinor.com") is True
    assert is_same_domain("https://careers.equinor.com/jobs", "www.equinor.com") is True
    assert is_same_domain("https://facebook.com/equinor", "equinor.com") is False


def test_private_ip_blocking_and_ssrf():
    """Test SSRF protection blocks private, loopback, and cloud metadata IPs."""
    with pytest.raises(SSRFError, match="blocked due to SSRF protection"):
        validate_url_security("http://localhost/admin")

    with pytest.raises(SSRFError, match="blocked due to SSRF protection"):
        validate_url_security("http://127.0.0.1/internal")

    with pytest.raises(SSRFError, match="blocked due to SSRF protection"):
        validate_url_security("http://169.254.169.254/latest/meta-data")

    with pytest.raises(SSRFError, match="blocked due to SSRF protection"):
        validate_url_security("http://metadata.google.internal")


def test_missing_official_website():
    """Test discovery behavior when company has no registered website."""
    company = CompanyProfile(
        org_nr="923609016",
        name="NOWEBSITE ASA",
        website=None,
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    discoverer = SourceDiscoverer()
    result = discoverer.discover_sources(company)

    assert result.official_website is None
    assert result.verification_status == "UNVERIFIED"
    assert len(result.sources) == 0
    assert "No official website registered" in result.summary.warnings[0]


def test_robots_txt_restrictions():
    """Test robots.txt parser disallowing restricted paths."""
    robots_text = "User-agent: *\nDisallow: /secret/\n"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = robots_text

    cache = RobotsCache()
    with patch.object(httpx.Client, "get", return_value=mock_resp):
        with patch("nordtrace.robots.validate_url_security", return_value="https://equinor.com/robots.txt"):
            assert cache.is_allowed("https://equinor.com/about") is True
            assert cache.is_allowed("https://equinor.com/secret/page") is False


def test_redirect_handling_and_content_hashing():
    """Test HTTP redirect handling, final URL tracking, and content hashing."""
    redirect_resp = MagicMock()
    redirect_resp.status_code = 301
    redirect_resp.headers = {"location": "https://www.equinor.com/home"}

    final_resp = MagicMock()
    final_resp.status_code = 200
    final_resp.headers = {"content-type": "text/html"}
    final_resp.content = b"<html><head><title>Equinor Home</title></head><body>Welcome EQUINOR ASA 923609016</body></html>"
    final_resp.text = final_resp.content.decode("utf-8")

    fetcher = SafeFetcher()

    def mock_get(url, **kwargs):
        if "www.equinor.com/home" in url:
            return final_resp
        return redirect_resp

    with patch("nordtrace.fetcher.validate_url_security", side_effect=lambda u: u):
        with patch.object(httpx.Client, "get", side_effect=mock_get):
            res = fetcher.fetch("http://equinor.com")

    assert res.final_url == "https://www.equinor.com/home"
    assert res.redirect_history == ["https://www.equinor.com/home"]
    assert len(res.content_hash) == 64  # SHA-256 hex digest length


def test_oversized_response_truncation():
    """Test response larger than max_bytes is truncated and flagged."""
    large_bytes = b"A" * (100 * 1024)  # 100 KB
    fetcher = SafeFetcher(max_bytes=10 * 1024)  # Limit to 10 KB

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.content = large_bytes
    mock_resp.text = large_bytes[:10 * 1024].decode("utf-8")

    with patch("nordtrace.fetcher.validate_url_security", side_effect=lambda u: u):
        with patch.object(httpx.Client, "get", return_value=mock_resp):
            res = fetcher.fetch("https://equinor.com")

    assert res.oversized is True
    assert len(res.content_bytes) == 10 * 1024


def test_wrong_company_page_rejection():
    """Test page referencing a different valid Norwegian org number is rejected."""
    # 984851006 is DNB BANK ASA org number
    status, msg = evaluate_page(
        target_org_nr="923609016",
        legal_name="EQUINOR ASA",
        official_domain="equinor.com",
        url="https://equinor.com/subsidiary-dnb",
        status_code=200,
        page_title="DNB Info Page",
        page_text="This page belongs to DNB Bank ASA Org nr: 984851006.",
    )
    assert status == "REJECTED"
    assert "competing valid Norwegian organisation number" in msg


def test_crawl_depth_and_page_limits():
    """Test crawler strictly enforces max_pages and max_depth limits."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    # Mock fetcher to return HTML pages with multiple links
    def mock_fetch(url):
        title = f"Title for {url}"
        links_html = "".join([f'<a href="{url}/page{i}">Link {i}</a>' for i in range(5)])
        html = f"<html><head><title>{title}</title></head><body>Welcome EQUINOR ASA {links_html}</body></html>"
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            headers={},
            content_hash="abc",
            text=html,
            content_bytes=html.encode("utf-8"),
            retrieved_at="2026-10-08T20:00:00Z",
        )

    mock_fetcher = MagicMock()
    mock_fetcher.fetch.side_effect = mock_fetch

    mock_robots = MagicMock()
    mock_robots.is_allowed.return_value = True

    discoverer = SourceDiscoverer(max_pages=5, max_depth=2, fetcher=mock_fetcher, robots_cache=mock_robots)
    result = discoverer.discover_sources(company)

    assert len(result.sources) <= 5
    assert result.summary.total_sources_discovered <= 5
    assert result.verification_status == "VERIFIED"

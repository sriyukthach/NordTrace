"""Comprehensive Phase 3 unit tests covering categorization regressions, fact extractions, evidence verification, and CLI execution."""

from unittest.mock import MagicMock, patch
import json
import pytest
from typer.testing import CliRunner

from nordtrace.cli import app
from nordtrace.extractor import IntelligenceExtractor
from nordtrace.extractor_utils import (
    extract_emails,
    extract_metadata,
    extract_phone_numbers,
    find_evidence_snippet,
)
from nordtrace.models import (
    Address,
    CompanyProfile,
    CompanyStatus,
    DiscoveredSource,
    OrgForm,
    SourceMetadata,
)
from nordtrace.url_utils import categorize_url

runner = CliRunner()


def test_source_categorization_equinor_regression():
    """Test Task 1: Source categorization regression on all Equinor URLs."""
    assert categorize_url("https://www.equinor.com") == "homepage"
    assert categorize_url("https://www.equinor.com/careers") == "careers"
    assert categorize_url("https://www.equinor.com/about-us") == "about"
    assert categorize_url("https://www.equinor.com/about-us/contact-us") == "contact"
    assert categorize_url("https://www.equinor.com/about-us/privacy-policy") == "legal"
    assert categorize_url("https://www.equinor.com/about-us/terms-and-conditions") == "legal"
    assert categorize_url("https://www.equinor.com/about-us/site-info") == "legal"
    assert categorize_url("https://www.equinor.com/about-us/accessibility-statement") == "legal"
    assert categorize_url("https://www.equinor.com/about-us/equinor-in-brief") == "about"
    assert categorize_url("https://www.equinor.com/investors") == "investor"


def test_json_ld_description_extraction():
    """Test extraction of company description from JSON-LD Organization block."""
    html = """
    <html>
    <head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Organization",
      "name": "EQUINOR ASA",
      "description": "Equinor is an international energy company committed to long-term value creation in a low-carbon future."
    }
    </script>
    </head>
    <body></body>
    </html>
    """
    parsed = extract_metadata(html, "https://www.equinor.com/about-us")
    assert len(parsed.json_ld_data) == 1
    assert parsed.json_ld_data[0]["description"].startswith("Equinor is an international energy company")


def test_og_description_extraction():
    """Test extraction of Open Graph description metadata."""
    html = """
    <html>
    <head>
    <meta property="og:description" content="Equinor is a broad energy company operating in 30 countries." />
    </head>
    <body></body>
    </html>
    """
    parsed = extract_metadata(html, "https://www.equinor.com")
    assert parsed.meta_tags.get("og:description") == "Equinor is a broad energy company operating in 30 countries."


def test_description_ranking_priority():
    """Test that JSON-LD / OG descriptions on About pages outrank generic paragraphs."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    html_about = """
    <html>
    <head>
    <script type="application/ld+json">
    {"@type": "Organization", "description": "JSON-LD High Priority Description for Equinor ASA."}
    </script>
    </head>
    <body><p>Generic body paragraph text.</p></body>
    </html>
    """

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://www.equinor.com/about-us",
            normalized_url="https://www.equinor.com/about-us",
            source_category="about",
            verification_status="VERIFIED",
            verification_explanation="Verified domain",
            retrieval_timestamp="2026-10-08T20:00:00Z",
            raw_html=html_about,
        )
    ]

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(company, "https://www.equinor.com", sources)

    assert intel.description is not None
    assert "JSON-LD High Priority Description" in intel.description.description
    assert intel.description.confidence == "HIGH"


def test_contact_email_extraction():
    """Test email extraction and blacklist filtering."""
    text = "For corporate inquiries contact press@equinor.com or example@example.com."
    emails = extract_emails(text)

    extracted_addresses = [e[0] for e in emails]
    assert "press@equinor.com" in extracted_addresses
    assert "example@example.com" not in extracted_addresses


def test_phone_number_extraction():
    """Test Norwegian phone number extraction and normalization."""
    text = "Call main switchboard at +47 51 99 00 00 or 51990000 for customer support."
    phones = extract_phone_numbers(text)

    normalized_phones = [p[0] for p in phones]
    assert "+47 51 99 00 00" in normalized_phones


def test_leadership_extraction_and_former_rejection():
    """Test leadership extraction and rejection of former executives."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    html_leadership = """
    <html>
    <head><title>Leadership Team</title></head>
    <body>
    <p>Anders Opedal is President and CEO of Equinor ASA.</p>
    <p>Eldar Sætre is the former CEO who retired in 2020.</p>
    </body>
    </html>
    """

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://www.equinor.com/leadership",
            normalized_url="https://www.equinor.com/leadership",
            source_category="leadership",
            verification_status="VERIFIED",
            verification_explanation="Verified leadership page",
            retrieval_timestamp="2026-10-08T20:00:00Z",
            raw_html=html_leadership,
        )
    ]

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(company, "https://www.equinor.com", sources)

    leader_names = [l.person_name for l in intel.leadership]
    assert "Anders Opedal" in leader_names
    assert "Eldar Sætre" not in leader_names  # Rejected former leader


def test_wrong_company_attribution_rejection():
    """Test that facts from rejected sources are not included."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://external.com/equinor",
            normalized_url="https://external.com/equinor",
            source_category="about",
            verification_status="REJECTED",
            verification_explanation="External domain mismatch",
            retrieval_timestamp="2026-10-08T20:00:00Z",
            raw_html="<html><body><p>Unrelated page content</p></body></html>",
        )
    ]

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(company, "https://www.equinor.com", sources)

    assert intel.description is None
    assert len(intel.contacts) == 0


def test_registry_address_preservation():
    """Test authoritative preservation of Brønnøysund registry business and postal addresses."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        business_address=Address(address_lines=["Forusbeen 50"], postal_code="4035", city="STAVANGER", country="Norge"),
        postal_address=Address(address_lines=["Postboks 8500"], postal_code="4035", city="STAVANGER", country="Norge"),
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(company, "https://www.equinor.com", [])

    assert len(intel.locations) == 2
    reg_office = [loc for loc in intel.locations if loc.location_type == "registered_office"][0]
    assert reg_office.city == "STAVANGER"
    assert reg_office.confidence == "HIGH"
    assert reg_office.verification_status == "VERIFIED"


def test_product_service_extraction():
    """Test product and service heading extraction from verified product pages."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    html_products = """
    <html>
    <head><title>Energy Solutions</title></head>
    <body>
    <h2>Offshore Wind Power</h2>
    <p>Developing large scale offshore wind farms globally.</p>
    <h2>Low Carbon Solutions</h2>
    <p>Carbon capture and storage technology.</p>
    </body>
    </html>
    """

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://www.equinor.com/products",
            normalized_url="https://www.equinor.com/products",
            source_category="products_services",
            verification_status="VERIFIED",
            verification_explanation="Verified products page",
            retrieval_timestamp="2026-10-08T20:00:00Z",
            raw_html=html_products,
        )
    ]

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(company, "https://www.equinor.com", sources)

    product_names = [p.name for p in intel.products_services]
    assert "Offshore Wind Power" in product_names
    assert "Low Carbon Solutions" in product_names


def test_important_link_classification():
    """Test classification and deduplication of discovered important official links."""
    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://www.equinor.com",
            normalized_url="https://www.equinor.com",
            source_category="homepage",
            verification_status="VERIFIED",
            verification_explanation="Verified seed",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        ),
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://www.equinor.com/careers",
            normalized_url="https://www.equinor.com/careers",
            source_category="careers",
            verification_status="VERIFIED",
            verification_explanation="Verified careers link",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        ),
    ]

    extractor = IntelligenceExtractor()
    intel = extractor.extract_intelligence(
        CompanyProfile(org_nr="923609016", name="EQUINOR ASA", status=CompanyStatus(summary="Active"), source=SourceMetadata(source_url="", retrieved_at="")),
        "https://www.equinor.com",
        sources,
    )

    categories = [link.category for link in intel.important_links]
    assert "homepage" in categories
    assert "careers" in categories


def test_evidence_text_correctness_and_confidence():
    """Test supporting evidence snippet generation and confidence assignment."""
    text = "Equinor ASA is headquartered in Stavanger, Norway."
    snippet = find_evidence_snippet(text, "Stavanger")
    assert "Stavanger" in snippet
    assert len(snippet) <= 180


def test_cli_intelligence_command():
    """Test CLI intelligence subcommand output format."""
    sample_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    with patch("nordtrace.cli.BrregClient.fetch_company", return_value=sample_profile):
        with patch("nordtrace.cli.SourceDiscoverer.discover_sources") as mock_discover:
            mock_discover.return_value = MagicMock(official_website="https://www.equinor.com", sources=[])
            result = runner.invoke(app, ["intelligence", "923609016"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["company"]["org_nr"] == "923609016"
    assert "summary" in payload

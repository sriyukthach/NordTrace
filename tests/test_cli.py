"""Tests for NordTrace CLI interface."""

from typer.testing import CliRunner
from unittest.mock import patch
from nordtrace.cli import app
from tests.test_client import SAMPLE_EQUINOR_RESPONSE
from nordtrace.models import CompanyProfile, SourceMetadata, CompanyStatus, OrgForm, Address, IndustryCode

runner = CliRunner()


def test_cli_lookup_success():
    """Test CLI lookup with valid organisation number."""
    sample_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        org_form=OrgForm(code="ASA", description="Allmennaksjeselskap"),
        registration_date="1995-03-12",
        business_address=Address(city="STAVANGER", country="Norge"),
        postal_address=Address(address_lines=["Postboks 8500"], city="STAVANGER"),
        industry_code=IndustryCode(code="06.100", description="Utvinning av råolje"),
        website="www.equinor.com",
        status=CompanyStatus(is_bankrupt=False, summary="Active"),
        source=SourceMetadata(
            source_name="Brønnøysund Register Centre (Enhetsregisteret)",
            source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
            retrieved_at="2026-10-08T20:00:00Z",
        ),
    )

    with patch("nordtrace.cli.BrregClient.fetch_company", return_value=sample_profile):
        result = runner.invoke(app, ["lookup", "923609016"])

    assert result.exit_code == 0
    assert "923609016" in result.stdout
    assert "EQUINOR ASA" in result.stdout
    assert "www.equinor.com" in result.stdout


def test_cli_lookup_invalid_org_nr():
    """Test CLI lookup with invalid organisation number."""
    result = runner.invoke(app, ["lookup", "123456789"])
    assert result.exit_code == 1
    assert "InvalidOrgNumberError" in result.stderr

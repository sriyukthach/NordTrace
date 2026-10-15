"""Tests for BrregClient covering API responses, missing fields, HTTP errors, and malformed responses."""

import httpx
import pytest
from unittest.mock import MagicMock, patch

from nordtrace.client import BrregClient
from nordtrace.exceptions import (
    BrregAPIError,
    CompanyNotFoundError,
    InvalidOrgNumberError,
    MalformedAPIResponseError,
)

SAMPLE_EQUINOR_RESPONSE = {
    "organisasjonsnummer": "923609016",
    "navn": "EQUINOR ASA",
    "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
    "registreringsdatoEnhetsregisteret": "1995-03-12",
    "forretningsadresse": {
        "adresse": ["Forusbeen 50"],
        "postnummer": "4035",
        "poststed": "STAVANGER",
        "kommune": "STAVANGER",
        "kommunenummer": "1103",
        "land": "Norge",
        "landkode": "NO",
    },
    "postadresse": {
        "adresse": ["Postboks 8500"],
        "postnummer": "4035",
        "poststed": "STAVANGER",
        "kommune": "STAVANGER",
        "kommunenummer": "1103",
        "land": "Norge",
        "landkode": "NO",
    },
    "naeringskode1": {"kode": "06.100", "beskrivelse": "Utvinning av råolje"},
    "hjemmeside": "www.equinor.com",
    "konkurs": False,
    "underAvvikling": False,
    "underTvangsavviklingEllerTvangsopplosning": False,
}


def test_fetch_company_success():
    """Test successful company profile extraction."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = SAMPLE_EQUINOR_RESPONSE

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        profile = client.fetch_company("923609016")

    assert profile.org_nr == "923609016"
    assert profile.name == "EQUINOR ASA"
    assert profile.org_form.code == "ASA"
    assert profile.org_form.description == "Allmennaksjeselskap"
    assert profile.registration_date == "1995-03-12"
    assert profile.business_address.city == "STAVANGER"
    assert profile.postal_address.address_lines == ["Postboks 8500"]
    assert profile.industry_code.code == "06.100"
    assert profile.website == "www.equinor.com"
    assert profile.status.is_bankrupt is False
    assert profile.status.summary == "Active"
    assert profile.source.source_name == "Brønnøysund Register Centre (Enhetsregisteret)"
    assert "https://data.brreg.no/enhetsregisteret/api/enheter/923609016" in profile.source.source_url


def test_fetch_company_missing_optional_fields():
    """Test response with missing optional fields (website, addresses, industry code, registration date)."""
    minimal_response = {
        "organisasjonsnummer": "923609016",
        "navn": "MINIMAL ASA",
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = minimal_response

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        profile = client.fetch_company("923609016")

    assert profile.org_nr == "923609016"
    assert profile.name == "MINIMAL ASA"
    assert profile.org_form is None
    assert profile.registration_date is None
    assert profile.business_address is None
    assert profile.postal_address is None
    assert profile.industry_code is None
    assert profile.website is None
    assert profile.status.is_bankrupt is False
    assert profile.status.summary == "Active"


def test_fetch_company_not_found_404():
    """Test 404 response raises CompanyNotFoundError."""
    mock_response = MagicMock()
    mock_response.status_code = 404

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        with pytest.raises(CompanyNotFoundError, match="was not found in Brønnøysund Register Centre"):
            client.fetch_company("923609016")


def test_fetch_company_server_error_500():
    """Test 500 status code raises BrregAPIError."""
    mock_response = MagicMock()
    mock_response.status_code = 500

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        with pytest.raises(BrregAPIError, match="returned HTTP status 500"):
            client.fetch_company("923609016")


def test_fetch_company_timeout():
    """Test request timeout raises BrregAPIError."""
    with patch.object(httpx.Client, "get", side_effect=httpx.TimeoutException("Connection timed out")):
        client = BrregClient()
        with pytest.raises(BrregAPIError, match="timed out while contacting Brønnøysund API"):
            client.fetch_company("923609016")


def test_fetch_company_malformed_json():
    """Test malformed JSON response raises MalformedAPIResponseError."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = ValueError("Invalid JSON")

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        with pytest.raises(MalformedAPIResponseError, match="Failed to parse response body"):
            client.fetch_company("923609016")


def test_fetch_company_missing_required_fields():
    """Test response missing required fields ('navn' or 'organisasjonsnummer') raises MalformedAPIResponseError."""
    incomplete_response = {
        "organisasjonsnummer": "923609016",
        # missing 'navn'
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = incomplete_response

    with patch.object(httpx.Client, "get", return_value=mock_response):
        client = BrregClient()
        with pytest.raises(MalformedAPIResponseError, match="Missing essential fields"):
            client.fetch_company("923609016")


def test_fetch_company_invalid_org_nr_before_http():
    """Test that invalid org number fails before making an HTTP request."""
    client = BrregClient()
    with pytest.raises(InvalidOrgNumberError):
        client.fetch_company("123456789")

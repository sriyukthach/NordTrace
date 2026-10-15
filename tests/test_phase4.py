"""Comprehensive Phase 4 unit tests covering financial source discovery, PDF parsing, numeric normalization, period classification, entity attribution, and CLI execution."""

import json
from unittest.mock import MagicMock, patch
import pytest
from typer.testing import CliRunner

from nordtrace.cli import app
from nordtrace.exceptions import BrregAPIError, OversizedResponseError, SSRFError
from nordtrace.financial_extractor import FinancialExtractor
from nordtrace.financial_utils import (
    calculate_normalized_value,
    detect_result_type,
    detect_scale_and_currency,
    is_evidence_complete,
    match_metric_name,
    parse_financial_number,
)
from nordtrace.models import (
    CompanyProfile,
    CompanyStatus,
    DiscoveredSource,
    SourceMetadata,
)
from nordtrace.pdf_fetcher import PDFDocument, PDFPageText, SafePDFRetriever

runner = CliRunner()


def test_financial_numeric_parsing_formats():
    """Test 5 & 9: Test numeric parsing of spaces, parentheses negative values, and decimals."""
    assert parse_financial_number("(1,250)") == -1250.0
    assert parse_financial_number("(1 250)") == -1250.0
    assert parse_financial_number("12 500") == 12500.0
    assert parse_financial_number("12.5") == 12.5
    assert parse_financial_number("12,5") == 12.5
    assert parse_financial_number("-500.00") == -500.0


def test_scale_normalization_mults():
    """Test 10: Scale normalization for units, thousands, millions, and billions."""
    assert calculate_normalized_value(12.5, "millions") == 12500000.0
    assert calculate_normalized_value(500, "thousands") == 500000.0
    assert calculate_normalized_value(2.5, "billions") == 2500000000.0
    assert calculate_normalized_value(100, "units") == 100.0


def test_currency_and_scale_detection():
    """Test 10 & 11: Scale and currency detection from text context."""
    scale, curr = detect_scale_and_currency("Tall i millioner NOK (Amounts in NOK million)")
    assert scale == "millions"
    assert curr == "NOK"

    scale, curr = detect_scale_and_currency("Amounts in USD billions ($ bn)")
    assert scale == "billions"
    assert curr == "USD"

    scale, curr = detect_scale_and_currency("in kEUR (thousands of EUR)")
    assert scale == "thousands"
    assert curr == "EUR"


def test_metric_name_matching():
    """Test 6, 7 & 8: Metric name matching for revenue, operating profit, net income, assets, equity, liabilities."""
    assert match_metric_name("Total Revenue / Driftsinntekter") == "revenue"
    assert match_metric_name("Operating Profit (Driftsresultat)") == "operating_profit"
    assert match_metric_name("Årsresultat / Profit for the year") == "net_income"
    assert match_metric_name("Sum eiendeler (Total Assets)") == "total_assets"
    assert match_metric_name("Sum egenkapital (Total Equity)") == "total_equity"
    assert match_metric_name("Sum gjeld (Total Liabilities)") == "total_liabilities"


def test_future_year_guidance_and_target_rejection():
    """Task 1 & 2: Future-year guidance and targets MUST NOT be classified as VERIFIED ACTUAL figures."""
    guidance_type = detect_result_type("Net income guidance of USD 4.84 billion for 2027", 2027)
    assert guidance_type == "GUIDANCE"

    target_type = detect_result_type("Target 2028 revenue: USD 15 billion", 2028)
    assert target_type == "TARGET"

    forecast_type = detect_result_type("2025 forecast outlook revenue: 10 billion", 2025)
    assert forecast_type == "FORECAST"

    actual_type = detect_result_type("Audited Financial Statements for the year ended 2025", 2025)
    assert actual_type == "ACTUAL"


def test_evidence_completeness_validation():
    """Task 3: Evidence completeness validation for material attributes."""
    complete = "NOK 12 500 million revenue in 2025"
    assert is_evidence_complete(complete, 12500.0, "millions", 2025, "NOK") is True

    incomplete_no_year = "NOK 12 500 million revenue"
    assert is_evidence_complete(incomplete_no_year, 12500.0, "millions", 2025, "NOK") is False

    incomplete_no_currency = "12 500 million revenue in 2025"
    assert is_evidence_complete(incomplete_no_currency, 12500.0, "millions", 2025, "NOK") is False


def test_pdf_signature_validation_failure():
    """Test 2: Non-PDF signature bytes raise BrregAPIError."""
    retriever = SafePDFRetriever()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"<html><body>Not a PDF</body></html>"

    with patch("nordtrace.pdf_fetcher.validate_url_security", side_effect=lambda u: u):
        with patch("httpx.Client.get", return_value=mock_resp):
            with pytest.raises(BrregAPIError, match="did not return a valid PDF signature"):
                retriever.fetch_and_parse_pdf("https://equinor.com/report.pdf")


def test_pdf_download_size_limit():
    """Test 3: Oversized PDF exceeds max_bytes limit."""
    retriever = SafePDFRetriever(max_bytes=1024)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"%PDF-1.5 " + b"A" * 2000

    with patch("nordtrace.pdf_fetcher.validate_url_security", side_effect=lambda u: u):
        with patch("httpx.Client.get", return_value=mock_resp):
            with pytest.raises(OversizedResponseError, match="PDF file size"):
                retriever.fetch_and_parse_pdf("https://equinor.com/large.pdf")


def test_pdf_extraction_mock():
    """Test 4 & 18: PDF text extraction with page numbers."""
    pdf_bytes = b"%PDF-1.4 header text"
    doc_mock = MagicMock()
    doc_mock.page_count = 2
    page1 = MagicMock()
    page1.get_text.return_value = "Income Statement 2025\nDriftsinntekter 12 500"
    doc_mock.load_page.return_value = page1

    with patch("nordtrace.pdf_fetcher.fitz.open", return_value=doc_mock):
        with patch("nordtrace.pdf_fetcher.validate_url_security", side_effect=lambda u: u):
            with patch("httpx.Client.get", return_value=MagicMock(status_code=200, content=pdf_bytes)):
                retriever = SafePDFRetriever()
                pdf_doc = retriever.fetch_and_parse_pdf("https://equinor.com/report.pdf")

    assert pdf_doc.num_pages == 2
    assert pdf_doc.pages[0].page_number == 1
    assert "Driftsinntekter" in pdf_doc.pages[0].text


def test_financial_table_parsing_revenue():
    """Test 5 & 6: Financial statement table parsing for revenue."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    pdf_doc = PDFDocument(
        url="https://www.equinor.com/annual-report-2025.pdf",
        final_url="https://www.equinor.com/annual-report-2025.pdf",
        content_hash="hash123",
        num_pages=5,
        pages=[
            PDFPageText(
                page_number=3,
                text="Consolidated Income Statement 2025 2024\n(Amounts in NOK million)\nTotal Revenue  12 500  11 000\nOperating Profit  3 200  2 800",
            )
        ],
        retrieved_at="2026-10-08T20:00:00Z",
    )

    mock_retriever = MagicMock()
    mock_retriever.fetch_and_parse_pdf.return_value = pdf_doc

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url=pdf_doc.url,
            normalized_url=pdf_doc.url,
            source_category="annual_reports",
            verification_status="VERIFIED",
            verification_explanation="Verified PDF link",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        )
    ]

    extractor = FinancialExtractor(pdf_retriever=mock_retriever)
    intel = extractor.extract_financials(company, sources)

    assert len(intel.financial_metrics) >= 1
    rev_metric = [m for m in intel.financial_metrics if m.metric_name == "revenue"][0]
    assert rev_metric.reported_value == 12500.0
    assert rev_metric.normalized_value == 12500000000.0
    assert rev_metric.scale == "millions"
    assert rev_metric.currency == "NOK"
    assert rev_metric.fiscal_year == 2025
    assert rev_metric.result_type == "ACTUAL"
    assert rev_metric.verification_status == "VERIFIED"


def test_parent_vs_consolidated_scope():
    """Test 13: Scope determination for parent company vs consolidated group."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    pdf_doc = PDFDocument(
        url="https://www.equinor.com/parent-report-2025.pdf",
        final_url="https://www.equinor.com/parent-report-2025.pdf",
        content_hash="hash456",
        num_pages=3,
        pages=[
            PDFPageText(
                page_number=2,
                text="Parent Company Income Statement (Morselskap) 2025\nAmounts in NOK million\nOperating Profit  1 500",
            )
        ],
        retrieved_at="2026-10-08T20:00:00Z",
    )

    mock_retriever = MagicMock()
    mock_retriever.fetch_and_parse_pdf.return_value = pdf_doc

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url=pdf_doc.url,
            normalized_url=pdf_doc.url,
            source_category="annual_reports",
            verification_status="VERIFIED",
            verification_explanation="Verified link",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        )
    ]

    extractor = FinancialExtractor(pdf_retriever=mock_retriever)
    intel = extractor.extract_financials(company, sources)

    assert len(intel.financial_metrics) == 1
    metric = intel.financial_metrics[0]
    assert metric.reporting_scope == "parent_company"


def test_wrong_company_rejection():
    """Test 14: Sources from rejected external domains are not processed."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url="https://wrongdomain.com/report.pdf",
            normalized_url="https://wrongdomain.com/report.pdf",
            source_category="annual_reports",
            verification_status="REJECTED",
            verification_explanation="External domain mismatch",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        )
    ]

    mock_retriever = MagicMock()
    extractor = FinancialExtractor(pdf_retriever=mock_retriever)
    intel = extractor.extract_financials(company, sources)

    assert len(intel.financial_metrics) == 0
    assert mock_retriever.fetch_and_parse_pdf.call_count == 0


def test_missing_fiscal_year_rejection():
    """Test 16: Statements without identifiable fiscal year are ignored."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    pdf_doc = PDFDocument(
        url="https://www.equinor.com/no-year.pdf",
        final_url="https://www.equinor.com/no-year.pdf",
        content_hash="hash789",
        num_pages=1,
        pages=[
            PDFPageText(
                page_number=1,
                text="Income Statement\nTotal Revenue  12 500",  # No year present
            )
        ],
        retrieved_at="2026-10-08T20:00:00Z",
    )

    mock_retriever = MagicMock()
    mock_retriever.fetch_and_parse_pdf.return_value = pdf_doc

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url=pdf_doc.url,
            normalized_url=pdf_doc.url,
            source_category="annual_reports",
            verification_status="VERIFIED",
            verification_explanation="Verified",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        )
    ]

    extractor = FinancialExtractor(pdf_retriever=mock_retriever)
    intel = extractor.extract_financials(company, sources)

    assert len(intel.financial_metrics) == 0


def test_duplicate_financial_record_removal():
    """Test 19: Duplicate financial metrics are deduplicated."""
    company = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    pdf_doc = PDFDocument(
        url="https://www.equinor.com/report.pdf",
        final_url="https://www.equinor.com/report.pdf",
        content_hash="hashdup",
        num_pages=2,
        pages=[
            PDFPageText(page_number=1, text="Income Statement 2025\nNOK million\nTotal Revenue  12 500"),
            PDFPageText(page_number=2, text="Income Statement 2025\nNOK million\nTotal Revenue  12 500"),
        ],
        retrieved_at="2026-10-08T20:00:00Z",
    )

    mock_retriever = MagicMock()
    mock_retriever.fetch_and_parse_pdf.return_value = pdf_doc

    sources = [
        DiscoveredSource(
            target_org_nr="923609016",
            source_url=pdf_doc.url,
            normalized_url=pdf_doc.url,
            source_category="annual_reports",
            verification_status="VERIFIED",
            verification_explanation="Verified",
            retrieval_timestamp="2026-10-08T20:00:00Z",
        )
    ]

    extractor = FinancialExtractor(pdf_retriever=mock_retriever)
    intel = extractor.extract_financials(company, sources)

    assert len(intel.financial_metrics) == 1


def test_empty_financial_results():
    """Test 24: Returns valid FinancialIntelligence schema when no metrics exist."""
    company = CompanyProfile(
        org_nr="923609016",
        name="NOFINANCIALS ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    extractor = FinancialExtractor()
    intel = extractor.extract_financials(company, [])

    assert intel.company.name == "NOFINANCIALS ASA"
    assert len(intel.financial_metrics) == 0
    assert intel.summary.metrics_extracted == 0


def test_cli_financials_command():
    """Test 23: CLI financials subcommand returns valid JSON stdout."""
    sample_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no/api", retrieved_at="2026-10-08T20:00:00Z"),
    )

    with patch("nordtrace.cli.BrregClient.fetch_company", return_value=sample_profile):
        with patch("nordtrace.cli.SourceDiscoverer.discover_sources") as mock_discover:
            mock_discover.return_value = MagicMock(sources=[])
            result = runner.invoke(app, ["financials", "923609016"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["company"]["org_nr"] == "923609016"
    assert "financial_metrics" in payload
    assert "summary" in payload

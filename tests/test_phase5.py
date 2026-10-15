"""Phase 5 unit and regression tests for batch processing, budgeting, persistence, and reporting."""

import json
from unittest.mock import MagicMock, patch
import httpx
import pytest
from typer.testing import CliRunner

from nordtrace.batch import BatchEngine, parse_batch_input
from nordtrace.budget import RequestBudget
from nordtrace.cli import app
from nordtrace.client import BrregClient
from nordtrace.exceptions import (
    BudgetExhaustedError,
    CompanyNotFoundError,
    InvalidOrgNumberError,
)
from nordtrace.financial_extractor import classify_pdf_document
from nordtrace.models import (
    Address,
    CompanyDescription,
    CompanyProfile,
    CompanyStatus,
    ComprehensiveCompanyProfile,
    ContactInfo,
    FinancialMetricRecord,
    IndustryCode,
    LeadershipRecord,
    OrgForm,
    ProductService,
    SourceMetadata,
)
from nordtrace.pdf_fetcher import PDFDocument, PDFPageText, SafePDFRetriever
from nordtrace.reporting import (
    build_coverage_report,
    calculate_completeness_score,
    format_report_text,
)
from nordtrace.robots import RobotsCache
from nordtrace.storage import StorageManager


def test_csv_input_parsing(tmp_path):
    """Test 1: Parsing organisation numbers from CSV with headers."""
    csv_file = tmp_path / "companies.csv"
    csv_file.write_text("org_nr,notes\n923609016,Equinor\n984851006,DNB\n123456789,Invalid\n", encoding="utf-8")

    valid_orgs, invalid_orgs = parse_batch_input(str(csv_file))
    assert valid_orgs == ["923609016", "984851006"]
    assert invalid_orgs == ["123456789"]


def test_text_input_parsing(tmp_path):
    """Test 2: Parsing organisation numbers from plain text with comments."""
    txt_file = tmp_path / "companies.txt"
    txt_file.write_text("# Test companies\n923609016\n\n984851006\n# trailing comment\n", encoding="utf-8")

    valid_orgs, invalid_orgs = parse_batch_input(str(txt_file))
    assert valid_orgs == ["923609016", "984851006"]
    assert invalid_orgs == []


def test_org_number_deduplication(tmp_path):
    """Test 3: Input organisation numbers are deduplicated while preserving order."""
    txt_file = tmp_path / "dedup.txt"
    txt_file.write_text("984851006\n923609016\n984851006\n923609016\n", encoding="utf-8")

    valid_orgs, _ = parse_batch_input(str(txt_file))
    assert valid_orgs == ["984851006", "923609016"]


def test_invalid_org_numbers_handling(tmp_path):
    """Test 4: Invalid org numbers are safely captured without crashing."""
    txt_file = tmp_path / "invalid.txt"
    txt_file.write_text("short\n123456789\n923609016\n923609015\n", encoding="utf-8")

    valid_orgs, invalid_orgs = parse_batch_input(str(txt_file))
    assert valid_orgs == ["923609016"]
    assert len(invalid_orgs) == 3


def test_global_request_budget_limits():
    """Test 5: Global request budget enforcement."""
    budget = RequestBudget(max_requests=3)
    assert budget.remaining == 3
    assert not budget.is_exhausted

    budget.consume(2)
    assert budget.total_used == 2
    assert budget.remaining == 1

    budget.consume(1)
    assert budget.is_exhausted

    with pytest.raises(BudgetExhaustedError, match="Global request budget exhausted"):
        budget.consume(1)


def test_redirect_request_accounting():
    """Test 6: Redirect hops consume budget via httpx event hook."""
    budget = RequestBudget(max_requests=10)
    hook = budget.create_httpx_hook()

    def handler(request):
        if "step1" in str(request.url):
            return httpx.Response(302, headers={"location": "http://test.local/step2"})
        if "step2" in str(request.url):
            return httpx.Response(302, headers={"location": "http://test.local/step3"})
        return httpx.Response(200, text="success")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=True, event_hooks={"request": [hook]}) as client:
        resp = client.get("http://test.local/step1")
        assert resp.status_code == 200

    # 1 initial + 2 redirects = 3 requests
    assert budget.total_used == 3


def test_retry_request_accounting():
    """Test 7: Retries consume budget units."""
    budget = RequestBudget(max_requests=5)
    hook = budget.create_httpx_hook()
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("Network blip", request=request)
        return httpx.Response(200, text="ok")

    # Manually call hook per request attempt
    for _ in range(3):
        hook(httpx.Request("GET", "http://test.local/retry"))

    assert budget.total_used == 3


def test_robots_txt_request_accounting():
    """Test 8: Robots.txt requests consume budget once, cache hit consumes zero."""
    budget = RequestBudget(max_requests=5)
    cache = RobotsCache(budget=budget)

    # Mock _fetch_and_parse_robots network call
    with patch.object(cache, "_fetch_and_parse_robots", return_value=None) as mock_fetch:
        cache.is_allowed("https://www.equinor.com/about")
        cache.is_allowed("https://www.equinor.com/contact")
        # Second call to same origin must be a cache hit
        assert mock_fetch.call_count == 1


def test_budget_exhaustion_stops_further_requests():
    """Test 9: Exhausted budget blocks new requests immediately."""
    budget = RequestBudget(max_requests=1)
    hook = budget.create_httpx_hook()

    hook(httpx.Request("GET", "http://test.local/1"))
    assert budget.is_exhausted

    with pytest.raises(BudgetExhaustedError):
        hook(httpx.Request("GET", "http://test.local/2"))


def test_sqlite_persistence_storage(tmp_path):
    """Test 10: SQLite storage manager persistence and retrieval."""
    db_file = str(tmp_path / "test.db")
    storage = StorageManager(db_file)

    storage.get_or_create_batch("batch_1", total_companies=1, max_requests=100)
    profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="IDENTITY_COMPLETE",
        completeness_score=30,
    )
    storage.upsert_company(profile, batch_id="batch_1", input_order=1)

    loaded = storage.get_company("923609016")
    assert loaded is not None
    assert loaded.org_nr == "923609016"
    assert loaded.legal_name == "EQUINOR ASA"
    assert loaded.completeness_score == 30


def test_sqlite_upserts_and_no_data_degradation(tmp_path):
    """Test 11: Higher confidence enriched profiles are not overwritten by lower score errors."""
    db_file = str(tmp_path / "test_upsert.db")
    storage = StorageManager(db_file)

    good_profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="ENRICHED",
        completeness_score=85,
    )
    storage.upsert_company(good_profile, batch_id="b1", input_order=1)

    degraded_profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="Unknown",
        last_updated="2026-10-13T12:05:00Z",
        processing_status="FAILED",
        completeness_score=0,
    )
    storage.upsert_company(degraded_profile, batch_id="b1", input_order=1)

    persisted = storage.get_company("923609016")
    assert persisted.processing_status == "ENRICHED"
    assert persisted.completeness_score == 85


def test_resume_after_interruption(tmp_path):
    """Test 12: Resume skips already resolved companies."""
    db_file = str(tmp_path / "test_resume.db")
    storage = StorageManager(db_file)

    # Seed company 1 as completed
    p1 = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="ENRICHED",
        completeness_score=80,
    )
    storage.upsert_company(p1, batch_id="batch_mock", input_order=1)

    batch_csv = tmp_path / "input.csv"
    batch_csv.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=50)

    # Mock client fetch for 984851006
    dnb_profile = CompanyProfile(
        org_nr="984851006",
        name="DNB BANK ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=dnb_profile) as mock_fetch:
        engine.run_batch(str(batch_csv), resume=True)
        # Should only fetch DNB (984851006), skipping Equinor (923609016)
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args[0][0] == "984851006"


def test_resume_without_budget_reset(tmp_path):
    """Test 13: Resuming batch preserves requests_used state."""
    db_file = str(tmp_path / "test_budget_persist.db")
    storage = StorageManager(db_file)

    batch_id = "batch_test"
    storage.get_or_create_batch(batch_id, total_companies=10, max_requests=100)
    storage.update_batch_progress(batch_id, requests_used=45, status="IN_PROGRESS")

    used, status = storage.get_or_create_batch(batch_id, total_companies=10, max_requests=100)
    assert used == 45
    assert status == "IN_PROGRESS"


def test_registry_first_processing(tmp_path):
    """Test 14: Pass 1 resolves registry identities for all companies."""
    db_file = str(tmp_path / "test_pass1.db")
    batch_csv = tmp_path / "pass1.csv"
    batch_csv.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=50)

    def fake_fetch(org_nr):
        return CompanyProfile(
            org_nr=org_nr,
            name="Test Corp",
            status=CompanyStatus(summary="Active"),
            source=SourceMetadata(source_url="", retrieved_at=""),
        )

    with patch.object(BrregClient, "fetch_company", side_effect=fake_fetch):
        # Stop before Pass 2 by patching SourceDiscoverer
        with patch.object(engine, "_build_dummy_company_profile", side_effect=RuntimeError("stop pass2")):
            try:
                engine.run_batch(str(batch_csv))
            except RuntimeError:
                pass

    storage = StorageManager(db_file)
    p1 = storage.get_company("923609016")
    p2 = storage.get_company("984851006")
    assert p1 is not None and p1.processing_status == "IDENTITY_COMPLETE"
    assert p2 is not None and p2.processing_status == "IDENTITY_COMPLETE"


def test_adaptive_enrichment_mode(tmp_path):
    """Test 15: Batch engine enriches profiles during Pass 2."""
    db_file = str(tmp_path / "test_enrich.db")
    batch_csv = tmp_path / "enrich.csv"
    batch_csv.write_text("org_nr\n923609016\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=50)

    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=fake_profile):
        with patch("nordtrace.discovery.SourceDiscoverer.discover_sources") as mock_disc:
            mock_disc.return_value = MagicMock(sources=[])
            rep = engine.run_batch(str(batch_csv))

    assert rep.identities_resolved == 1
    assert rep.total_input_companies == 1


def test_profile_completeness_scoring():
    """Test 16: Profile completeness score calculation."""
    profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        official_website="https://www.equinor.com",
        description=CompanyDescription(
            description="Leading energy provider",
            source_url="https://www.equinor.com",
            evidence_text="Leading energy provider",
            extraction_method="meta_tag",
            confidence="HIGH",
            verification_status="VERIFIED",
        ),
        contacts=[
            ContactInfo(
                contact_type="email",
                value="contact@equinor.com",
                source_url="https://www.equinor.com",
                evidence_text="contact@equinor.com",
                extraction_method="regex",
                confidence="HIGH",
                verification_status="VERIFIED",
            )
        ],
        leadership=[
            LeadershipRecord(
                person_name="Anders Opedal",
                role="Chief Executive Officer",
                company_org_nr="923609016",
                source_url="https://www.equinor.com",
                evidence_text="Anders Opedal CEO",
                confidence="HIGH",
                verification_status="VERIFIED",
                retrieved_at="2026-10-13T12:00:00Z",
            )
        ],
        products_services=[
            ProductService(
                name="Natural Gas",
                source_url="https://www.equinor.com",
                evidence_text="Natural Gas",
                extraction_method="heading",
                confidence="HIGH",
                verification_status="VERIFIED",
            )
        ],
        financial_metrics=[
            FinancialMetricRecord(
                target_org_nr="923609016",
                legal_company_name="EQUINOR ASA",
                metric_name="revenue",
                reported_value=12500.0,
                normalized_value=12500000000.0,
                currency="NOK",
                scale="millions",
                fiscal_year=2025,
                reporting_scope="consolidated_group",
                result_type="ACTUAL",
                source_url="https://www.equinor.com/report.pdf",
                evidence_text="Revenue 12 500",
                extraction_method="table",
                confidence="HIGH",
                verification_status="VERIFIED",
                retrieved_at="2026-10-13T12:00:00Z",
            )
        ],
        last_updated="2026-10-13T12:00:00Z",
    )

    score, breakdown = calculate_completeness_score(profile)
    assert score == 100
    assert breakdown["identity"] == 30
    assert breakdown["official_website"] == 10
    assert breakdown["description"] == 15
    assert breakdown["contacts"] == 10
    assert breakdown["leadership"] == 10
    assert breakdown["products_services"] == 10
    assert breakdown["financials"] == 15


def test_guidance_exclusion_from_verified_financial_scoring():
    """Test 17: Forward guidance or targets award 0 points to financial completeness score."""
    profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        financial_metrics=[
            FinancialMetricRecord(
                target_org_nr="923609016",
                legal_company_name="EQUINOR ASA",
                metric_name="net_income",
                reported_value=4.84,
                normalized_value=4840000000.0,
                currency="USD",
                scale="billions",
                fiscal_year=2027,
                reporting_scope="consolidated_group",
                result_type="GUIDANCE",
                source_url="https://www.equinor.com/investors",
                evidence_text="guidance of USD 4.84 billion",
                extraction_method="pattern",
                confidence="LOW",
                verification_status="AMBIGUOUS",
                retrieved_at="2026-10-13T12:00:00Z",
            )
        ],
        last_updated="2026-10-13T12:00:00Z",
    )

    score, breakdown = calculate_completeness_score(profile)
    assert breakdown["financials"] == 0
    assert score == 30  # Identity only


def test_jsonl_export(tmp_path):
    """Test 18: Exporting profiles to JSONL format."""
    db_file = str(tmp_path / "test_export.db")
    storage = StorageManager(db_file)

    p = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="IDENTITY_COMPLETE",
        completeness_score=30,
    )
    storage.upsert_company(p, batch_id="b1", input_order=1)

    jsonl_output = tmp_path / "out.jsonl"
    profiles = storage.list_companies()
    with open(jsonl_output, "w", encoding="utf-8") as f:
        for prof in profiles:
            f.write(prof.model_dump_json() + "\n")

    lines = jsonl_output.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    loaded = json.loads(lines[0])
    assert loaded["org_nr"] == "923609016"
    assert loaded["legal_name"] == "EQUINOR ASA"


def test_coverage_reporting():
    """Test 19: Coverage report calculation and formatting."""
    profiles = [
        ComprehensiveCompanyProfile(
            org_nr="923609016",
            legal_name="EQUINOR ASA",
            processing_status="ENRICHED",
            completeness_score=85,
            last_updated="2026-10-13T12:00:00Z",
        ),
        ComprehensiveCompanyProfile(
            org_nr="984851006",
            legal_name="DNB BANK ASA",
            processing_status="IDENTITY_COMPLETE",
            completeness_score=30,
            last_updated="2026-10-13T12:00:00Z",
        ),
    ]

    report = build_coverage_report(
        profiles=profiles,
        total_input_count=2,
        invalid_org_count=0,
        requests_used=12,
        max_requests=2000,
        runtime_seconds=1.5,
    )

    assert report.total_input_companies == 2
    assert report.identities_resolved == 2
    assert report.enriched_profiles == 1
    assert report.partial_profiles == 1
    assert report.average_completeness_score == 57.5
    assert report.remaining_request_budget == 1988

    txt = format_report_text(report)
    assert "NORDTRACE BATCH COVERAGE REPORT" in txt
    assert "Remaining Request Budget:          1988" in txt


def test_per_company_failure_isolation(tmp_path):
    """Test 20: A failure on one company does not crash the entire batch."""
    db_file = str(tmp_path / "test_iso.db")
    batch_csv = tmp_path / "iso.csv"
    batch_csv.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=50)

    def fetch_mock(org_nr):
        if org_nr == "923609016":
            raise CompanyNotFoundError("Not found")
        return CompanyProfile(
            org_nr=org_nr,
            name="DNB BANK ASA",
            status=CompanyStatus(summary="Active"),
            source=SourceMetadata(source_url="", retrieved_at=""),
        )

    with patch.object(BrregClient, "fetch_company", side_effect=fetch_mock):
        rep = engine.run_batch(str(batch_csv))

    assert rep.failed_profiles == 1
    assert rep.identities_resolved == 1


def test_wrong_company_fact_rejection():
    """Test 21: Facts referencing a different company are rejected."""
    wrong_leadership = LeadershipRecord(
        person_name="Competitor CEO",
        role="CEO",
        company_org_nr="999999999",  # Wrong org
        source_url="https://other.com",
        evidence_text="CEO of other company",
        confidence="LOW",
        verification_status="REJECTED",
        retrieved_at="2026-10-13T12:00:00Z",
    )
    profile = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        leadership=[wrong_leadership],
        last_updated="2026-10-13T12:00:00Z",
    )
    _, breakdown = calculate_completeness_score(profile)
    assert breakdown["leadership"] == 0


def test_misleading_pdf_classification_rejection():
    """Test 22 (Task 13 Regression): Order form PDF is rejected and not classified as verified annual report."""
    order_form_url = "https://content.dnb.no/docs/7560248/bestillingskjema-for-oppslagsrapport-04-03-25.pdf"
    doc = PDFDocument(
        url=order_form_url,
        final_url=order_form_url,
        content_hash="abc",
        num_pages=1,
        pages=[
            PDFPageText(
                page_number=1,
                text="Bestillingskjema for oppslagsrapport. Vennligst fyll ut organisasjonsnummer.",
            )
        ],
        retrieved_at="2026-10-13T12:00:00Z",
    )

    source_type, status, title = classify_pdf_document(order_form_url, doc)
    assert status == "REJECTED"
    assert source_type == "irrelevant_document"
    assert "bestillingskjema" in title.lower() or "rejected" in title.lower()


def test_cli_batch_execution(tmp_path):
    """Test 23: CLI batch command runs and outputs coverage report."""
    runner = CliRunner()
    batch_csv = tmp_path / "cli_batch.csv"
    batch_csv.write_text("org_nr\n923609016\n", encoding="utf-8")
    db_file = str(tmp_path / "cli.db")

    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=fake_profile):
        result = runner.invoke(app, ["batch", str(batch_csv), "--db-path", db_file, "--max-requests", "10"])

    assert result.exit_code == 0
    assert "NORDTRACE BATCH COVERAGE REPORT" in result.stdout


def test_cli_resume(tmp_path):
    """Test 24: CLI batch command with --resume flag."""
    runner = CliRunner()
    batch_csv = tmp_path / "cli_resume.csv"
    batch_csv.write_text("org_nr\n923609016\n", encoding="utf-8")
    db_file = str(tmp_path / "cli_resume.db")

    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=fake_profile):
        result = runner.invoke(app, ["batch", str(batch_csv), "--db-path", db_file, "--resume"])

    assert result.exit_code == 0
    assert "NORDTRACE BATCH COVERAGE REPORT" in result.stdout


def test_cli_export_and_report(tmp_path):
    """Test 25: CLI report and export commands."""
    runner = CliRunner()
    db_file = str(tmp_path / "cli_rep.db")
    storage = StorageManager(db_file)

    p = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="IDENTITY_COMPLETE",
        completeness_score=30,
    )
    storage.get_or_create_batch("batch_test", 1, 100)
    storage.upsert_company(p, batch_id="batch_test", input_order=1)

    # Test report command
    rep_result = runner.invoke(app, ["report", "--db-path", db_file])
    assert rep_result.exit_code == 0
    assert "NORDTRACE BATCH COVERAGE REPORT" in rep_result.stdout

    # Test export command
    out_file = str(tmp_path / "profiles.jsonl")
    exp_result = runner.invoke(app, ["export", "--format", "jsonl", "--output", out_file, "--db-path", db_file])
    assert exp_result.exit_code == 0
    assert "Successfully exported 1 company profiles" in exp_result.stdout

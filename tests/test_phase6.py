"""Phase 6 comprehensive unit and integration tests for performance, accuracy, freshness, and evaluation readiness."""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock, patch
import httpx
import pytest
from typer.testing import CliRunner

from nordtrace.batch import BatchEngine, format_benchmark_text
from nordtrace.budget import RequestBudget
from nordtrace.cli import app
from nordtrace.client import BrregClient
from nordtrace.discovery import SourceDiscoverer
from nordtrace.exceptions import BudgetExhaustedError
from nordtrace.models import (
    Address,
    CompanyDescription,
    CompanyProfile,
    CompanyStatus,
    ComprehensiveCompanyProfile,
    ContactInfo,
    FinancialMetricRecord,
    IndustryCode,
    OrgForm,
    ProductService,
    SourceMetadata,
)
from nordtrace.pdf_fetcher import SafePDFRetriever
from nordtrace.reporting import (
    calculate_completeness_score,
    compute_freshness,
)
from nordtrace.robots import RobotsCache
from nordtrace.storage import StorageManager


# -----------------------------------------------------------------------------
# 1 & 2. Persistence & Budget Calculation Tests
# -----------------------------------------------------------------------------

def test_batch_report_reads_persisted_max_requests(tmp_path):
    """Test 1: Batch report reads actual persisted max_requests from DB (e.g. 20, not hardcoded 2000)."""
    db_file = str(tmp_path / "test_report_budget.db")
    storage = StorageManager(db_file)
    batch_id = "batch_test_20"

    # Create batch record with max_requests = 20 and requests_used = 8
    storage.get_or_create_batch(batch_id=batch_id, total_companies=2, max_requests=20)
    storage.update_batch_progress(batch_id=batch_id, requests_used=8, status="COMPLETED")

    p1 = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="IDENTITY_COMPLETE",
        completeness_score=40,
    )
    storage.upsert_company(p1, batch_id=batch_id, input_order=1)

    runner = CliRunner()
    result = runner.invoke(app, ["report", "--db-path", db_file, "--batch-id", batch_id, "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["total_network_requests"] == 8
    assert data["remaining_request_budget"] == 12  # 20 - 8 = 12, NOT 1992!


def test_remaining_request_calculation(tmp_path):
    """Test 2: Remaining request calculation accurately reflects batch limit."""
    db_file = str(tmp_path / "test_rem_calc.db")
    storage = StorageManager(db_file)
    batch_id = "batch_rem"

    storage.get_or_create_batch(batch_id=batch_id, total_companies=5, max_requests=50)
    storage.update_batch_progress(batch_id=batch_id, requests_used=35, status="IN_PROGRESS")

    runner = CliRunner()
    result = runner.invoke(app, ["report", "--db-path", db_file, "--batch-id", batch_id, "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["remaining_request_budget"] == 15  # 50 - 35 = 15


# -----------------------------------------------------------------------------
# 3, 4, 5. Scalability & Two-Pass Architecture Tests
# -----------------------------------------------------------------------------

def test_two_pass_architecture_two_requests_per_company(tmp_path):
    """Test 3: Two-pass architecture adheres to 2 requests/company average on sample."""
    db_file = str(tmp_path / "test_2pass.db")
    input_file = tmp_path / "sample.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=4)  # 2 companies * 2 requests = 4
    # Run offline benchmark simulation
    res = engine.run_benchmark(str(input_file), sample_limit=2, offline=True)
    assert res["requests_per_company"] <= 2.0
    assert res["projected_requests_1000"] <= 2000
    assert res["request_budget_compliant"] is True


def test_pass1_guarantees_registry_identity_resolution(tmp_path):
    """Test 4: Pass 1 guarantees registry identity resolution for all companies before enrichment."""
    db_file = str(tmp_path / "test_pass1_guarantee.db")
    input_file = tmp_path / "companies.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=2)  # Exactly 2 requests for 2 companies in Pass 1

    def fake_fetch(self, org_nr):
        if self.budget:
            self.budget.consume(1)
        return CompanyProfile(
            org_nr=org_nr,
            name=f"Company {org_nr}",
            status=CompanyStatus(summary="Active"),
            source=SourceMetadata(source_url="", retrieved_at=""),
        )

    with patch.object(BrregClient, "fetch_company", autospec=True, side_effect=fake_fetch):
        report = engine.run_batch(str(input_file))

    assert report.identities_resolved == 2
    assert report.total_network_requests == 2
    # Both companies resolved in Pass 1
    storage = StorageManager(db_file)
    c1 = storage.get_company("923609016")
    c2 = storage.get_company("984851006")
    assert c1.processing_status == "IDENTITY_COMPLETE"
    assert c2.processing_status == "IDENTITY_COMPLETE"


def test_pass2_adapts_to_remaining_headroom(tmp_path):
    """Test 5: Pass 2 halts when remaining request budget is under headroom threshold (<2)."""
    db_file = str(tmp_path / "test_headroom.db")
    input_file = tmp_path / "headroom.csv"
    input_file.write_text("org_nr\n923609016\n", encoding="utf-8")

    # Budget has only 1 request: Pass 1 consumes it, leaving 0 for Pass 2
    engine = BatchEngine(db_path=db_file, max_requests=1)

    def fake_fetch(self, org_nr):
        if self.budget:
            self.budget.consume(1)
        return CompanyProfile(
            org_nr="923609016",
            name="EQUINOR ASA",
            website="https://www.equinor.com",
            status=CompanyStatus(summary="Active"),
            source=SourceMetadata(source_url="", retrieved_at=""),
        )

    with patch.object(BrregClient, "fetch_company", autospec=True, side_effect=fake_fetch):
        report = engine.run_batch(str(input_file))

    # Pass 2 did not crash; identity completed without exceeding 1 request
    assert report.total_network_requests == 1
    assert report.remaining_request_budget == 0


# -----------------------------------------------------------------------------
# 6, 7, 8. Request Budget Accounting Tests
# -----------------------------------------------------------------------------

def test_request_budget_accounts_for_all_client_requests():
    """Test 6: RequestBudget accounts for all client requests."""
    budget = RequestBudget(max_requests=10)
    assert budget.total_used == 0

    def handler(request):
        return httpx.Response(200, json={"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA"})

    transport = httpx.MockTransport(handler)
    hook = budget.create_httpx_hook()
    with httpx.Client(transport=transport, event_hooks={"request": [hook]}) as client:
        resp = client.get("https://data.brreg.no/enhetsregisteret/api/enheter/923609016")
        assert resp.status_code == 200

    assert budget.total_used == 1
    assert budget.remaining == 9


def test_request_budget_accounts_for_redirects_and_retries():
    """Test 7: RequestBudget accounts for redirects and retry attempts."""
    budget = RequestBudget(max_requests=5)
    hook = budget.create_httpx_hook()

    # 1 original + 2 redirects
    hook(httpx.Request("GET", "http://test.local/req1"))
    hook(httpx.Request("GET", "http://test.local/req2"))
    hook(httpx.Request("GET", "http://test.local/req3"))
    assert budget.total_used == 3
    assert budget.remaining == 2


def test_request_budget_accounts_for_robots_txt():
    """Test 8: RequestBudget accounts for robots.txt network fetches and caches correctly."""
    budget = RequestBudget(max_requests=5)
    cache = RobotsCache(budget=budget)

    with patch.object(cache, "_fetch_and_parse_robots", return_value=None) as mock_fetch:
        cache.is_allowed("https://www.equinor.com/page1")
        cache.is_allowed("https://www.equinor.com/page2")
        # Only 1 network fetch for domain
        assert mock_fetch.call_count == 1


# -----------------------------------------------------------------------------
# 9 & 10. Stricter Completeness Scoring (Contacts)
# -----------------------------------------------------------------------------

def test_completeness_scoring_requires_direct_contact():
    """Test 9: Direct contact (email, phone, contact_url) awards full 10 points."""
    p_email = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
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
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_email)
    assert breakdown["contacts"] == 10

    p_phone = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        contacts=[
            ContactInfo(
                contact_type="phone",
                value="+47 51 99 00 00",
                source_url="https://www.equinor.com",
                evidence_text="+47 51 99 00 00",
                extraction_method="regex",
                confidence="HIGH",
                verification_status="VERIFIED",
            )
        ],
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_phone)
    assert breakdown["contacts"] == 10


def test_completeness_scoring_social_only_receives_partial():
    """Test 10: Social media alone receives partial credit (4 pts), NOT full 10 points."""
    p_social = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        contacts=[
            ContactInfo(
                contact_type="social_media",
                value="https://linkedin.com/company/equinor",
                source_url="https://www.equinor.com",
                evidence_text="LinkedIn",
                extraction_method="href",
                confidence="HIGH",
                verification_status="VERIFIED",
            )
        ],
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_social)
    assert breakdown["contacts"] == 4  # Partial points only


# -----------------------------------------------------------------------------
# 11, 12, 13. Product/Service & Description Quality Filtering
# -----------------------------------------------------------------------------

def test_navigation_labels_rejected_from_products():
    """Test 11: Navigation labels ('Explore', 'Connect', 'Useful links') are rejected from products."""
    p = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        products_services=[
            ProductService(
                name="Explore",
                source_url="https://www.equinor.com",
                evidence_text="Explore",
                extraction_method="heading",
                confidence="HIGH",
                verification_status="VERIFIED",
            ),
            ProductService(
                name="Connect",
                source_url="https://www.equinor.com",
                evidence_text="Connect",
                extraction_method="heading",
                confidence="HIGH",
                verification_status="VERIFIED",
            ),
        ],
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p)
    assert breakdown["products_services"] == 0


def test_generic_marketing_slogans_rejected_from_products():
    """Test 12: Generic boilerplate headings ('Useful links', 'Latest news', 'Menu') are rejected."""
    p = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        products_services=[
            ProductService(
                name="Useful links",
                source_url="https://www.equinor.com",
                evidence_text="Useful links",
                extraction_method="heading",
                confidence="HIGH",
                verification_status="VERIFIED",
            ),
            ProductService(
                name="Latest news",
                source_url="https://www.equinor.com",
                evidence_text="Latest news",
                extraction_method="heading",
                confidence="HIGH",
                verification_status="VERIFIED",
            ),
        ],
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p)
    assert breakdown["products_services"] == 0


def test_substantial_description_required_for_points():
    """Test 13: Substantial description (>20 chars, non-boilerplate) required for description points."""
    # Too short
    p_short = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        description=CompanyDescription(
            description="Energy corp",
            source_url="https://www.equinor.com",
            evidence_text="Energy corp",
            extraction_method="text",
            confidence="HIGH",
            verification_status="VERIFIED",
        ),
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_short)
    assert breakdown["description"] == 0

    # Cookie boilerplate
    p_cookie = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        description=CompanyDescription(
            description="We use cookies on our website to enhance your user experience and collect stats.",
            source_url="https://www.equinor.com",
            evidence_text="cookies",
            extraction_method="text",
            confidence="HIGH",
            verification_status="VERIFIED",
        ),
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_cookie)
    assert breakdown["description"] == 0

    # Valid substantial description
    p_valid = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        description=CompanyDescription(
            description="Equinor is an international broad energy company committed to long-term value creation.",
            source_url="https://www.equinor.com",
            evidence_text="broad energy company",
            extraction_method="text",
            confidence="HIGH",
            verification_status="VERIFIED",
        ),
        last_updated="2026-10-13T12:00:00Z",
    )
    score, breakdown = calculate_completeness_score(p_valid)
    assert breakdown["description"] == 15


# -----------------------------------------------------------------------------
# 14, 15, 16. Freshness Calculation Tests
# -----------------------------------------------------------------------------

def test_freshness_status_fresh():
    """Test 14: Freshness status is correctly calculated as FRESH when under threshold."""
    now_iso = datetime.now(timezone.utc).isoformat()
    status, age = compute_freshness(last_checked_iso=now_iso, freshness_threshold_days=30)
    assert status == "FRESH"
    assert age <= 0.1


def test_freshness_status_stale():
    """Test 15: Freshness status is correctly calculated as STALE when over threshold."""
    stale_date = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    status, age = compute_freshness(last_checked_iso=stale_date, freshness_threshold_days=30)
    assert status == "STALE"
    assert age >= 44.0


def test_freshness_status_unknown():
    """Test 16: Freshness status is correctly calculated as UNKNOWN when date is missing or invalid."""
    status, age = compute_freshness(last_checked_iso=None, last_updated_iso=None)
    assert status == "UNKNOWN"
    assert age == 0.0

    status_inv, _ = compute_freshness(last_checked_iso="not-a-date")
    assert status_inv == "UNKNOWN"


# -----------------------------------------------------------------------------
# 17, 18, 19. Refresh CLI Command Tests
# -----------------------------------------------------------------------------

def test_refresh_identifies_stale_profiles(tmp_path):
    """Test 17: StorageManager.get_stale_companies identifies stale profiles."""
    db_file = str(tmp_path / "test_stale.db")
    storage = StorageManager(db_file)

    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    p_stale = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated=old_date,
        last_checked_at=old_date,
        processing_status="IDENTITY_COMPLETE",
    )
    storage.upsert_company(p_stale, batch_id="b1", input_order=1)

    stale_list = storage.get_stale_companies(freshness_days=30)
    assert len(stale_list) == 1
    assert stale_list[0].org_nr == "923609016"
    assert stale_list[0].freshness_status == "STALE"


def test_refresh_command_skips_fresh_profiles(tmp_path):
    """Test 18: Refresh command skips fresh profiles when stale-only is active."""
    db_file = str(tmp_path / "test_fresh_skip.db")
    storage = StorageManager(db_file)

    now_date = datetime.now(timezone.utc).isoformat()
    p_fresh = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated=now_date,
        last_checked_at=now_date,
        processing_status="IDENTITY_COMPLETE",
    )
    storage.upsert_company(p_fresh, batch_id="b1", input_order=1)

    runner = CliRunner()
    result = runner.invoke(app, ["refresh", "--db-path", db_file, "--stale-only", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["total_candidates"] == 0
    assert data["refreshed_count"] == 0


def test_refresh_command_updates_timestamps_on_change(tmp_path):
    """Test 19: Refresh command updates timestamps and detects legal name change."""
    db_file = str(tmp_path / "test_refresh_update.db")
    storage = StorageManager(db_file)

    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    p_old = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="STATOIL ASA",  # Old name
        last_updated=old_date,
        last_checked_at=old_date,
        processing_status="IDENTITY_COMPLETE",
    )
    storage.upsert_company(p_old, batch_id="b1", input_order=1)

    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",  # New name
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=fake_profile):
        runner = CliRunner()
        result = runner.invoke(app, ["refresh", "--db-path", db_file, "--stale-only", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["refreshed_count"] == 1
        assert data["updated_count"] == 1

    updated_comp = storage.get_company("923609016")
    assert updated_comp.legal_name == "EQUINOR ASA"
    assert updated_comp.freshness_status == "FRESH"


# -----------------------------------------------------------------------------
# 20, 21, 22, 23. Benchmark Command & 1,000-Company Extrapolation
# -----------------------------------------------------------------------------

def test_benchmark_throughput_measurement(tmp_path):
    """Test 20: Benchmark command measures throughput accurately."""
    input_file = tmp_path / "test_bm.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=str(tmp_path / "bm.db"), max_requests=10)
    res = engine.run_benchmark(str(input_file), sample_limit=2, offline=True)

    assert res["sample_size"] == 2
    assert res["throughput_companies_per_sec"] > 0
    assert res["requests_per_company"] == 2.0


def test_benchmark_extrapolation_to_1000_companies(tmp_path):
    """Test 21: Benchmark command extrapolates requests and runtime to 1,000 companies."""
    input_file = tmp_path / "test_bm_extrap.csv"
    input_file.write_text("org_nr\n923609016\n", encoding="utf-8")

    engine = BatchEngine(db_path=str(tmp_path / "bm.db"), max_requests=10)
    res = engine.run_benchmark(str(input_file), sample_limit=1, target_scale=1000, offline=True)

    assert res["target_scale"] == 1000
    assert res["projected_requests_1000"] == 2000  # 1000 * 2.0 = 2000
    assert res["projected_runtime_minutes_1000"] >= 0.0


def test_benchmark_verifies_request_limit_compliance(tmp_path):
    """Test 22: Benchmark verifies compliance with the 2,000 request budget ceiling."""
    input_file = tmp_path / "test_bm_comp.csv"
    input_file.write_text("org_nr\n923609016\n", encoding="utf-8")

    engine = BatchEngine(db_path=str(tmp_path / "bm.db"), max_requests=10)
    res = engine.run_benchmark(str(input_file), sample_limit=1, target_scale=1000, offline=True)

    assert res["projected_requests_1000"] <= 2000
    assert res["request_budget_compliant"] is True
    assert res["cost_compliant"] is True
    assert res["evaluation_ready"] is True


def test_benchmark_verifies_runtime_limit_compliance(tmp_path):
    """Test 23: Benchmark verifies compliance with the 45-minute runtime ceiling."""
    input_file = tmp_path / "test_bm_time.csv"
    input_file.write_text("org_nr\n923609016\n", encoding="utf-8")

    engine = BatchEngine(db_path=str(tmp_path / "bm.db"), max_requests=10)
    res = engine.run_benchmark(str(input_file), sample_limit=1, target_scale=1000, offline=True)

    assert res["projected_runtime_minutes_1000"] <= 45.0
    assert res["runtime_compliant"] is True


# -----------------------------------------------------------------------------
# 24 & 25. Runtime Ceiling Guard & Resumption
# -----------------------------------------------------------------------------

def test_batchengine_enforces_runtime_ceiling(tmp_path):
    """Test 24: BatchEngine enforces runtime ceiling and sets RUNTIME_EXHAUSTED."""
    db_file = str(tmp_path / "test_timeout.db")
    input_file = tmp_path / "timeout.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    # Set max_runtime_seconds to 0.0001s so it immediately triggers runtime ceiling
    engine = BatchEngine(db_path=db_file, max_requests=50, max_runtime_seconds=0.0001)

    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="", retrieved_at=""),
    )

    with patch.object(BrregClient, "fetch_company", return_value=fake_profile):
        report = engine.run_batch(str(input_file))

    storage = StorageManager(db_file)
    batch_rec = storage.get_batch(storage.get_latest_batch_id())
    assert batch_rec["status"] == "RUNTIME_EXHAUSTED"


def test_resumption_preserves_budget_without_leakage(tmp_path):
    """Test 25: Resumption preserves requests_used state without double counting or leakage."""
    db_file = str(tmp_path / "test_resume_budget.db")
    storage = StorageManager(db_file)
    batch_id = "batch_resume_1"

    # Seed batch with 5 used requests out of 20
    storage.get_or_create_batch(batch_id=batch_id, total_companies=2, max_requests=20)
    storage.update_batch_progress(batch_id=batch_id, requests_used=5, status="IN_PROGRESS")

    budget = RequestBudget(max_requests=20, initial_used=5)
    assert budget.total_used == 5
    assert budget.remaining == 15

    # Consume 3 more
    budget.consume(3)
    storage.update_batch_progress(batch_id=batch_id, requests_used=budget.total_used, status="IN_PROGRESS")

    batch_rec = storage.get_batch(batch_id)
    assert batch_rec["requests_used"] == 8
    assert batch_rec["max_requests"] == 20


# -----------------------------------------------------------------------------
# 26, 27, 28, 29. Critical Evaluation Fixes Regression Tests (Issues 1, 2, 3)
# -----------------------------------------------------------------------------

def test_failed_registry_lookups_prevent_evaluation_readiness(tmp_path):
    """Test 26 (Issue 1): Failed registry lookups mark readiness as NOT YET VERIFIED and throughput 0."""
    db_file = str(tmp_path / "test_eval_fail.db")
    input_file = tmp_path / "failed_sample.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=10)

    # Simulate network/registry lookup failure for all lookups in sample
    with patch.object(BrregClient, "fetch_company", side_effect=httpx.ConnectError("Network unreachable")):
        res = engine.run_benchmark(str(input_file), sample_limit=2, offline=False)

    assert res["successful_lookups"] == 0
    assert res["failed_lookups"] == 2
    assert res["throughput_companies_per_sec"] == 0.0
    assert res["evaluation_ready"] is False
    assert res["readiness_status"] == "NOT YET VERIFIED"
    assert "Registry lookups failed" in res["failure_reason"]

    formatted = format_benchmark_text(res)
    assert "OVERALL EVALUATION STATUS:         NOT YET VERIFIED" in formatted


def test_batch_report_separates_current_failures_from_cached_db_records(tmp_path):
    """Test 27 (Issue 2): Batch report does not report historical cached profiles as current successes."""
    db_file = str(tmp_path / "test_db_separation.db")
    input_file = tmp_path / "cached_sample.csv"
    input_file.write_text("org_nr\n923609016\n984851006\n", encoding="utf-8")

    storage = StorageManager(db_file)
    batch_id = "batch_test_hist"

    # Pre-populate SQLite with historical enriched profiles from an earlier run
    p1 = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-10T10:00:00Z",
        processing_status="ENRICHED",
        completeness_score=85,
    )
    p2 = ComprehensiveCompanyProfile(
        org_nr="984851006",
        legal_name="DNB BANK ASA",
        last_updated="2026-10-10T10:00:00Z",
        processing_status="ENRICHED",
        completeness_score=80,
    )
    storage.upsert_company(p1, batch_id=batch_id, input_order=1)
    storage.upsert_company(p2, batch_id=batch_id, input_order=2)

    engine = BatchEngine(db_path=db_file, max_requests=10)

    # Now execute a new run where registry lookups fail
    with patch.object(BrregClient, "fetch_company", side_effect=httpx.ConnectError("Connection refused")):
        report = engine.run_batch(str(input_file))

    # The current run report must report failures, NOT claim the old cached records as newly resolved
    assert report.failed_current_run == 2
    assert report.processed_current_run == 0
    assert report.identities_resolved == 0

    # Ensure historical records in DB were preserved without being erased
    hist_p1 = storage.get_company("923609016")
    assert hist_p1 is not None
    assert hist_p1.legal_name == "EQUINOR ASA"


def test_adaptive_scaling_evaluates_two_pass_budget_headroom(tmp_path):
    """Test 28 (Issue 3): Scalability benchmark calculates Pass 1 + Pass 2 adaptive strategy without naive 4000 req explosion."""
    db_file = str(tmp_path / "test_adaptive_headroom.db")
    input_file = tmp_path / "sample_adaptive.csv"
    input_file.write_text("org_nr\n923609016\n", encoding="utf-8")

    engine = BatchEngine(db_path=db_file, max_requests=20)

    # Simulate 1 company resolved
    fake_profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        website="https://www.equinor.com",
        status=CompanyStatus(summary="Active"),
        source=SourceMetadata(source_url="https://data.brreg.no", retrieved_at="2026-10-13T00:00:00Z"),
    )

    def fake_fetch(self, org_nr):
        self.budget.consume(1)
        return fake_profile

    with patch.object(BrregClient, "fetch_company", autospec=True, side_effect=fake_fetch):
        with patch.object(SourceDiscoverer, "discover_sources") as mock_disc:
            mock_disc.return_value = MagicMock(official_website=None, candidate_urls=[])
            res = engine.run_benchmark(str(input_file), sample_limit=1, target_scale=1000, offline=False)

    assert res["pass1_identity_requests"] == 1000
    assert res["pass2_available_headroom"] == 1000
    assert res["projected_requests_1000"] <= 2000
    assert res["request_budget_compliant"] is True
    assert res["expected_registry_only"] >= 0
    assert res["expected_fully_enriched"] + res["expected_registry_only"] == 1000


def test_coverage_report_text_distinguishes_current_run_and_reused(tmp_path):
    """Test 29 (Issue 2): CoverageReport text clearly formats current run, reused, and profile classifications."""
    from nordtrace.reporting import build_coverage_report, format_report_text

    p_fresh = ComprehensiveCompanyProfile(
        org_nr="923609016",
        legal_name="EQUINOR ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="ENRICHED",
        completeness_score=85,
    )
    p_cached = ComprehensiveCompanyProfile(
        org_nr="984851006",
        legal_name="DNB BANK ASA",
        last_updated="2026-10-13T12:00:00Z",
        processing_status="IDENTITY_COMPLETE",
        completeness_score=40,
    )

    rep = build_coverage_report(
        profiles=[p_fresh, p_cached],
        total_input_count=2,
        invalid_org_count=0,
        requests_used=5,
        max_requests=20,
        runtime_seconds=1.5,
        processed_current_run=1,
        reused_from_db=1,
        failed_current_run=0,
    )

    formatted = format_report_text(rep)
    assert "BATCH EXECUTION (CURRENT RUN):" in formatted
    assert "Successfully Processed:            1" in formatted
    assert "Reused From Existing Database:     1" in formatted
    assert "Failed In Current Run:             0" in formatted
    assert "Registry-Only Profiles:            1" in formatted
    assert "Fully Enriched Profiles (ENRICHED): 1" in formatted

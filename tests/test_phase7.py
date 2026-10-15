"""Tests for Phase 7 — Signalpost Starter Kit Integration & Evaluator Compatibility."""

import json
import os
import tempfile
from pathlib import Path
import pytest

from nordtrace.batch import parse_batch_input, parse_batch_input_ordered
from nordtrace.models import (
    Address,
    CompanyDescription,
    ComprehensiveCompanyProfile,
    FinancialMetricRecord,
    IndustryCode,
    LeadershipRecord,
    OrgForm,
)
from nordtrace.signalpost import (
    EVALUATOR_MODULES,
    TERMINAL_STATES,
    build_terminal_error_envelope,
    to_signalpost_envelope,
    to_signalpost_profile,
    utc_now_iso,
    validate_signalpost_envelopes,
    write_signalpost_run_report,
)


def _make_dummy_profile(org_nr: str = "923609016", name: str = "EQUINOR ASA") -> ComprehensiveCompanyProfile:
    return ComprehensiveCompanyProfile(
        org_nr=org_nr,
        legal_name=name,
        organisation_form=OrgForm(code="ASA", description="Allmennaksjeselskap"),
        registration_status="Active",
        industry=IndustryCode(code="06.100", description="Extraction of crude petroleum"),
        business_address=Address(
            postal_code="4035",
            city="STAVANGER",
            municipality="STAVANGER",
            country="Norway",
        ),
        official_website="https://www.equinor.com",
        description=CompanyDescription(
            description="Equinor is an international energy company committed to long-term value creation.",
            evidence_text="Equinor is an international energy company.",
            source_url="https://www.equinor.com/about-us",
            extraction_method="heuristic_regex",
            confidence="HIGH",
            verification_status="VERIFIED",
        ),
        leadership=[
            LeadershipRecord(
                company_org_nr=org_nr,
                person_name="Anders Opedal",
                role="CEO",
                source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller",
                evidence_text="Anders Opedal, Daglig leder / Administrerende direktør",
                confidence="HIGH",
                verification_status="VERIFIED",
                retrieved_at="2026-08-24T06:00:00Z",
            )
        ],
        financial_metrics=[
            FinancialMetricRecord(
                target_org_nr=org_nr,
                legal_company_name=name,
                metric_name="revenue",
                reported_value=107000000000.0,
                normalized_value=107000000000.0,
                currency="USD",
                scale="units",
                fiscal_year=2024,
                reporting_period="FY",
                reporting_scope="consolidated_group",
                result_type="ACTUAL",
                source_url="https://www.equinor.com/annual-report-2024",
                evidence_text="Total revenues and other income USD 107,174 million",
                extraction_method="heuristic_regex",
                confidence="HIGH",
                verification_status="VERIFIED",
                retrieved_at="2026-08-24T06:00:00Z",
            ),
            FinancialMetricRecord(
                target_org_nr=org_nr,
                legal_company_name=name,
                metric_name="operating_profit",
                reported_value=28800000000.0,
                normalized_value=28800000000.0,
                currency="USD",
                scale="units",
                fiscal_year=2024,
                reporting_period="FY",
                reporting_scope="consolidated_group",
                result_type="ACTUAL",
                source_url="https://www.equinor.com/annual-report-2024",
                evidence_text="Net operating income USD 28,828 million",
                extraction_method="heuristic_regex",
                confidence="HIGH",
                verification_status="VERIFIED",
                retrieved_at="2026-08-24T06:00:00Z",
            ),
        ],
        last_updated="2026-08-24T06:00:00Z",
        last_checked_at="2026-08-24T06:00:00Z",
        processing_status="ENRICHED",
        completeness_score=95,
    )


class TestSignalpostInputParsing:
    def test_parse_jsonl_input(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"organisation_number": "923609016"}) + "\n")
            f.write(json.dumps({"organisation_number": "984851006"}) + "\n")
            f.write(json.dumps({"org_nr": "123456780"}) + "\n")  # invalid checksum
            path = f.name

        try:
            valid, invalid = parse_batch_input(path)
            assert valid == ["923609016", "984851006"]
            assert invalid == ["123456780"]
        finally:
            os.remove(path)

    def test_parse_json_input(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps([
                {"organisation_number": "923609016"},
                {"organisation_number": "984851006"},
            ]))
            path = f.name

        try:
            valid, invalid = parse_batch_input(path)
            assert valid == ["923609016", "984851006"]
            assert invalid == []
        finally:
            os.remove(path)

    def test_parse_batch_input_ordered(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("org_nr\n")
            f.write("923609016\n")
            f.write("invalid_123\n")
            f.write("984851006\n")
            path = f.name

        try:
            valid, invalid, ordered = parse_batch_input_ordered(path)
            assert valid == ["923609016", "984851006"]
            assert invalid == ["invalid_123"]
            assert ordered == ["923609016", "invalid_123", "984851006"]
        finally:
            os.remove(path)


class TestSignalpostEnvelope:
    def test_envelope_schema_conformance(self):
        profile = _make_dummy_profile()
        env = to_signalpost_envelope(
            profile,
            run_id="run-test-01",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:05Z",
            requests=4,
            runtime_ms=5000,
        )

        # OUTPUT_CONTRACT.md schema checks
        assert env["organisation_number"] == "923609016"
        assert "run" in env
        assert env["run"]["run_id"] == "run-test-01"
        assert env["run"]["terminal_status"] == "completed"
        assert isinstance(env["claims"], list)
        assert len(env["claims"]) > 0
        assert isinstance(env["evidence"], list)
        assert len(env["evidence"]) > 0
        assert isinstance(env["operations"], dict)
        assert env["operations"]["requests"] == 4
        assert env["operations"]["runtime_ms"] == 5000
        assert env["operations"]["third_party_cost_usd"] == 0

        # Starter kit batch.py validator checks
        assert env["state"] in TERMINAL_STATES
        assert "modules" in env
        for mod in ("registry", "registry_live", "financials", "roles", "website"):
            assert mod in env["modules"]
            assert env["modules"][mod]["state"] in TERMINAL_STATES

    def test_terminal_error_envelope(self):
        env = build_terminal_error_envelope(
            org_nr="111222333",
            run_id="run-err-01",
            started_at="2026-08-24T06:00:00Z",
            completed_at="2026-08-24T06:00:01Z",
            error_state="not_found",
            error_message="Entity not found in Brønnøysund registry",
        )
        assert env["organisation_number"] == "111222333"
        assert env["state"] == "not_found"
        assert env["run"]["terminal_status"] == "not_found"
        assert len(env["errors"]) == 1
        assert "not found" in env["errors"][0].lower()
        for mod in EVALUATOR_MODULES:
            assert env["modules"][mod]["state"] == "not_found"


class TestSignalpostProfile:
    def test_evaluator_profile_evidence_mapping(self):
        profile = _make_dummy_profile()
        sp_profile = to_signalpost_profile(profile)

        assert sp_profile["organisation_number"] == "923609016"
        assert sp_profile["name"] == "EQUINOR ASA"
        assert sp_profile["legal_form"] == "ASA"
        assert sp_profile["municipality"] == "STAVANGER"
        assert sp_profile["industry_code"] == "06.100"
        assert sp_profile["industry_label"] == "Extraction of crude petroleum"

        ev = sp_profile["evidence"]
        assert "registry" in ev
        assert "registry_live" in ev
        assert "accounting_obligation" in ev
        assert "financials" in ev
        assert "roles" in ev
        assert "website" in ev

        assert ev["registry"]["status"] == "available"
        assert ev["registry_live"]["value"]["organisation_number"] == "923609016"
        assert ev["financials"]["status"] == "available"
        fin_records = ev["financials"]["value"]["records"]
        assert len(fin_records) >= 1
        assert fin_records[0]["period"] == "2024"
        assert fin_records[0]["revenue"] == 107000000000.0
        assert fin_records[0]["operating_result"] == 28800000000.0


class TestEvaluatorValidation:
    def test_validate_signalpost_envelopes_success(self):
        profile = _make_dummy_profile("923609016", "EQUINOR ASA")
        env1 = to_signalpost_envelope(profile, "run-1", "2026-08-24T06:00:00Z", "2026-08-24T06:00:05Z")
        env2 = build_terminal_error_envelope("999999999", "run-1", "2026-08-24T06:00:00Z", "2026-08-24T06:00:05Z", "not_found", "Not found")

        val = validate_signalpost_envelopes([env1, env2], expected_count=2)
        assert val["passed"] is True
        assert val["checks"]["exact_expected_count"] is True
        assert val["checks"]["unique_organisation_numbers"] is True
        assert val["checks"]["all_entity_states_terminal"] is True
        assert val["checks"]["all_module_states_terminal"] is True
        assert val["checks"]["zero_silent_drops"] is True

    def test_validate_signalpost_envelopes_mismatch(self):
        profile = _make_dummy_profile("923609016", "EQUINOR ASA")
        env1 = to_signalpost_envelope(profile, "run-1", "2026-08-24T06:00:00Z", "2026-08-24T06:00:05Z")

        # Expect 2, but only 1 emitted -> zero_silent_drops must fail!
        val = validate_signalpost_envelopes([env1], expected_count=2)
        assert val["passed"] is False
        assert val["checks"]["exact_expected_count"] is False
        assert val["checks"]["zero_silent_drops"] is False

    def test_write_signalpost_run_report(self):
        profile = _make_dummy_profile("923609016", "EQUINOR ASA")
        env1 = to_signalpost_envelope(profile, "run-1", "2026-08-24T06:00:00Z", "2026-08-24T06:00:05Z")

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            report_path = f.name

        try:
            report = write_signalpost_run_report(
                report_path=report_path,
                run_id="run-1",
                started_at="2026-08-24T06:00:00Z",
                completed_at="2026-08-24T06:00:05Z",
                expected_count=1,
                envelopes=[env1],
                operations={"requests": 2, "runtime_ms": 1200, "third_party_cost_usd": 0},
            )
            assert report["run_id"] == "run-1"
            assert report["expected_count"] == 1
            assert report["emitted_envelopes"] == 1
            assert report["validation"]["passed"] is True
            assert os.path.exists(report_path)
        finally:
            if os.path.exists(report_path):
                os.remove(report_path)


class TestStarterKitNativeIntegration:
    """Tests direct interop with starter kit logic when installed."""

    def test_starter_kit_research_agent_qa(self):
        import sys
        starter_kit_src = str(Path(__file__).resolve().parents[1] / "starter_kit" / "src")
        if starter_kit_src not in sys.path:
            sys.path.insert(0, starter_kit_src)

        try:
            from norway_company_agent.research import answer_profile
        except ImportError:
            pytest.skip("Starter kit not available or dependencies missing")

        profile = _make_dummy_profile()
        sp_profile = to_signalpost_profile(profile)

        # Question on leadership and financials
        ans = answer_profile(sp_profile, "Who leads this company and what financial facts and locations are available?")
        assert ans["organisation_number"] == "923609016"
        assert len(ans["facts"]) >= 4

        # Verify all facts have required provenance
        for fact in ans["facts"]:
            assert fact.get("source_url")
            assert fact.get("retrieved_at")
            assert fact.get("content_sha256")

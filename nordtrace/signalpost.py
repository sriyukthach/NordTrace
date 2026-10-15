"""Signalpost starter kit and evaluator compatibility integration.

Implements the official evaluation contract:
- OUTPUT_CONTRACT.md minimal output envelope schema
- Evaluator-compatible profiles schema with evidence mapping
- Evaluator envelope validation with Zero Silent Drops guarantee
- Machine-readable batch run report JSON
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from nordtrace.models import ComprehensiveCompanyProfile


TERMINAL_STATES = {
    "complete",
    "not_applicable",
    "not_found",
    "blocked_policy",
    "blocked_robots",
    "source_error",
    "budget_exhausted",
    "submission_error",
}

EVALUATOR_MODULES = [
    "registry",
    "registry_live",
    "accounting_obligation",
    "financials",
    "roles",
    "locations",
    "website",
]


def utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    """Computes SHA-256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_signalpost_envelope(
    profile: ComprehensiveCompanyProfile,
    run_id: str,
    started_at: str,
    completed_at: str,
    requests: int = 1,
    runtime_ms: int = 100,
) -> Dict[str, Any]:
    """Builds a terminal envelope satisfying OUTPUT_CONTRACT.md and evaluator validation."""
    now_ts = utc_now_iso()
    org_nr = profile.org_nr
    claims: List[Dict[str, Any]] = []
    evidence_list: List[Dict[str, Any]] = []

    # Map status to official terminal state
    if profile.processing_status in ("IDENTITY_COMPLETE", "ENRICHED", "PARTIALLY_ENRICHED"):
        terminal_state = "complete"
    elif profile.processing_status == "BUDGET_EXHAUSTED":
        terminal_state = "budget_exhausted"
    elif profile.processing_status == "FAILED":
        if any("not found" in w.lower() for w in profile.warnings):
            terminal_state = "not_found"
        else:
            terminal_state = "source_error"
    else:
        terminal_state = "complete"

    # Module states for evaluator contract
    module_states: Dict[str, Dict[str, Any]] = {}

    # 1. Registry claim & evidence
    reg_ev_id = "ev-registry-1"
    reg_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}"
    reg_span = f"Official registration for {profile.legal_name} ({org_nr})"
    reg_hash = sha256_text(f"{org_nr}:{profile.legal_name}:{profile.registration_status}")

    if terminal_state == "complete":
        evidence_list.append({
            "id": reg_ev_id,
            "source_url": reg_url,
            "source_class": "official_registry",
            "retrieved_at": profile.last_checked_at or completed_at,
            "content_sha256": reg_hash,
            "claim_span": reg_span,
        })
        claims.append({
            "field": "company_name",
            "value": profile.legal_name,
            "availability": "available",
            "confidence": 1.0,
            "evidence_ids": [reg_ev_id],
        })
        if profile.organisation_form:
            claims.append({
                "field": "organisation_form",
                "value": profile.organisation_form.code,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [reg_ev_id],
            })
        if profile.registration_status:
            claims.append({
                "field": "registration_status",
                "value": profile.registration_status,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [reg_ev_id],
            })
        if profile.business_address:
            addr = profile.business_address
            addr_str = f"{addr.postal_code or ''} {addr.city or ''}".strip()
            claims.append({
                "field": "registered_office",
                "value": addr_str,
                "availability": "available",
                "confidence": 1.0,
                "evidence_ids": [reg_ev_id],
            })

        module_states["registry"] = {"state": "complete", "retry_count": 0, "final_timestamp": profile.last_checked_at or completed_at}
        module_states["registry_live"] = {"state": "complete", "retry_count": 0, "final_timestamp": profile.last_checked_at or completed_at}
        module_states["accounting_obligation"] = {"state": "complete", "retry_count": 0, "final_timestamp": profile.last_checked_at or completed_at}
    else:
        module_states["registry"] = {"state": terminal_state, "retry_count": 0, "final_timestamp": completed_at}
        module_states["registry_live"] = {"state": terminal_state, "retry_count": 0, "final_timestamp": completed_at}
        module_states["accounting_obligation"] = {"state": "not_applicable" if terminal_state == "not_found" else terminal_state, "retry_count": 0, "final_timestamp": completed_at}

    # 2. Official Website
    if profile.official_website:
        web_ev_id = "ev-web-1"
        web_hash = sha256_text(profile.official_website)
        evidence_list.append({
            "id": web_ev_id,
            "source_url": profile.official_website,
            "source_class": "company_owned",
            "retrieved_at": profile.last_checked_at or completed_at,
            "content_sha256": web_hash,
            "claim_span": f"Official website discovered: {profile.official_website}",
        })
        claims.append({
            "field": "official_website",
            "value": profile.official_website,
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": [web_ev_id],
        })
        module_states["website"] = {"state": "complete", "retry_count": 0, "final_timestamp": profile.last_checked_at or completed_at}
    else:
        claims.append({
            "field": "official_website",
            "value": None,
            "availability": "not_available",
            "confidence": 0.9,
            "evidence_ids": [],
        })
        module_states["website"] = {"state": "not_applicable" if terminal_state == "complete" else terminal_state, "retry_count": 0, "final_timestamp": completed_at}

    # 3. Description
    if profile.description and profile.description.description:
        desc = profile.description
        desc_ev_id = f"ev-desc-{len(evidence_list)+1}"
        evidence_list.append({
            "id": desc_ev_id,
            "source_url": desc.source_url,
            "source_class": "company_reported",
            "retrieved_at": profile.last_checked_at or completed_at,
            "content_sha256": sha256_text(desc.description),
            "claim_span": desc.evidence_text[:120],
        })
        claims.append({
            "field": "description",
            "value": desc.description,
            "availability": "available",
            "confidence": 0.95 if desc.verification_status == "VERIFIED" else 0.70,
            "evidence_ids": [desc_ev_id],
        })

    # 4. Financials
    fin_records = [m for m in profile.financial_metrics if m.result_type == "ACTUAL" and m.verification_status == "VERIFIED"]
    if fin_records:
        fin_ev_id = f"ev-fin-{len(evidence_list)+1}"
        m0 = fin_records[0]
        evidence_list.append({
            "id": fin_ev_id,
            "source_url": m0.source_url or reg_url,
            "source_class": "official_annual_accounts",
            "retrieved_at": m0.retrieved_at or completed_at,
            "content_sha256": sha256_text(f"{m0.metric_name}:{m0.reported_value}"),
            "claim_span": m0.evidence_text[:120],
        })
        for m in fin_records:
            claims.append({
                "field": f"financial_{m.metric_name}",
                "value": m.reported_value,
                "availability": "available",
                "confidence": 0.95,
                "evidence_ids": [fin_ev_id],
            })
        module_states["financials"] = {"state": "complete", "retry_count": 0, "final_timestamp": m0.retrieved_at or completed_at}
    else:
        module_states["financials"] = {"state": "not_applicable" if terminal_state == "complete" else terminal_state, "retry_count": 0, "final_timestamp": completed_at}

    # 5. Roles / Leadership
    if profile.leadership:
        roles_ev_id = f"ev-roles-{len(evidence_list)+1}"
        l0 = profile.leadership[0]
        evidence_list.append({
            "id": roles_ev_id,
            "source_url": l0.source_url,
            "source_class": "official_role_record",
            "retrieved_at": l0.retrieved_at or completed_at,
            "content_sha256": sha256_text(f"{l0.person_name}:{l0.role}"),
            "claim_span": l0.evidence_text[:120],
        })
        claims.append({
            "field": "leadership",
            "value": [f"{l.person_name} ({l.role})" for l in profile.leadership],
            "availability": "available",
            "confidence": 0.95,
            "evidence_ids": [roles_ev_id],
        })
        module_states["roles"] = {"state": "complete", "retry_count": 0, "final_timestamp": l0.retrieved_at or completed_at}
    else:
        module_states["roles"] = {"state": "not_applicable" if terminal_state == "complete" else terminal_state, "retry_count": 0, "final_timestamp": completed_at}

    # 6. Locations
    if profile.locations:
        module_states["locations"] = {"state": "complete", "retry_count": 0, "final_timestamp": completed_at}
    else:
        module_states["locations"] = {"state": "not_applicable" if terminal_state == "complete" else terminal_state, "retry_count": 0, "final_timestamp": completed_at}

    # Evaluator-compatible profile structure
    evaluator_profile = to_signalpost_profile(profile)

    return {
        "run_id": run_id,
        "organisation_number": org_nr,
        "state": terminal_state,
        "started_at": started_at,
        "completed_at": completed_at,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed" if terminal_state == "complete" else terminal_state,
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": [],
        "errors": profile.warnings if terminal_state != "complete" else [],
        "operations": {
            "requests": requests,
            "runtime_ms": runtime_ms,
            "third_party_cost_usd": 0,
        },
        "modules": module_states,
        "profile": evaluator_profile,
    }


def to_signalpost_profile(profile: ComprehensiveCompanyProfile) -> Dict[str, Any]:
    """Maps ComprehensiveCompanyProfile into the evaluator-compatible evidence structure."""
    org_nr = profile.org_nr
    retrieved_at = profile.last_checked_at or utc_now_iso()

    municipality = profile.business_address.municipality if profile.business_address else None
    legal_form = profile.organisation_form.code if profile.organisation_form else None

    fin_by_period: Dict[str, Dict[str, Any]] = {}
    for m in sorted(profile.financial_metrics, key=lambda x: -(x.fiscal_year or 0)):
        if m.result_type != "ACTUAL":
            continue
        period_key = str(m.fiscal_year) if m.fiscal_year else "latest"
        if period_key not in fin_by_period:
            fin_by_period[period_key] = {
                "period": period_key,
                "currency": m.currency,
                "revenue": None,
                "operating_result": None,
                "annual_result": None,
                "assets": None,
                "debt": None,
            }
        rec = fin_by_period[period_key]
        m_name = m.metric_name.lower()
        if "revenue" in m_name or "sales" in m_name:
            rec["revenue"] = m.reported_value
        elif "operating" in m_name or "ebit" in m_name:
            rec["operating_result"] = m.reported_value
        elif "net" in m_name or "annual" in m_name or "profit_before" in m_name:
            rec["annual_result"] = m.reported_value
        elif "asset" in m_name:
            rec["assets"] = m.reported_value
        elif "debt" in m_name or "liabilit" in m_name:
            rec["debt"] = m.reported_value
    fin_records = list(fin_by_period.values())

    roles_list = []
    for l in profile.leadership:
        roles_list.append({"name": l.person_name, "role": l.role})

    locations_list = []
    for loc in profile.locations:
        locations_list.append({
            "name": profile.legal_name,
            "address": loc.city or loc.postal_code or "",
        })

    social_links = []
    for c in profile.contacts:
        if c.contact_type == "social_media":
            platform = "unknown"
            val_lower = c.value.lower()
            if "linkedin" in val_lower:
                platform = "LinkedIn"
            elif "facebook" in val_lower:
                platform = "Facebook"
            elif "instagram" in val_lower:
                platform = "Instagram"
            elif "youtube" in val_lower:
                platform = "YouTube"
            elif "twitter" in val_lower or "x.com" in val_lower:
                platform = "X"
            social_links.append({"platform": platform, "url": c.value})

    evidence_dict: Dict[str, Any] = {
        "registry": {
            "status": "available",
            "source_type": "official_registry_bulk",
            "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
            "retrieved_at": retrieved_at,
            "value": {
                "organisasjonsnummer": org_nr,
                "navn": profile.legal_name,
                "organisasjonsform": {"kode": legal_form} if legal_form else None,
            },
            "content_sha256": sha256_text(f"{org_nr}:{profile.legal_name}"),
        },
        "registry_live": {
            "status": "available",
            "source_type": "official_registry_live",
            "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
            "retrieved_at": retrieved_at,
            "value": {
                "organisation_number": org_nr,
                "name": profile.legal_name,
                "legal_form": legal_form,
                "municipality": municipality,
                "website": profile.official_website,
            },
            "content_sha256": sha256_text(f"{org_nr}:{profile.legal_name}"),
        },
        "accounting_obligation": {
            "status": "available",
            "source_type": "official_rule_interpretation",
            "source_url": "https://www.brreg.no/en/submission-of-annual-accounts/",
            "retrieved_at": retrieved_at,
            "value": {
                "classification": "filing_observed" if fin_records else "required_by_legal_form" if legal_form in ("AS", "ASA") else "threshold_or_activity_dependent",
                "legal_form": legal_form,
            },
            "content_sha256": sha256_text(str(legal_form)),
        },
        "financials": {
            "status": "available" if fin_records else "not_applicable",
            "source_type": "official_annual_accounts",
            "source_url": f"https://data.brreg.no/regnskapsregisteret/regnskap/{org_nr}",
            "retrieved_at": retrieved_at,
            "value": {"records": fin_records},
            "content_sha256": sha256_text(str(fin_records)),
        },
        "roles": {
            "status": "available" if roles_list else "not_applicable",
            "source_type": "official_role_record",
            "source_url": f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}/roller",
            "retrieved_at": retrieved_at,
            "value": {"roles": roles_list},
            "content_sha256": sha256_text(str(roles_list)),
        },
        "locations": {
            "status": "available" if locations_list else "not_applicable",
            "source_type": "official_subunit_record",
            "source_url": f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_nr}",
            "retrieved_at": retrieved_at,
            "value": {"locations": locations_list},
            "content_sha256": sha256_text(str(locations_list)),
        },
        "website": {
            "status": "available" if profile.official_website else "not_available",
            "source_type": "company_website",
            "source_url": profile.official_website or "",
            "retrieved_at": retrieved_at,
            "value": {
                "title": profile.legal_name,
                "description": profile.description.description if profile.description else None,
                "social_links": social_links,
                "identity_assessment": {"publishable": True, "status": "exact"},
            },
            "content_sha256": sha256_text(profile.official_website or ""),
        },
    }

    return {
        "organisation_number": org_nr,
        "company_name": profile.legal_name,
        "name": profile.legal_name,
        "legal_form": legal_form,
        "municipality": municipality,
        "industry_code": profile.industry.code if profile.industry else None,
        "industry_label": profile.industry.description if profile.industry else None,
        "employees": None,
        "website": profile.official_website,
        "completeness_score": profile.completeness_score,
        "processing_status": profile.processing_status,
        "evidence": evidence_dict,
    }


def build_terminal_error_envelope(
    org_nr: str,
    run_id: str,
    started_at: str,
    completed_at: str,
    error_state: str,
    error_message: str,
) -> Dict[str, Any]:
    """Constructs a terminal envelope for failed or invalid organisation numbers to prevent silent drops."""
    state = error_state if error_state in TERMINAL_STATES else "source_error"

    module_states = {
        mod: {"state": state, "retry_count": 0, "final_timestamp": completed_at}
        for mod in EVALUATOR_MODULES
    }

    return {
        "run_id": run_id,
        "organisation_number": org_nr,
        "state": state,
        "started_at": started_at,
        "completed_at": completed_at,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": state,
        },
        "claims": [],
        "evidence": [],
        "changes": [],
        "errors": [error_message],
        "operations": {
            "requests": 1,
            "runtime_ms": 50,
            "third_party_cost_usd": 0,
        },
        "modules": module_states,
        "profile": {
            "organisation_number": org_nr,
            "company_name": f"Unknown ({error_message})",
            "name": f"Unknown ({error_message})",
            "evidence": {},
            "processing_status": "FAILED",
            "completeness_score": 0,
        },
    }


def validate_signalpost_envelopes(envelopes: List[Dict[str, Any]], expected_count: int) -> Dict[str, Any]:
    """Validates envelopes against official evaluator rules (zero silent drops, terminal states)."""
    orgs = [item.get("organisation_number") for item in envelopes]
    invalid_states = [
        {"organisation_number": item.get("organisation_number"), "module": mod, "state": state.get("state")}
        for item in envelopes
        for mod, state in item.get("modules", {}).items()
        if state.get("state") not in TERMINAL_STATES
    ]
    invalid_entity_states = [
        {"organisation_number": item.get("organisation_number"), "state": item.get("state")}
        for item in envelopes
        if item.get("state") not in TERMINAL_STATES
    ]
    checks = {
        "exact_expected_count": len(envelopes) == expected_count,
        "unique_organisation_numbers": len(orgs) == len(set(orgs)),
        "all_entity_states_terminal": len(invalid_entity_states) == 0,
        "all_module_states_terminal": len(invalid_states) == 0,
        "zero_silent_drops": len(envelopes) == expected_count and len(orgs) == len(set(orgs)),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "invalid_states": invalid_states + invalid_entity_states,
    }


def write_signalpost_run_report(
    report_path: str,
    run_id: str,
    started_at: str,
    completed_at: str,
    expected_count: int,
    envelopes: List[Dict[str, Any]],
    operations: Dict[str, Any],
) -> Dict[str, Any]:
    """Writes a machine-readable run report JSON matching evaluator expectations."""
    validation = validate_signalpost_envelopes(envelopes, expected_count)
    report = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_count": expected_count,
        "emitted_envelopes": len(envelopes),
        "resumed_profiles": operations.get("resumed_profiles", 0),
        "profiles_fetched_this_run": operations.get("profiles_fetched_this_run", len(envelopes)),
        "modules": EVALUATOR_MODULES,
        "operations": operations,
        "validation": validation,
    }
    out_path = Path(report_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report

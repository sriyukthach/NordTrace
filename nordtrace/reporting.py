"""Completeness scoring and batch coverage reporting engine for NordTrace."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from nordtrace.models import ComprehensiveCompanyProfile

NAV_OR_BOILERPLATE_HEADINGS = {
    "explore", "connect", "useful links", "latest news", "site info", "navigation",
    "menu", "quick links", "find out more", "read more", "get in touch", "follow us",
    "social media", "stay updated", "subscribe", "newsletter", "overview", "links",
    "related links", "search", "share", "copyright", "privacy", "terms", "cookies",
    "cookie policy", "terms of use", "privacy policy", "disclaimer", "all rights reserved",
    "footer", "header", "main menu", "sub menu", "accessibility", "sitemap", "site map",
    "about us", "who we are", "contact us", "contact", "careers", "jobs", "vacancies",
    "press", "press releases", "news", "articles", "media", "investors", "investor relations",
    "more news", "events", "forthcoming events", "current and forthcoming events", "upcoming events",
    "latest updates", "recent news",
}


def compute_freshness(
    last_checked_iso: Optional[str],
    last_updated_iso: Optional[str] = None,
    freshness_threshold_days: int = 30,
) -> Tuple[str, float]:
    """Computes freshness status ('FRESH', 'STALE', 'UNKNOWN') and data age in days.

    Args:
        last_checked_iso: ISO 8601 UTC timestamp of last verification check.
        last_updated_iso: Fallback ISO timestamp of last update if last_checked is None.
        freshness_threshold_days: Max days before data is deemed STALE (default 30).

    Returns:
        Tuple of (status_string, age_in_days)
    """
    target_str = last_checked_iso or last_updated_iso
    if not target_str:
        return "UNKNOWN", 0.0

    try:
        clean_str = target_str.replace("Z", "+00:00")
        target_dt = datetime.fromisoformat(clean_str)
        now_dt = datetime.now(timezone.utc)
        age_seconds = (now_dt - target_dt).total_seconds()
        age_days = max(0.0, round(age_seconds / 86400.0, 1))

        status = "STALE" if age_days > freshness_threshold_days else "FRESH"
        return status, age_days
    except Exception:
        return "UNKNOWN", 0.0


def calculate_completeness_score(
    profile: ComprehensiveCompanyProfile,
) -> Tuple[int, Dict[str, int]]:
    """Calculates evidence-backed completeness score (0-100) and component breakdown.

    Scoring Breakdown:
        - Identity (30 pts): Official valid registry company identity resolved.
        - Official website (10 pts): Verified or registry official website present.
        - Description (15 pts): Verified substantial company description text (>20 chars, non-boilerplate).
        - Contact information (10 pts): Verified direct contact (email, phone, direct contact form = 10 pts).
          Social media channels alone award partial credit (4 pts).
        - Leadership (10 pts): At least one verified executive / board member.
        - Products/services (10 pts): At least one verified product/service entry (non-navigation label).
        - Verified actual financials (15 pts): At least one verified ACTUAL historical financial result.
          (Guidance, forecasts, targets, estimates, and ambiguous metrics award 0 pts).

    Returns:
        Tuple of (total_score, breakdown_dict)
    """
    breakdown: Dict[str, int] = {
        "identity": 0,
        "official_website": 0,
        "description": 0,
        "contacts": 0,
        "leadership": 0,
        "products_services": 0,
        "financials": 0,
    }

    # 1. Identity: 30 points
    if profile.org_nr and profile.legal_name:
        breakdown["identity"] = 30

    # 2. Official Website: 10 points
    if profile.official_website:
        breakdown["official_website"] = 10

    # 3. Description: 15 points
    if (
        profile.description
        and profile.description.description
        and profile.description.verification_status == "VERIFIED"
        and len(profile.description.description.strip()) >= 20
        and not any(
            bp in profile.description.description.lower()
            for bp in ["cookie", "all rights reserved", "javascript", "browser"]
        )
    ):
        breakdown["description"] = 15

    # 4. Contact Information: 10 points (Direct contact required for full 10 pts; social-only gets 4 pts)
    verified_direct_contacts = [
        c for c in profile.contacts
        if c.verification_status == "VERIFIED" and c.contact_type in ("email", "phone", "contact_url")
    ]
    verified_social_contacts = [
        c for c in profile.contacts
        if c.verification_status == "VERIFIED" and c.contact_type == "social_media"
    ]
    if verified_direct_contacts:
        breakdown["contacts"] = 10
    elif verified_social_contacts:
        breakdown["contacts"] = 4

    # 5. Leadership: 10 points
    verified_leadership = [l for l in profile.leadership if l.verification_status == "VERIFIED"]
    if verified_leadership:
        breakdown["leadership"] = 10

    # 6. Products/services: 10 points
    verified_products = [
        p for p in profile.products_services
        if p.verification_status == "VERIFIED"
        and p.name.strip().lower() not in NAV_OR_BOILERPLATE_HEADINGS
        and not any(
            bp in p.name.strip().lower()
            for bp in ["useful links", "quick links", "latest news", "site info", "follow us", "all rights reserved"]
        )
    ]
    if verified_products:
        breakdown["products_services"] = 10

    # 7. Financials: 15 points (STRICT: ONLY verified ACTUAL results!)
    verified_actual_financials = [
        m for m in profile.financial_metrics
        if m.verification_status == "VERIFIED" and m.result_type == "ACTUAL"
    ]
    if verified_actual_financials:
        breakdown["financials"] = 15

    total = sum(breakdown.values())
    return total, breakdown


class CoverageReport(BaseModel):
    """Structured evaluation report containing batch statistics and coverage metrics."""

    total_input_companies: int = Field(description="Total input records processed")
    valid_org_numbers: int = Field(description="Count of valid Modulus-11 organisation numbers")
    invalid_org_numbers: int = Field(description="Count of invalid organisation numbers skipped/failed")
    processed_current_run: int = Field(default=0, description="Companies successfully processed in current run")
    reused_from_db: int = Field(default=0, description="Companies reused from existing database")
    failed_current_run: int = Field(default=0, description="Companies failed in current run")
    identities_resolved: int = Field(description="Count of companies with official registry identity resolved in current run")
    registry_only_profiles: int = Field(default=0, description="Profiles with registry identity only (IDENTITY_COMPLETE)")
    partially_enriched_profiles: int = Field(default=0, description="Partially enriched profiles (PARTIALLY_ENRICHED)")
    fully_enriched_profiles: int = Field(default=0, description="Fully enriched profiles (ENRICHED)")
    websites_discovered: int = Field(description="Count of companies with verified official websites")
    descriptions_extracted: int = Field(description="Count of companies with verified descriptions")
    contacts_extracted: int = Field(description="Count of companies with verified contact details")
    leadership_extracted: int = Field(description="Count of companies with verified leadership records")
    products_services_extracted: int = Field(description="Count of companies with verified products/services")
    verified_financials_count: int = Field(
        description="Count of companies with at least one verified ACTUAL historical financial result"
    )
    enriched_profiles: int = Field(description="Profiles classified as fully ENRICHED")
    partial_profiles: int = Field(description="Profiles classified as PARTIALLY_ENRICHED or IDENTITY_COMPLETE")
    failed_profiles: int = Field(description="Profiles classified as FAILED")
    budget_exhausted_profiles: int = Field(description="Profiles halted due to BUDGET_EXHAUSTED")
    average_completeness_score: float = Field(description="Mean completeness score across valid companies")
    total_network_requests: int = Field(description="Total outbound HTTP requests consumed")
    remaining_request_budget: int = Field(description="Remaining global request budget units")
    total_runtime_seconds: float = Field(description="Execution runtime in seconds")


def build_coverage_report(
    profiles: List[ComprehensiveCompanyProfile],
    total_input_count: int,
    invalid_org_count: int,
    requests_used: int,
    max_requests: int,
    runtime_seconds: float,
    processed_current_run: Optional[int] = None,
    reused_from_db: Optional[int] = None,
    failed_current_run: Optional[int] = None,
) -> CoverageReport:
    """Aggregates batch execution profiles into a structured CoverageReport."""
    valid_count = len(profiles)

    reg_only = sum(1 for p in profiles if p.processing_status == "IDENTITY_COMPLETE")
    part_enr = sum(1 for p in profiles if p.processing_status == "PARTIALLY_ENRICHED")
    full_enr = sum(1 for p in profiles if p.processing_status == "ENRICHED")
    failed_p = sum(1 for p in profiles if p.processing_status == "FAILED")
    budget_exh = sum(1 for p in profiles if p.processing_status == "BUDGET_EXHAUSTED")

    identities_count = sum(1 for p in profiles if p.legal_name and p.processing_status != "FAILED")
    curr_proc = processed_current_run if processed_current_run is not None else identities_count
    curr_reused = reused_from_db if reused_from_db is not None else 0
    curr_failed = failed_current_run if failed_current_run is not None else failed_p

    websites = sum(1 for p in profiles if p.official_website and p.processing_status != "FAILED")
    descriptions = sum(1 for p in profiles if p.description and p.description.verification_status == "VERIFIED")
    contacts = sum(1 for p in profiles if any(c.verification_status == "VERIFIED" for c in p.contacts))
    leadership = sum(1 for p in profiles if any(l.verification_status == "VERIFIED" for l in p.leadership))
    products = sum(1 for p in profiles if any(pr.verification_status == "VERIFIED" for pr in p.products_services))
    financials = sum(
        1 for p in profiles
        if any(m.verification_status == "VERIFIED" and m.result_type == "ACTUAL" for m in p.financial_metrics)
    )

    scores = [p.completeness_score for p in profiles if p.processing_status != "FAILED"]
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0

    remaining = max(0, max_requests - requests_used)

    return CoverageReport(
        total_input_companies=total_input_count,
        valid_org_numbers=valid_count,
        invalid_org_numbers=invalid_org_count,
        processed_current_run=curr_proc,
        reused_from_db=curr_reused,
        failed_current_run=curr_failed,
        identities_resolved=curr_proc,
        registry_only_profiles=reg_only,
        partially_enriched_profiles=part_enr,
        fully_enriched_profiles=full_enr,
        websites_discovered=websites,
        descriptions_extracted=descriptions,
        contacts_extracted=contacts,
        leadership_extracted=leadership,
        products_services_extracted=products,
        verified_financials_count=financials,
        enriched_profiles=full_enr,
        partial_profiles=reg_only + part_enr,
        failed_profiles=curr_failed,
        budget_exhausted_profiles=budget_exh,
        average_completeness_score=avg_score,
        total_network_requests=requests_used,
        remaining_request_budget=remaining,
        total_runtime_seconds=round(runtime_seconds, 3),
    )


def format_report_text(report: CoverageReport) -> str:
    """Renders CoverageReport as formatted human-readable terminal output."""
    lines = [
        "============================================================",
        "             NORDTRACE BATCH COVERAGE REPORT                ",
        "============================================================",
        f" Total Input Companies:             {report.total_input_companies}",
        f" Valid Organisation Numbers:        {report.valid_org_numbers}",
        f" Invalid Organisation Numbers:      {report.invalid_org_numbers}",
        "------------------------------------------------------------",
        " BATCH EXECUTION (CURRENT RUN):",
        f" Successfully Processed:            {report.processed_current_run}",
        f" Reused From Existing Database:     {report.reused_from_db}",
        f" Failed In Current Run:             {report.failed_current_run}",
        "------------------------------------------------------------",
        " PROFILE CLASSIFICATION & ENRICHMENT:",
        f" Registry-Only Profiles:            {report.registry_only_profiles}",
        f" Partially Enriched Profiles:       {report.partially_enriched_profiles}",
        f" Fully Enriched Profiles (ENRICHED): {report.fully_enriched_profiles}",
        f" Failed Profiles (FAILED):          {report.failed_profiles}",
        f" Budget Exhausted Profiles:         {report.budget_exhausted_profiles}",
        "------------------------------------------------------------",
        " FIELD-LEVEL EVIDENCE EXTRACTION:",
        f" Identities Resolved (Registry):    {report.identities_resolved}",
        f" Websites Discovered:               {report.websites_discovered}",
        f" Descriptions Extracted:            {report.descriptions_extracted}",
        f" Contacts Extracted:                {report.contacts_extracted}",
        f" Leadership Extracted:              {report.leadership_extracted}",
        f" Products/Services Extracted:       {report.products_services_extracted}",
        f" Verified Actual Financials:        {report.verified_financials_count}",
        "------------------------------------------------------------",
        f" Average Completeness Score:        {report.average_completeness_score} / 100",
        f" Total Outbound HTTP Requests:      {report.total_network_requests}",
        f" Remaining Request Budget:          {report.remaining_request_budget}",
        f" Total Runtime:                     {report.total_runtime_seconds:.3f} seconds",
        "============================================================",
    ]
    return "\n".join(lines) + "\n"

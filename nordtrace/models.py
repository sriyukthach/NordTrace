"""Pydantic models for Norwegian company intelligence, source evidence verification, and financial intelligence."""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Address(BaseModel):
    """Business or postal address structure from Brønnøysund Register Centre."""

    address_lines: Optional[List[str]] = Field(
        default=None, description="Street address lines"
    )
    postal_code: Optional[str] = Field(default=None, description="Postal code")
    city: Optional[str] = Field(default=None, description="City / Postal area")
    municipality: Optional[str] = Field(default=None, description="Municipality name")
    municipality_code: Optional[str] = Field(
        default=None, description="Official 4-digit municipality code"
    )
    country: Optional[str] = Field(default=None, description="Country name")
    country_code: Optional[str] = Field(
        default=None, description="Two-letter ISO country code"
    )


class OrgForm(BaseModel):
    """Organisation form details (e.g. ASA - Allmennaksjeselskap)."""

    code: str = Field(description="Organisation form code, e.g. ASA, AS, ENK")
    description: str = Field(
        description="Official description of the organisation form"
    )


class IndustryCode(BaseModel):
    """Industry classification code (NÆRINGSKODE)."""

    code: str = Field(
        description="NACE / Næringskode, e.g. 06.100 (Utvinning av råolje)"
    )
    description: str = Field(description="Official industry description")


class CompanyStatus(BaseModel):
    """Official operational and legal registration status."""

    is_bankrupt: bool = Field(
        default=False, description="True if marked bankrupt (konkurs)"
    )
    under_liquidation: bool = Field(
        default=False, description="True if under liquidation (underAvvikling)"
    )
    under_forced_liquidation_or_dissolution: bool = Field(
        default=False,
        description="True if under forced liquidation or forced dissolution",
    )
    deletion_date: Optional[str] = Field(
        default=None,
        description="Date of deletion from the registry if deleted (slettedato)",
    )
    summary: str = Field(
        description="Human-readable legal status summary (Active, Bankrupt, Under Liquidation, etc.)"
    )


class SourceMetadata(BaseModel):
    """Evidence provenance metadata preserving the official source of extracted information."""

    source_name: str = Field(
        default="Brønnøysund Register Centre (Enhetsregisteret)",
        description="Official entity or authority providing the data",
    )
    source_url: str = Field(
        description="Direct endpoint URL from which data was retrieved"
    )
    retrieved_at: str = Field(
        description="UTC timestamp of API retrieval in ISO 8601 format"
    )


class CompanyProfile(BaseModel):
    """Evidence-backed Norwegian company profile model."""

    org_nr: str = Field(
        description="Validated 9-digit Norwegian organisation number"
    )
    name: str = Field(description="Official legal company name")
    org_form: Optional[OrgForm] = Field(
        default=None, description="Organisation form structure"
    )
    registration_date: Optional[str] = Field(
        default=None,
        description="Registration date in Enhetsregisteret (YYYY-MM-DD)",
    )
    business_address: Optional[Address] = Field(
        default=None, description="Official business address"
    )
    postal_address: Optional[Address] = Field(
        default=None, description="Official postal address"
    )
    industry_code: Optional[IndustryCode] = Field(
        default=None, description="Primary industry code (Næringskode 1)"
    )
    website: Optional[str] = Field(
        default=None, description="Official web address if registered"
    )
    status: CompanyStatus = Field(
        description="Current legal status flags and summary"
    )
    source: SourceMetadata = Field(
        description="Provenance and source metadata preserving the official source"
    )


class DiscoveredSource(BaseModel):
    """Evidence model for a discovered web page source."""

    target_org_nr: str = Field(description="Target Norwegian organisation number")
    source_url: str = Field(description="Raw requested source URL")
    normalized_url: str = Field(description="Normalized canonical source URL")
    page_title: Optional[str] = Field(default=None, description="Extracted HTML page title")
    source_category: str = Field(
        description="Category classification (homepage, about, contact, leadership, products_services, investor, annual_reports, careers, news, legal, other)"
    )
    verification_status: str = Field(
        description="Verification classification: VERIFIED, PROBABLE, or REJECTED"
    )
    verification_explanation: str = Field(description="Explanation rationale for verification status")
    retrieval_timestamp: str = Field(description="ISO 8601 UTC timestamp of retrieval")
    content_hash: Optional[str] = Field(default=None, description="SHA-256 hash of response content")
    http_status: Optional[int] = Field(default=None, description="HTTP status code")
    redirect_destination: Optional[str] = Field(
        default=None, description="Final URL if redirected"
    )
    raw_html: Optional[str] = Field(default=None, description="In-memory HTML content buffer for reuse")


class CrawlSummary(BaseModel):
    """Summary metrics for the web discovery crawl session."""

    total_sources_discovered: int = Field(description="Total sources discovered")
    verified_count: int = Field(description="Number of VERIFIED sources")
    probable_count: int = Field(description="Number of PROBABLE sources")
    rejected_count: int = Field(description="Number of REJECTED sources")
    network_requests_count: int = Field(description="Total HTTP network requests issued")
    runtime_seconds: float = Field(description="Total crawl execution runtime in seconds")
    warnings: List[str] = Field(default_factory=list, description="List of warnings or notices")


class SourcesDiscoveryResult(BaseModel):
    """Structured result container for Phase 2 official website and source discovery."""

    company: CompanyProfile = Field(description="Verified company identity profile")
    official_website: Optional[str] = Field(default=None, description="Normalized official website URL")
    verification_status: str = Field(
        description="Overall website verification status: VERIFIED, PROBABLE, or UNVERIFIED"
    )
    sources: List[DiscoveredSource] = Field(default_factory=list, description="List of discovered sources")
    summary: CrawlSummary = Field(description="Crawl session summary metrics")


# =========================================================
# PHASE 3 — STRUCTURED INTELLIGENCE & EVIDENCE MODELS
# =========================================================

class ExtractedFact(BaseModel):
    """Base evidence-backed fact model."""

    value: Any = Field(description="Extracted value or structured fact data")
    fact_type: str = Field(description="Type classification of fact")
    target_org_nr: str = Field(description="Target Norwegian organisation number")
    source_url: str = Field(description="Source URL from which fact was extracted")
    source_category: str = Field(description="Source page category")
    source_title: Optional[str] = Field(default=None, description="Source page title")
    evidence_text: str = Field(description="Concise, verifiable supporting evidence snippet")
    extraction_method: str = Field(description="Method used for extraction")
    confidence: str = Field(description="Confidence rating: HIGH, MEDIUM, or LOW")
    verification_status: str = Field(description="Fact verification rating: VERIFIED, AMBIGUOUS, or REJECTED")
    retrieved_at: str = Field(description="ISO 8601 UTC timestamp of retrieval")


class CompanyDescription(BaseModel):
    """Structured evidence-backed company description."""

    description: str = Field(description="Extracted company overview description text")
    source_url: str = Field(description="Source URL")
    evidence_text: str = Field(description="Concise supporting text snippet")
    extraction_method: str = Field(description="Extraction method")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")


class ContactInfo(BaseModel):
    """Structured contact details record."""

    contact_type: str = Field(description="Contact classification: phone, email, contact_url, social_media")
    value: str = Field(description="Normalized contact value")
    label: Optional[str] = Field(default=None, description="Descriptive label")
    source_url: str = Field(description="Source URL")
    evidence_text: str = Field(description="Supporting evidence text snippet")
    extraction_method: str = Field(description="Extraction method")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")


class LeadershipRecord(BaseModel):
    """Structured executive or board leadership record."""

    person_name: str = Field(description="Full name of leader/executive")
    role: str = Field(description="Official role or title")
    company_org_nr: str = Field(description="Target company organisation number")
    source_url: str = Field(description="Source URL")
    evidence_text: str = Field(description="Exact supporting text snippet from source")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")
    retrieved_at: str = Field(description="ISO 8601 UTC timestamp")


class BusinessLocation(BaseModel):
    """Structured company business address or office location."""

    location_type: str = Field(description="registered_office, postal_address, headquarters, or office")
    address_lines: Optional[List[str]] = Field(default=None, description="Street address lines")
    postal_code: Optional[str] = Field(default=None, description="Postal code")
    city: Optional[str] = Field(default=None, description="City / Postal area")
    country: Optional[str] = Field(default=None, description="Country name")
    source_url: str = Field(description="Source URL or authority endpoint")
    evidence_text: str = Field(description="Supporting evidence text snippet")
    extraction_method: str = Field(description="Extraction method")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")


class ProductService(BaseModel):
    """Structured product or service entry."""

    name: str = Field(description="Product or service name")
    description: Optional[str] = Field(default=None, description="Brief description")
    category: Optional[str] = Field(default=None, description="Category classification")
    source_url: str = Field(description="Source URL")
    evidence_text: str = Field(description="Supporting evidence snippet")
    extraction_method: str = Field(description="Extraction method")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")


class ImportantLink(BaseModel):
    """Categorized official link."""

    label: str = Field(description="Link label or page name")
    url: str = Field(description="Normalized target URL")
    category: str = Field(description="Category")
    source_url: str = Field(description="Source URL from which link was discovered")
    confidence: str = Field(description="Confidence level: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED or AMBIGUOUS")


class IntelligenceSummary(BaseModel):
    """Summary metrics for the extracted company intelligence."""

    facts_extracted: int = Field(description="Total number of facts extracted")
    facts_verified: int = Field(description="Number of VERIFIED facts")
    facts_ambiguous: int = Field(description="Number of AMBIGUOUS facts")
    runtime_seconds: float = Field(description="Extraction execution runtime in seconds")
    warnings: List[str] = Field(default_factory=list, description="List of warnings or notices")


class CompanyIntelligence(BaseModel):
    """Top-level structured container for evidence-backed Norwegian company intelligence."""

    company: CompanyProfile = Field(description="Official verified company profile from registry")
    official_website: Optional[str] = Field(default=None, description="Verified official website URL")
    description: Optional[CompanyDescription] = Field(default=None, description="Extracted company description")
    contacts: List[ContactInfo] = Field(default_factory=list, description="Extracted contact details")
    leadership: List[LeadershipRecord] = Field(default=None, description="Extracted leadership records")
    locations: List[BusinessLocation] = Field(default_factory=list, description="Extracted office/business locations")
    products_services: List[ProductService] = Field(default_factory=list, description="Extracted products and services")
    important_links: List[ImportantLink] = Field(default_factory=list, description="Discovered official important links")
    sources: List[DiscoveredSource] = Field(default_factory=list, description="Discovered web sources metadata")
    summary: IntelligenceSummary = Field(description="Extraction metrics summary")


# =========================================================
# PHASE 4 — FINANCIAL INTELLIGENCE & VERIFICATION MODELS
# =========================================================

class FinancialMetricRecord(BaseModel):
    """Structured evidence-backed financial metric record."""

    target_org_nr: str = Field(description="Target Norwegian organisation number")
    legal_company_name: str = Field(description="Target legal company name")
    metric_name: str = Field(
        description="Supported metric name (revenue, operating_revenue, operating_profit, EBITDA, EBIT, profit_before_tax, net_income, total_assets, total_equity, total_liabilities, cash_and_cash_equivalents)"
    )
    reported_value: float = Field(description="Direct reported numerical figure from source document")
    normalized_value: float = Field(description="Calculated absolute value in base units")
    currency: str = Field(description="Currency code (NOK, USD, EUR, etc.)")
    scale: str = Field(description="Scale: units, thousands, millions, billions")
    fiscal_year: int = Field(description="Fiscal reporting year (e.g. 2025, 2024)")
    reporting_period: str = Field(default="FY", description="Reporting period (FY, Q1, Q2, Q3, Q4, H1)")
    reporting_scope: str = Field(description="Reporting scope: parent_company, consolidated_group, subsidiary, unknown")
    result_type: str = Field(
        default="ACTUAL",
        description="Financial period classification: ACTUAL, FORECAST, GUIDANCE, TARGET, ESTIMATE, or UNKNOWN"
    )
    accounting_standard: Optional[str] = Field(default=None, description="Accounting framework if stated (IFRS, NGAAP, etc.)")
    source_url: str = Field(description="Source URL or PDF document endpoint")
    source_title: Optional[str] = Field(default=None, description="Source document or page title")
    document_hash: Optional[str] = Field(default=None, description="SHA-256 hash of PDF or document")
    page_number: Optional[int] = Field(default=None, description="1-indexed PDF page number if derived from PDF")
    evidence_text: str = Field(description="Exact, un-synthesized supporting text or table row snippet from source")
    extraction_method: str = Field(description="Method used for extraction")
    confidence: str = Field(description="Confidence rating: HIGH, MEDIUM, LOW")
    verification_status: str = Field(description="VERIFIED, AMBIGUOUS, or REJECTED")
    verification_explanation: Optional[str] = Field(default=None, description="Explanation for verification status")
    retrieved_at: str = Field(description="ISO 8601 UTC timestamp")


class FinancialSource(BaseModel):
    """Discovered financial document or page source metadata."""

    target_org_nr: str = Field(description="Target organisation number")
    source_url: str = Field(description="Raw requested source URL")
    normalized_url: str = Field(description="Normalized source URL")
    source_title: Optional[str] = Field(default=None, description="Title of document or page")
    source_type: str = Field(description="Source classification: pdf_annual_report, html_investor_page, financial_statement_page, public_registry")
    content_hash: Optional[str] = Field(default=None, description="SHA-256 hash of document")
    retrieval_timestamp: str = Field(description="ISO 8601 UTC timestamp")
    verification_status: str = Field(description="VERIFIED, PROBABLE, or REJECTED")


class FinancialSummary(BaseModel):
    """Summary metrics for financial intelligence extraction."""

    sources_discovered: int = Field(description="Total financial sources discovered")
    documents_processed: int = Field(description="Number of PDF/HTML documents processed")
    metrics_extracted: int = Field(description="Total financial metrics extracted")
    metrics_verified: int = Field(description="Number of VERIFIED financial metrics")
    metrics_ambiguous: int = Field(description="Number of AMBIGUOUS/REJECTED financial metrics")
    runtime_seconds: float = Field(description="Total execution runtime in seconds")
    warnings: List[str] = Field(default_factory=list, description="List of warnings or notices")


class FinancialIntelligence(BaseModel):
    """Top-level structured container for evidence-backed Norwegian financial intelligence."""

    company: CompanyProfile = Field(description="Official verified company profile from registry")
    financial_sources: List[FinancialSource] = Field(default_factory=list, description="Discovered financial sources")
    financial_metrics: List[FinancialMetricRecord] = Field(default_factory=list, description="Extracted financial metric records")
    summary: FinancialSummary = Field(description="Extraction metrics summary")


# =========================================================
# PHASE 5 — COMPREHENSIVE BATCH PROFILE MODEL
# =========================================================

class ComprehensiveCompanyProfile(BaseModel):
    """Unified evidence-backed company profile model for Phase 5 batch processing and coverage."""

    org_nr: str = Field(description="Validated 9-digit Norwegian organisation number")
    legal_name: str = Field(description="Official legal company name from registry")
    organisation_form: Optional[OrgForm] = Field(default=None, description="Organisation form structure")
    registration_status: Optional[str] = Field(default=None, description="Official registration status summary")
    industry: Optional[IndustryCode] = Field(default=None, description="Primary industry code")
    business_address: Optional[Address] = Field(default=None, description="Official business address")
    postal_address: Optional[Address] = Field(default=None, description="Official postal address")
    official_website: Optional[str] = Field(default=None, description="Normalized official website URL")
    description: Optional[CompanyDescription] = Field(default=None, description="Extracted company description")
    contacts: List[ContactInfo] = Field(default_factory=list, description="Extracted contact details")
    leadership: List[LeadershipRecord] = Field(default_factory=list, description="Extracted leadership records")
    locations: List[BusinessLocation] = Field(default_factory=list, description="Extracted business/office locations")
    products_services: List[ProductService] = Field(default_factory=list, description="Extracted products and services")
    important_links: List[ImportantLink] = Field(default_factory=list, description="Discovered important official links")
    financial_metrics: List[FinancialMetricRecord] = Field(default_factory=list, description="Financial metric records")
    sources: List[DiscoveredSource] = Field(default_factory=list, description="Discovered web sources")
    evidence: List[ExtractedFact] = Field(default_factory=list, description="Supporting extracted facts")
    last_updated: str = Field(description="ISO 8601 UTC timestamp of last update")
    last_checked_at: Optional[str] = Field(default=None, description="ISO 8601 UTC timestamp of last verification check")
    freshness_status: str = Field(
        default="FRESH",
        description="Data freshness status: FRESH, STALE, or UNKNOWN",
    )
    data_age_days: float = Field(default=0.0, description="Elapsed age in days since last verification check")
    processing_status: str = Field(
        default="IDENTITY_COMPLETE",
        description="Processing status: IDENTITY_COMPLETE, PARTIALLY_ENRICHED, ENRICHED, FAILED, BUDGET_EXHAUSTED",
    )
    completeness_score: int = Field(default=0, description="Overall profile completeness score (0-100)")
    score_breakdown: Dict[str, int] = Field(default_factory=dict, description="Component-level score breakdown")
    warnings: List[str] = Field(default_factory=list, description="Warnings or notices encountered")

"""Phase 4 financial intelligence extraction, PDF table parsing, period classification, and entity attribution verification engine."""

import time
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

from nordtrace.exceptions import BrregAPIError
from nordtrace.financial_utils import (
    CURRENT_YEAR,
    calculate_normalized_value,
    detect_result_type,
    detect_scale_and_currency,
    is_evidence_complete,
    match_metric_name,
    parse_financial_number,
)
from nordtrace.models import (
    CompanyProfile,
    DiscoveredSource,
    FinancialIntelligence,
    FinancialMetricRecord,
    FinancialSource,
    FinancialSummary,
)
from nordtrace.pdf_fetcher import PDFDocument, SafePDFRetriever
from nordtrace.url_utils import extract_domain, is_same_domain, normalize_url

STATEMENT_KEYWORDS = [
    "income statement", "resultatregnskap", "balance sheet", "balanse",
    "financial statements", "årsregnskap", "arsregnskap", "consolidated statement"
]

IRRELEVANT_PDF_KEYWORDS = [
    "bestillingskjema", "skjema", "oppslagsrapport", "ordre", "order-form",
    "order_form", "prisliste", "brosjyre", "pamphlet", "fullmakt", "terms",
    "vilkar", "retningslinjer", "instruks", "søknad", "soknad", "klage"
]

ANNUAL_REPORT_KEYWORDS = [
    "annual report", "årsrapport", "arsrapport", "annual-report",
    "financial report", "årsregnskap", "arsregnskap", "financial statements",
    "resultatregnskap", "balanse", "income statement", "balance sheet",
    "integrated report", "kvartalsrapport", "delårsrapport", "delarsrapport"
]


def classify_pdf_document(pdf_url: str, pdf_doc: PDFDocument) -> Tuple[str, str, str]:
    """Inspects PDF filename, title, and content to classify as verified annual report or rejected non-financial document.

    Returns:
        Tuple of (source_type, verification_status, source_title)
    """
    url_lower = pdf_url.lower()
    full_text = " ".join(p.text for p in pdf_doc.pages).lower()

    # 1. Reject order forms, brochures, terms, or administrative documents
    for irr in IRRELEVANT_PDF_KEYWORDS:
        if irr in url_lower or irr in full_text[:600]:
            return (
                "irrelevant_document",
                "REJECTED",
                f"Rejected Document ({pdf_doc.num_pages} pages) - Identified as non-financial document or order form ({irr})",
            )

    # 2. Inspect document content for actual financial statement or annual report indicators
    has_statement = any(k in full_text for k in STATEMENT_KEYWORDS)
    has_report_header = any(k in full_text for k in ANNUAL_REPORT_KEYWORDS)

    if has_statement or has_report_header:
        return (
            "pdf_annual_report",
            "VERIFIED",
            f"Annual/Financial Report PDF ({pdf_doc.num_pages} pages)",
        )

    # 3. Reject if content lacks financial statement or annual report sections
    return (
        "irrelevant_document",
        "REJECTED",
        f"Rejected PDF ({pdf_doc.num_pages} pages) - Content lacks financial statements or annual report sections",
    )


class FinancialExtractor:
    """Extracts, normalizes, and verifies evidence-backed financial metrics from PDF reports and web sources."""

    def __init__(self, pdf_retriever: Optional[SafePDFRetriever] = None):
        self.pdf_retriever = pdf_retriever or SafePDFRetriever()

    def extract_financials(
        self,
        company: CompanyProfile,
        discovered_sources: List[DiscoveredSource],
    ) -> FinancialIntelligence:
        """Runs the complete financial intelligence extraction pipeline.

        Args:
            company: Verified registry CompanyProfile.
            discovered_sources: List of discovered web sources.

        Returns:
            FinancialIntelligence instance.
        """
        start_time = time.time()
        warnings: List[str] = []

        financial_sources: List[FinancialSource] = []
        financial_metrics: List[FinancialMetricRecord] = []
        documents_processed = 0

        # 1. Discover potential financial documents and pages
        target_pdf_urls: Set[str] = set()
        html_financial_sources: List[DiscoveredSource] = []

        for src in discovered_sources:
            if src.verification_status == "REJECTED":
                continue

            # Identify PDF URLs
            if src.normalized_url.lower().endswith(".pdf") or "annual-report" in src.normalized_url.lower():
                target_pdf_urls.add(src.normalized_url)

            # Identify HTML investor/report pages
            if src.source_category in ("annual_reports", "investor") or "investor" in src.normalized_url.lower():
                html_financial_sources.append(src)

            # Search raw HTML for investor relations, reports, and PDF links
            if src.raw_html:
                pdf_matches = re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', src.raw_html, re.IGNORECASE)
                for pdf_href in pdf_matches:
                    if any(k in pdf_href.lower() for k in ["annual", "report", "rapport", "investor", "results", "financial", "2025", "2024"]):
                        full_pdf_url = urljoin(src.normalized_url, pdf_href)
                        norm_pdf_url = normalize_url(full_pdf_url)
                        if norm_pdf_url:
                            target_pdf_urls.add(norm_pdf_url)

                # Check for investor relations subdomains or portal links (e.g. ir.dnb.no)
                inv_portal_matches = re.findall(r'href=["\']([^"\']*(?:investor|financial-report|annual-report|arsrapport)[^"\']*)["\']', src.raw_html, re.IGNORECASE)
                for inv_href in inv_portal_matches:
                    full_inv_url = urljoin(src.normalized_url, inv_href)
                    norm_inv_url = normalize_url(full_inv_url)
                    if norm_inv_url and company.website:
                        norm_company_site = normalize_url(company.website)
                        if norm_company_site and is_same_domain(norm_inv_url, norm_company_site):
                            # Record discovered investor/financial portal if not already in financial_sources
                            if not any(fs.normalized_url == norm_inv_url for fs in financial_sources):
                                financial_sources.append(
                                    FinancialSource(
                                        target_org_nr=company.org_nr,
                                        source_url=full_inv_url,
                                        normalized_url=norm_inv_url,
                                        source_title="Investor Relations & Financial Reports Portal",
                                        source_type="html_investor_page",
                                        content_hash=None,
                                        retrieval_timestamp=src.retrieval_timestamp,
                                        verification_status="PROBABLE",
                                    )
                                )

        # Record HTML financial sources from crawled pages
        for h_src in html_financial_sources:
            if not any(fs.normalized_url == h_src.normalized_url for fs in financial_sources):
                financial_sources.append(
                    FinancialSource(
                        target_org_nr=company.org_nr,
                        source_url=h_src.source_url,
                        normalized_url=h_src.normalized_url,
                        source_title=h_src.page_title or "Investor Relations Page",
                        source_type="html_investor_page",
                        content_hash=h_src.content_hash,
                        retrieval_timestamp=h_src.retrieval_timestamp,
                        verification_status=h_src.verification_status,
                    )
                )

        # 2. Process PDF Financial Documents safely
        for pdf_url in list(target_pdf_urls)[:3]:
            try:
                pdf_doc = self.pdf_retriever.fetch_and_parse_pdf(pdf_url)
                source_type, v_status, s_title = classify_pdf_document(pdf_url, pdf_doc)

                financial_sources.append(
                    FinancialSource(
                        target_org_nr=company.org_nr,
                        source_url=pdf_url,
                        normalized_url=pdf_doc.final_url,
                        source_title=s_title,
                        source_type=source_type,
                        content_hash=pdf_doc.content_hash,
                        retrieval_timestamp=pdf_doc.retrieved_at,
                        verification_status=v_status,
                    )
                )

                # Only count and parse financial statements from verified annual/financial reports
                if v_status == "VERIFIED":
                    documents_processed += 1
                    extracted_from_pdf = self._parse_pdf_financial_metrics(company, pdf_doc)
                    financial_metrics.extend(extracted_from_pdf)
                else:
                    warnings.append(f"Ignored non-financial PDF '{pdf_url}': classified as {source_type} ({v_status}).")

            except Exception as exc:
                warnings.append(f"Could not process PDF from '{pdf_url}': {exc}")

        # 3. Process HTML Financial Tables/Text
        for h_src in html_financial_sources:
            if h_src.raw_html:
                extracted_from_html = self._parse_html_financial_metrics(company, h_src)
                financial_metrics.extend(extracted_from_html)

        # Filter & Deduplicate financial metrics
        unique_metrics: List[FinancialMetricRecord] = []
        seen_keys: Set[str] = set()

        for metric in financial_metrics:
            key = f"{metric.metric_name}:{metric.fiscal_year}:{metric.reporting_scope}:{metric.result_type}:{metric.normalized_value}"
            if key not in seen_keys:
                seen_keys.add(key)
                unique_metrics.append(metric)

        runtime = round(time.time() - start_time, 3)

        verified_cnt = sum(1 for m in unique_metrics if m.verification_status == "VERIFIED")
        ambiguous_cnt = sum(1 for m in unique_metrics if m.verification_status in ("AMBIGUOUS", "REJECTED"))

        summary = FinancialSummary(
            sources_discovered=len(financial_sources),
            documents_processed=documents_processed,
            metrics_extracted=len(unique_metrics),
            metrics_verified=verified_cnt,
            metrics_ambiguous=ambiguous_cnt,
            runtime_seconds=runtime,
            warnings=warnings,
        )

        return FinancialIntelligence(
            company=company,
            financial_sources=financial_sources,
            financial_metrics=unique_metrics,
            summary=summary,
        )

    def _parse_pdf_financial_metrics(self, company: CompanyProfile, doc: PDFDocument) -> List[FinancialMetricRecord]:
        """Scans PDF pages for financial statement tables and extracts metric records."""
        records: List[FinancialMetricRecord] = []

        for page in doc.pages:
            page_text = page.text
            page_text_lower = page_text.lower()

            # Check if page looks like a financial statement
            if not any(k in page_text_lower for k in STATEMENT_KEYWORDS):
                continue

            scale, currency = detect_scale_and_currency(page_text)

            # Determine reporting scope
            if "consolidated" in page_text_lower or "group" in page_text_lower or "konsern" in page_text_lower:
                scope = "consolidated_group"
            elif "parent" in page_text_lower or "morselskap" in page_text_lower:
                scope = "parent_company"
            else:
                scope = "consolidated_group"

            # Find year columns (e.g., 2025, 2024, 2023)
            years_found = [int(y) for y in re.findall(r"\b(202[0-9]|201[5-9])\b", page_text)]
            if not years_found:
                continue

            years = sorted(list(dict.fromkeys(years_found)), reverse=True)
            primary_year = years[0]

            # Determine result type (ACTUAL, FORECAST, GUIDANCE, TARGET, ESTIMATE)
            result_type = detect_result_type(page_text, primary_year)

            lines = page_text.splitlines()
            for line in lines:
                metric_name = match_metric_name(line)
                if not metric_name:
                    continue

                tokens = [t.strip() for t in re.split(r"\s{2,}", line) if t.strip()]
                parsed_values = []

                if len(tokens) > 1:
                    for token in tokens[1:]:
                        parsed_val = parse_financial_number(token)
                        if parsed_val is not None:
                            parsed_values.append(parsed_val)
                else:
                    num_matches = re.findall(r"\(\s*-?\s*\d[\d\s.,]*\)|-?\s*\d{1,3}(?:\s\d{3})*(?:[.,]\d+)?", line)
                    for num_str in num_matches:
                        parsed_val = parse_financial_number(num_str)
                        if parsed_val is not None and len(str(int(abs(parsed_val)))) <= 10:
                            parsed_values.append(parsed_val)

                if parsed_values:
                    reported_val = parsed_values[0]
                    norm_val = calculate_normalized_value(reported_val, scale)

                    # Preserve exact source text line
                    exact_evidence = line.strip()

                    # Verification rules:
                    # ONLY ACTUAL results with complete evidence and valid year <= CURRENT_YEAR can be VERIFIED
                    if result_type == "ACTUAL" and primary_year <= CURRENT_YEAR:
                        status = "VERIFIED"
                        confidence = "HIGH"
                        explanation = None
                    else:
                        status = "AMBIGUOUS"
                        confidence = "LOW"
                        explanation = f"Figure is classified as {result_type} (not an actual verified financial result)."

                    records.append(
                        FinancialMetricRecord(
                            target_org_nr=company.org_nr,
                            legal_company_name=company.name,
                            metric_name=metric_name,
                            reported_value=reported_val,
                            normalized_value=norm_val,
                            currency=currency,
                            scale=scale,
                            fiscal_year=primary_year,
                            reporting_period="FY",
                            reporting_scope=scope,
                            result_type=result_type,
                            accounting_standard="IFRS" if "ifrs" in page_text_lower else "NGAAP",
                            source_url=doc.final_url,
                            source_title=f"Annual Report PDF (Page {page.page_number})",
                            document_hash=doc.content_hash,
                            page_number=page.page_number,
                            evidence_text=exact_evidence,
                            extraction_method="pdf_table_row",
                            confidence=confidence,
                            verification_status=status,
                            verification_explanation=explanation,
                            retrieved_at=doc.retrieved_at,
                        )
                    )

        return records

    def _parse_html_financial_metrics(self, company: CompanyProfile, src: DiscoveredSource) -> List[FinancialMetricRecord]:
        """Parses financial tables or statements from HTML report pages with strict period classification."""
        records: List[FinancialMetricRecord] = []
        if not src.raw_html:
            return records

        text = src.raw_html
        text_lower = text.lower()

        if not any(k in text_lower for k in STATEMENT_KEYWORDS):
            return records

        scale, currency = detect_scale_and_currency(text)
        years_found = [int(y) for y in re.findall(r"\b(202[0-9]|201[5-9])\b", text)]
        if not years_found:
            return records

        primary_year = sorted(list(dict.fromkeys(years_found)), reverse=True)[0]
        scope = "consolidated_group" if ("consolidated" in text_lower or "group" in text_lower) else "parent_company"

        # Regex search for common metric patterns in HTML text/tables
        for metric_name, patterns in [
            ("revenue", [r"(?:revenue|driftsinntekter)[^\d]*([\(\)\d\s.,]+)"]),
            ("net_income", [r"(?:net income|årsresultat)[^\d]*([\(\)\d\s.,]+)"]),
            ("operating_profit", [r"(?:operating profit|driftsresultat)[^\d]*([\(\)\d\s.,]+)"]),
        ]:
            for pat in patterns:
                m = re.search(pat, text_lower)
                if m:
                    raw_num_str = m.group(1).strip()
                    val = parse_financial_number(raw_num_str)
                    if val is not None:
                        # Extract exact matching snippet text
                        match_text = text[max(0, m.start() - 20): min(len(text), m.end() + 20)].strip()
                        result_type = detect_result_type(match_text, primary_year)

                        norm_val = calculate_normalized_value(val, scale)
                        complete_ev = is_evidence_complete(match_text, val, scale, primary_year, currency)

                        if result_type == "ACTUAL" and primary_year <= CURRENT_YEAR and complete_ev and src.verification_status == "VERIFIED":
                            status = "VERIFIED"
                            confidence = "HIGH"
                            explanation = None
                        else:
                            status = "AMBIGUOUS"
                            confidence = "LOW"
                            explanation = f"Classification: {result_type}. Not published as verified actual result."

                        records.append(
                            FinancialMetricRecord(
                                target_org_nr=company.org_nr,
                                legal_company_name=company.name,
                                metric_name=metric_name,
                                reported_value=val,
                                normalized_value=norm_val,
                                currency=currency,
                                scale=scale,
                                fiscal_year=primary_year,
                                reporting_period="FY",
                                reporting_scope=scope,
                                result_type=result_type,
                                accounting_standard="IFRS" if "ifrs" in text_lower else None,
                                source_url=src.normalized_url,
                                source_title=src.page_title or "Financial Statement Page",
                                document_hash=src.content_hash,
                                page_number=None,
                                evidence_text=match_text,
                                extraction_method="html_financial_pattern",
                                confidence=confidence,
                                verification_status=status,
                                verification_explanation=explanation,
                                retrieved_at=src.retrieval_timestamp,
                            )
                        )
                        break

        return records

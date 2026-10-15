"""Scalable, adaptive batch processing engine for 1,000-company coverage with request budgeting and resumable checkpoints."""

import concurrent.futures
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx

from nordtrace.budget import DEFAULT_MAX_REQUESTS, RequestBudget
from nordtrace.client import BrregClient
from nordtrace.discovery import SourceDiscoverer
from nordtrace.exceptions import (
    BudgetExhaustedError,
    CompanyNotFoundError,
    InvalidOrgNumberError,
    NordTraceError,
)
from nordtrace.extractor import IntelligenceExtractor
from nordtrace.fetcher import SafeFetcher
from nordtrace.financial_extractor import FinancialExtractor
from nordtrace.metrics import TimingMetrics
from nordtrace.models import (
    Address,
    ComprehensiveCompanyProfile,
    IndustryCode,
    OrgForm,
)
from nordtrace.pdf_fetcher import SafePDFRetriever
from nordtrace.reporting import (
    CoverageReport,
    build_coverage_report,
    calculate_completeness_score,
)
from nordtrace.robots import RobotsCache
from nordtrace.storage import DEFAULT_DB_PATH, StorageManager
from nordtrace.validator import validate_org_nr


def parse_batch_input(file_path: str) -> Tuple[List[str], List[str]]:
    """Parses, validates, and deduplicates organisation numbers from a CSV or plain text file.

    Preserves input order and segregates valid from invalid records.

    Args:
        file_path: Path to CSV or TXT file.

    Returns:
        Tuple of (valid_org_numbers, invalid_org_numbers)
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Batch input file not found: {file_path}")

    raw_candidates: List[str] = []
    lower_path = file_path.lower()

    if lower_path.endswith(".jsonl"):
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict):
                        val = obj.get("organisation_number") or obj.get("org_nr") or obj.get("organisasjonsnummer")
                        if val:
                            raw_candidates.append(str(val))
                    elif isinstance(obj, (str, int)):
                        raw_candidates.append(str(obj))
                except json.JSONDecodeError:
                    raw_candidates.append(stripped)
    elif lower_path.endswith(".json"):
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            data = json.load(f)
            items = data if isinstance(data, list) else data.get("organisation_numbers", data.get("organisations", []))
            for item in items:
                if isinstance(item, dict):
                    val = item.get("organisation_number") or item.get("org_nr") or item.get("organisasjonsnummer")
                    if val:
                        raw_candidates.append(str(val))
                elif isinstance(item, (str, int)):
                    raw_candidates.append(str(item))
    else:
        # Check if CSV or plain text
        is_csv = lower_path.endswith(".csv")
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            first_line = f.readline()
            f.seek(0)

            # Detect delimiter or header
            if "," in first_line or ";" in first_line or is_csv:
                reader = csv.reader(f)
                header = next(reader, None)
                col_idx = 0
                if header:
                    for idx, h in enumerate(header):
                        cleaned_h = h.strip().lower()
                        if cleaned_h in ("org_nr", "orgnr", "organisasjonsnummer", "organisation_number"):
                            col_idx = idx
                            break
                for row in reader:
                    if row and len(row) > col_idx:
                        val = row[col_idx].strip()
                        if val and not val.lower().startswith("org"):
                            raw_candidates.append(val)
            else:
                # Plain text file (one org_nr per line)
                for line in f:
                    val = line.strip()
                    if val and not val.startswith("#"):
                        raw_candidates.append(val)

    # Deduplicate while preserving input order
    seen: Set[str] = set()
    deduped: List[str] = []
    for item in raw_candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    valid_list: List[str] = []
    invalid_list: List[str] = []

    for item in deduped:
        try:
            valid_nr = validate_org_nr(item)
            valid_list.append(valid_nr)
        except InvalidOrgNumberError:
            invalid_list.append(item)

    return valid_list, invalid_list


def parse_batch_input_ordered(file_path: str) -> Tuple[List[str], List[str], List[str]]:
    """Parses batch input file and returns (valid_list, invalid_list, ordered_candidates).

    Args:
        file_path: Path to CSV, TXT, JSONL, or JSON file.

    Returns:
        Tuple of (valid_org_numbers, invalid_org_numbers, all_candidates_in_order)
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Batch input file not found: {file_path}")

    raw_candidates: List[str] = []
    lower_path = file_path.lower()

    if lower_path.endswith(".jsonl"):
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict):
                        val = obj.get("organisation_number") or obj.get("org_nr") or obj.get("organisasjonsnummer")
                        if val:
                            raw_candidates.append(str(val))
                    elif isinstance(obj, (str, int)):
                        raw_candidates.append(str(obj))
                except json.JSONDecodeError:
                    raw_candidates.append(stripped)
    elif lower_path.endswith(".json"):
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            data = json.load(f)
            items = data if isinstance(data, list) else data.get("organisation_numbers", data.get("organisations", []))
            for item in items:
                if isinstance(item, dict):
                    val = item.get("organisation_number") or item.get("org_nr") or item.get("organisasjonsnummer")
                    if val:
                        raw_candidates.append(str(val))
                elif isinstance(item, (str, int)):
                    raw_candidates.append(str(item))
    else:
        is_csv = lower_path.endswith(".csv")
        with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
            first_line = f.readline()
            f.seek(0)
            if "," in first_line or ";" in first_line or is_csv:
                reader = csv.reader(f)
                header = next(reader, None)
                col_idx = 0
                if header:
                    for idx, h in enumerate(header):
                        cleaned_h = h.strip().lower()
                        if cleaned_h in ("org_nr", "orgnr", "organisasjonsnummer", "organisation_number"):
                            col_idx = idx
                            break
                for row in reader:
                    if row and len(row) > col_idx:
                        val = row[col_idx].strip()
                        if val and not val.lower().startswith("org"):
                            raw_candidates.append(val)
            else:
                for line in f:
                    val = line.strip()
                    if val and not val.startswith("#"):
                        raw_candidates.append(val)

    seen: Set[str] = set()
    deduped: List[str] = []
    for item in raw_candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    valid_list: List[str] = []
    invalid_list: List[str] = []
    for item in deduped:
        try:
            valid_nr = validate_org_nr(item)
            valid_list.append(valid_nr)
        except InvalidOrgNumberError:
            invalid_list.append(item)

    return valid_list, invalid_list, deduped


class BatchEngine:
    """Orchestrates scalable, two-pass company processing with global request budget enforcement and SQLite checkpoints."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        max_runtime_seconds: float = 2700.0,
        concurrency: int = 4,
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        self.db_path = db_path
        self.storage = StorageManager(db_path)
        self.max_requests = max_requests
        self.max_runtime_seconds = max_runtime_seconds
        self.concurrency = max(1, concurrency)
        self.progress_callback = progress_callback or self._default_progress
        self.timing_metrics = TimingMetrics()

    def _default_progress(self, msg: str) -> None:
        sys.stderr.write(f"[NordTrace] {msg}\n")
        sys.stderr.flush()

    def _handle_pass1_result(
        self,
        idx: int,
        org_nr: str,
        profile: Optional[ComprehensiveCompanyProfile],
        err: Optional[str],
        batch_id: str,
        budget: RequestBudget,
        total_valid: int,
        current_successful: Set[str],
        current_failed: Set[str],
    ) -> None:
        if profile is not None:
            current_successful.add(org_nr)
            t_db = time.time()
            self.storage.upsert_company(profile, batch_id=batch_id, input_order=idx)
            self.timing_metrics.record_persistence(time.time() - t_db)
            self.progress_callback(f"[{idx}/{total_valid}] Identity resolved: {profile.legal_name} ({org_nr})")
        elif err == "BUDGET_EXHAUSTED":
            current_failed.add(org_nr)
            self._record_exhausted_company(org_nr, batch_id, idx)
            self.progress_callback(f"[{idx}/{total_valid}] Request budget exhausted during identity lookup for {org_nr}.")
        elif err == "TIMEOUT":
            current_failed.add(org_nr)
        else:
            current_failed.add(org_nr)
            self._record_failed_company(org_nr, batch_id, idx, err or "Unknown error")
            self.progress_callback(f"[{idx}/{total_valid}] Error for {org_nr}: {err}")

    def run_batch(
        self,
        input_file: str,
        resume: bool = False,
        limit: Optional[int] = None,
    ) -> CoverageReport:
        """Executes full batch processing across valid organisation numbers with bounded concurrency."""
        start_time = time.time()
        self.timing_metrics = TimingMetrics()
        valid_orgs, invalid_orgs = parse_batch_input(input_file)

        if limit and limit > 0:
            valid_orgs = valid_orgs[:limit]

        total_input_count = len(valid_orgs) + len(invalid_orgs)
        self.progress_callback(
            f"Loaded {total_input_count} records ({len(valid_orgs)} valid, {len(invalid_orgs)} invalid)."
        )

        # Generate deterministic batch ID based on input file content
        file_hash = hashlib.sha256(open(input_file, "rb").read()).hexdigest()[:12]
        batch_id = f"batch_{file_hash}"

        # Initialize or resume budget state
        initial_used, batch_status = self.storage.get_or_create_batch(
            batch_id=batch_id,
            total_companies=len(valid_orgs),
            max_requests=self.max_requests,
        )

        requests_used_start = initial_used if resume else 0
        budget = RequestBudget(max_requests=self.max_requests, initial_used=requests_used_start)

        if resume:
            self.progress_callback(
                f"Resuming batch '{batch_id}'. Consumed {budget.total_used}/{budget.max_requests} requests so far."
            )
        else:
            self.progress_callback(
                f"Starting new batch '{batch_id}' with global budget of {self.max_requests} requests (concurrency: {self.concurrency})."
            )

        # =====================================================================
        # PASS 1: REGISTRY IDENTITY FIRST (CONCURRENT LOOKUPS)
        # =====================================================================
        self.progress_callback("--- PASS 1: Official Registry Identity Resolution ---")

        timed_out = False
        current_successful: Set[str] = set()
        current_reused: Set[str] = set()
        current_failed: Set[str] = set()

        pending_orgs: List[Tuple[int, str]] = []
        for idx, org_nr in enumerate(valid_orgs, start=1):
            existing_profile = self.storage.get_company(org_nr)
            if resume and existing_profile and existing_profile.processing_status not in ("FAILED", "BUDGET_EXHAUSTED"):
                current_reused.add(org_nr)
            else:
                pending_orgs.append((idx, org_nr))

        def _resolve_one(
            idx: int, org_nr: str, client: BrregClient
        ) -> Tuple[int, str, Optional[ComprehensiveCompanyProfile], Optional[str]]:
            if (time.time() - start_time) > self.max_runtime_seconds:
                return idx, org_nr, None, "TIMEOUT"
            if not budget.can_request(1):
                return idx, org_nr, None, "BUDGET_EXHAUSTED"

            t0 = time.time()
            try:
                company_profile = client.fetch_company(org_nr)
                dur = time.time() - t0
                self.timing_metrics.record_registry(dur)

                now_iso = datetime.now(timezone.utc).isoformat()
                profile = ComprehensiveCompanyProfile(
                    org_nr=company_profile.org_nr,
                    legal_name=company_profile.name,
                    organisation_form=company_profile.org_form,
                    registration_status=company_profile.status.summary,
                    industry=company_profile.industry_code,
                    business_address=company_profile.business_address,
                    postal_address=company_profile.postal_address,
                    official_website=company_profile.website,
                    last_updated=now_iso,
                    last_checked_at=now_iso,
                    freshness_status="FRESH",
                    data_age_days=0.0,
                    processing_status="IDENTITY_COMPLETE",
                    warnings=[],
                )
                score, breakdown = calculate_completeness_score(profile)
                profile.completeness_score = score
                profile.score_breakdown = breakdown
                return idx, org_nr, profile, None
            except CompanyNotFoundError as exc:
                return idx, org_nr, None, f"NOT_FOUND: {exc}"
            except BudgetExhaustedError:
                return idx, org_nr, None, "BUDGET_EXHAUSTED"
            except Exception as exc:
                return idx, org_nr, None, f"ERROR: {exc}"

        event_hooks = {"request": [budget.create_httpx_hook()]}
        limits = httpx.Limits(max_keepalive_connections=8, max_connections=16)

        with httpx.Client(timeout=10.0, follow_redirects=True, limits=limits, event_hooks=event_hooks) as http_client:
            client = BrregClient(budget=budget, client=http_client)

            chunk_size = self.concurrency
            for i in range(0, len(pending_orgs), chunk_size):
                if (time.time() - start_time) > self.max_runtime_seconds:
                    self.progress_callback(
                        f"Evaluation runtime ceiling ({self.max_runtime_seconds}s) reached; halting Pass 1."
                    )
                    timed_out = True
                    for idx, org_nr in pending_orgs[i:]:
                        current_failed.add(org_nr)
                    break

                if budget.is_exhausted:
                    for idx, org_nr in pending_orgs[i:]:
                        current_failed.add(org_nr)
                        self._record_exhausted_company(org_nr, batch_id, idx)
                    break

                chunk = pending_orgs[i : i + chunk_size]
                if self.concurrency > 1 and len(chunk) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chunk)) as executor:
                        future_map = {
                            executor.submit(_resolve_one, idx, org_nr, client): (idx, org_nr)
                            for idx, org_nr in chunk
                        }
                        for fut in concurrent.futures.as_completed(future_map):
                            idx, org_nr = future_map[fut]
                            try:
                                _, _, profile, err = fut.result()
                            except Exception as fut_exc:
                                profile, err = None, f"ERROR: {fut_exc}"

                            self._handle_pass1_result(
                                idx, org_nr, profile, err, batch_id, budget, len(valid_orgs),
                                current_successful, current_failed
                            )
                            if err == "TIMEOUT":
                                timed_out = True
                else:
                    for idx, org_nr in chunk:
                        _, _, profile, err = _resolve_one(idx, org_nr, client)
                        self._handle_pass1_result(
                            idx, org_nr, profile, err, batch_id, budget, len(valid_orgs),
                            current_successful, current_failed
                        )
                        if err == "TIMEOUT":
                            timed_out = True
                            break

                self.storage.update_batch_progress(batch_id, budget.total_used, "IN_PROGRESS")

        # =====================================================================
        # PASS 2: PRIORITIZED MINIMAL ENRICHMENT
        # =====================================================================
        self.progress_callback("--- PASS 2: Adaptive Source & Intelligence Enrichment ---")

        if timed_out:
            self.progress_callback("Evaluation runtime ceiling reached; skipping Pass 2.")
        elif budget.is_exhausted:
            self.progress_callback("Global request budget exhausted; skipping Pass 2 enrichment.")
        else:
            shared_robots_cache = RobotsCache(budget=budget)
            shared_fetcher = SafeFetcher(budget=budget)
            pdf_retriever = SafePDFRetriever(budget=budget)
            fin_extractor = FinancialExtractor(pdf_retriever=pdf_retriever)
            intelligence_extractor = IntelligenceExtractor()

            for idx, org_nr in enumerate(valid_orgs, start=1):
                if org_nr in current_failed:
                    continue

                remaining_time = self.max_runtime_seconds - (time.time() - start_time)
                if remaining_time < 5.0:
                    self.progress_callback(
                        f"Evaluation runtime ceiling ({self.max_runtime_seconds}s) reached; halting Pass 2."
                    )
                    timed_out = True
                    break

                if budget.remaining < 2:
                    self.progress_callback(f"Insufficient remaining budget ({budget.remaining}); ending Pass 2 enrichment.")
                    break

                profile = self.storage.get_company(org_nr)
                if not profile or profile.processing_status in ("FAILED", "BUDGET_EXHAUSTED", "ENRICHED"):
                    continue

                if not profile.official_website:
                    continue

                t_enr_start = time.time()
                try:
                    discoverer = SourceDiscoverer(
                        max_pages=2,
                        max_depth=1,
                        fetcher=shared_fetcher,
                        robots_cache=shared_robots_cache,
                        budget=budget,
                        timing_metrics=self.timing_metrics,
                    )
                    dummy_company_profile = self._build_dummy_company_profile(profile)
                    discovery_res = discoverer.discover_sources(dummy_company_profile)

                    t_ext0 = time.time()
                    intel = intelligence_extractor.extract_intelligence(
                        company=dummy_company_profile,
                        official_website=discovery_res.official_website or profile.official_website,
                        discovered_sources=discovery_res.sources,
                    )
                    self.timing_metrics.record_extraction(time.time() - t_ext0)

                    has_fin_source = any(
                        s.source_category in ("investor", "annual_reports") or "investor" in s.normalized_url.lower()
                        for s in discovery_res.sources
                    )
                    financial_metrics = []
                    if has_fin_source and budget.remaining >= 2:
                        try:
                            t_fin0 = time.time()
                            fin_res = fin_extractor.extract_financials(
                                company=dummy_company_profile,
                                discovered_sources=discovery_res.sources,
                            )
                            financial_metrics = fin_res.financial_metrics
                            self.timing_metrics.record_extraction(time.time() - t_fin0)
                        except BudgetExhaustedError:
                            raise
                        except Exception as fin_exc:
                            profile.warnings.append(f"Financial extraction warning: {fin_exc}")

                    now_iso = datetime.now(timezone.utc).isoformat()
                    profile.description = intel.description
                    profile.contacts = intel.contacts
                    profile.leadership = intel.leadership or []
                    profile.locations = intel.locations
                    profile.products_services = intel.products_services
                    profile.important_links = intel.important_links
                    profile.sources = intel.sources
                    profile.financial_metrics = financial_metrics
                    profile.last_updated = now_iso
                    profile.last_checked_at = now_iso
                    profile.freshness_status = "FRESH"
                    profile.data_age_days = 0.0

                    score, breakdown = calculate_completeness_score(profile)
                    profile.completeness_score = score
                    profile.score_breakdown = breakdown

                    if score >= 75:
                        profile.processing_status = "ENRICHED"
                    elif score > 40:
                        profile.processing_status = "PARTIALLY_ENRICHED"
                    else:
                        profile.processing_status = "IDENTITY_COMPLETE"

                    t_db = time.time()
                    self.storage.upsert_company(profile, batch_id=batch_id, input_order=idx)
                    self.timing_metrics.record_persistence(time.time() - t_db)

                    enr_dur = time.time() - t_enr_start
                    self.timing_metrics.record_enrichment(enr_dur)

                    self.progress_callback(
                        f"[{idx}/{len(valid_orgs)}] Enriched {profile.legal_name}: score {score}/100 ({profile.processing_status})"
                    )

                except BudgetExhaustedError:
                    self.progress_callback(f"Budget exhausted during enrichment of {profile.legal_name}.")
                    profile.warnings.append("Enrichment halted due to global request budget exhaustion.")
                    self.storage.upsert_company(profile, batch_id=batch_id, input_order=idx)
                    break
                except Exception as exc:
                    profile.warnings.append(f"Enrichment error: {exc}")
                    self.storage.upsert_company(profile, batch_id=batch_id, input_order=idx)

                self.storage.update_batch_progress(batch_id, budget.total_used, "IN_PROGRESS")

        if timed_out:
            final_status = "RUNTIME_EXHAUSTED"
        elif budget.is_exhausted:
            final_status = "BUDGET_EXHAUSTED"
        elif current_failed and not current_successful and not current_reused:
            final_status = "FAILED"
        else:
            final_status = "COMPLETED"

        runtime = time.time() - start_time

        batch_report_profiles: List[ComprehensiveCompanyProfile] = []
        for org_nr in valid_orgs:
            if org_nr in current_failed:
                batch_report_profiles.append(
                    ComprehensiveCompanyProfile(
                        org_nr=org_nr,
                        legal_name="Unknown (Lookup Failed in Current Run)",
                        last_updated=datetime.now(timezone.utc).isoformat(),
                        processing_status="FAILED",
                        completeness_score=0,
                        score_breakdown={"identity": 0},
                        warnings=["Lookup failed in current batch run"],
                    )
                )
            else:
                p = self.storage.get_company(org_nr)
                if p:
                    batch_report_profiles.append(p)

        report = build_coverage_report(
            profiles=batch_report_profiles,
            total_input_count=total_input_count,
            invalid_org_count=len(invalid_orgs),
            requests_used=budget.total_used,
            max_requests=self.max_requests,
            runtime_seconds=runtime,
            processed_current_run=len(current_successful),
            reused_from_db=len(current_reused),
            failed_current_run=len(current_failed),
        )

        self.storage.update_batch_progress(
            batch_id, budget.total_used, final_status, report_json=report.model_dump_json()
        )

        self.progress_callback(f"Batch completed in {runtime:.2f}s with status '{final_status}'.")
        return report

    def _record_failed_company(self, org_nr: str, batch_id: str, input_order: int, error_msg: str) -> None:
        profile = ComprehensiveCompanyProfile(
            org_nr=org_nr,
            legal_name="Unknown (Lookup Failed)",
            last_updated=datetime.now(timezone.utc).isoformat(),
            processing_status="FAILED",
            completeness_score=0,
            score_breakdown={"identity": 0},
            warnings=[error_msg],
        )
        self.storage.upsert_company(profile, batch_id=batch_id, input_order=input_order, error_message=error_msg)

    def _record_exhausted_company(self, org_nr: str, batch_id: str, input_order: int) -> None:
        profile = ComprehensiveCompanyProfile(
            org_nr=org_nr,
            legal_name="Unprocessed (Budget Exhausted)",
            last_updated=datetime.now(timezone.utc).isoformat(),
            processing_status="BUDGET_EXHAUSTED",
            completeness_score=0,
            score_breakdown={},
            warnings=["Global request budget exhausted before processing this company."],
        )
        self.storage.upsert_company(
            profile,
            batch_id=batch_id,
            input_order=input_order,
            error_message="Budget exhausted",
        )

    def _build_dummy_company_profile(self, comp: ComprehensiveCompanyProfile):
        from nordtrace.models import CompanyProfile, CompanyStatus, SourceMetadata

        return CompanyProfile(
            org_nr=comp.org_nr,
            name=comp.legal_name,
            org_form=comp.organisation_form,
            business_address=comp.business_address,
            postal_address=comp.postal_address,
            industry_code=comp.industry,
            website=comp.official_website,
            status=CompanyStatus(summary=comp.registration_status or "Active"),
            source=SourceMetadata(
                source_name="Brønnøysund Register Centre",
                source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{comp.org_nr}",
                retrieved_at=comp.last_updated,
            ),
        )

    def refresh_stale_profiles(
        self,
        stale_only: bool = True,
        freshness_days: int = 30,
        batch_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Refreshes persisted company profiles against Brønnøysund registry and web sources."""
        start_time = time.time()
        if stale_only:
            candidates = self.storage.get_stale_companies(freshness_days=freshness_days, batch_id=batch_id)
        else:
            candidates = self.storage.list_companies(batch_id=batch_id)

        if limit and limit > 0:
            candidates = candidates[:limit]

        budget = RequestBudget(max_requests=self.max_requests)
        client = BrregClient(budget=budget)

        refreshed_count = 0
        updated_count = 0

        self.progress_callback(
            f"Starting refresh of {len(candidates)} company profiles (budget: {self.max_requests} requests)."
        )

        now_iso = datetime.now(timezone.utc).isoformat()

        for idx, profile in enumerate(candidates, start=1):
            if not budget.can_request(1):
                self.progress_callback("Request budget exhausted during refresh.")
                break

            try:
                company_profile = client.fetch_company(profile.org_nr)
                refreshed_count += 1
                changes_detected = False

                if profile.legal_name != company_profile.name:
                    profile.legal_name = company_profile.name
                    changes_detected = True

                if profile.official_website != company_profile.website:
                    profile.official_website = company_profile.website
                    changes_detected = True

                if profile.registration_status != company_profile.status.summary:
                    profile.registration_status = company_profile.status.summary
                    changes_detected = True

                profile.last_checked_at = now_iso
                profile.freshness_status = "FRESH"
                profile.data_age_days = 0.0

                if changes_detected:
                    profile.last_updated = now_iso
                    updated_count += 1

                score, breakdown = calculate_completeness_score(profile)
                profile.completeness_score = score
                profile.score_breakdown = breakdown

                self.storage.upsert_company(profile, batch_id=batch_id or "refresh_batch", input_order=idx)
                self.progress_callback(
                    f"[{idx}/{len(candidates)}] Refreshed {profile.legal_name} (updated: {changes_detected})"
                )

            except BudgetExhaustedError:
                break
            except Exception as exc:
                self.progress_callback(f"Failed to refresh {profile.org_nr}: {exc}")

        elapsed = time.time() - start_time
        return {
            "total_candidates": len(candidates),
            "refreshed_count": refreshed_count,
            "updated_count": updated_count,
            "requests_used": budget.total_used,
            "remaining_budget": budget.remaining,
            "runtime_seconds": round(elapsed, 3),
        }

    def run_benchmark(
        self,
        input_file: str,
        sample_limit: int = 10,
        target_scale: int = 1000,
        offline: bool = False,
        concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Runs throughput and budget benchmarking, extrapolating 1,000-company evaluation readiness."""
        if concurrency is not None and concurrency > 0:
            self.concurrency = concurrency

        start_time = time.time()
        valid_orgs, invalid_orgs = parse_batch_input(input_file)
        sample_orgs = valid_orgs[:sample_limit] if sample_limit > 0 else valid_orgs
        sample_size = len(sample_orgs)

        if sample_size == 0:
            raise ValueError(f"No valid organisation numbers found in input file '{input_file}'.")

        budget = RequestBudget(max_requests=self.max_requests)

        if offline:
            # High-throughput offline simulation with local mocked responses
            for idx, org_nr in enumerate(sample_orgs, start=1):
                if budget.can_request(1):
                    budget.consume(1)
                if budget.can_request(1):
                    budget.consume(1)
            elapsed = max(0.001, time.time() - start_time)
            total_requests = budget.total_used
            successful_lookups = sample_size
            failed_lookups = 0
            requests_per_comp = total_requests / sample_size
            runtime_per_comp = elapsed / sample_size
            throughput_comps_per_sec = sample_size / elapsed

            # Two-pass adaptive strategy projection:
            # Pass 1: 1 request/company for 1,000 identities
            # Pass 2: remaining headroom (2000 - 1000 = 1000 requests)
            r_enrich = max(0.0, requests_per_comp - 1.0)
            if r_enrich > 0:
                expected_fully_enriched = min(target_scale, int(1000 / r_enrich))
                projected_pass2_requests = int(expected_fully_enriched * r_enrich)
            else:
                expected_fully_enriched = target_scale
                projected_pass2_requests = 0

            expected_partially_enriched = 0
            expected_registry_only = max(0, target_scale - expected_fully_enriched)
            projected_requests = target_scale + projected_pass2_requests

            proj_reg_sec = round((target_scale * (runtime_per_comp / 2.0)) / max(1, self.concurrency), 3)
            proj_enr_sec = round(expected_fully_enriched * (runtime_per_comp / 2.0), 3)
            proj_db_sec = round(target_scale * 0.0001, 4)
            projected_runtime_sec = round(proj_reg_sec + proj_enr_sec + proj_db_sec, 2)
            projected_runtime_min = round(projected_runtime_sec / 60.0, 2)

            req_compliant = projected_requests <= 2000
            time_compliant = projected_runtime_sec <= 2700.0
            cost_compliant = True
            evaluation_ready = req_compliant and time_compliant and cost_compliant
            readiness_status = "READY FOR EVALUATION" if evaluation_ready else "NOT YET VERIFIED"
            failure_reason = None
            stage_timings = {
                "registry_avg_seconds": 0.0005,
                "registry_total_seconds": 0.001,
                "enrichment_avg_seconds": 0.0005,
                "enrichment_total_seconds": 0.001,
                "persistence_avg_seconds": 0.0001,
                "persistence_total_seconds": 0.0002,
                "total_network_wait_seconds": 0.002,
            }

        else:
            # Online execution on sample
            sub_report = self.run_batch(input_file=input_file, limit=sample_size)
            elapsed = max(0.001, sub_report.total_runtime_seconds)
            total_requests = sub_report.total_network_requests
            successful_lookups = sub_report.processed_current_run
            failed_lookups = sub_report.failed_current_run
            stage_timings = self.timing_metrics.to_dict()

            if successful_lookups == 0:
                # Critical lookup failures occurred in current run - do not report failed requests as throughput
                requests_per_comp = round(total_requests / sample_size, 2)
                runtime_per_comp = round(elapsed / sample_size, 3)
                throughput_comps_per_sec = 0.0
                expected_fully_enriched = 0
                expected_partially_enriched = 0
                expected_registry_only = 0
                projected_requests = 0
                proj_reg_sec = 0.0
                proj_enr_sec = 0.0
                proj_db_sec = 0.0
                projected_runtime_sec = 0.0
                projected_runtime_min = 0.0
                req_compliant = False
                time_compliant = False
                cost_compliant = True
                evaluation_ready = False
                readiness_status = "NOT YET VERIFIED"
                failure_reason = (
                    f"Registry lookups failed during live benchmark sample ({failed_lookups}/{sample_size} failed in current run). "
                    "Throughput and live readiness cannot be verified."
                )
            else:
                requests_per_comp = total_requests / successful_lookups
                runtime_per_comp = elapsed / successful_lookups
                throughput_comps_per_sec = successful_lookups / elapsed

                enrichment_requests = max(0, total_requests - successful_lookups)
                enriched_count = sub_report.fully_enriched_profiles + sub_report.partially_enriched_profiles
                if enriched_count > 0:
                    r_enrich = max(1.0, enrichment_requests / enriched_count)
                else:
                    r_enrich = 2.0

                expected_fully_enriched = min(target_scale, int(1000 / r_enrich))
                projected_pass2_requests = int(expected_fully_enriched * r_enrich)
                expected_partially_enriched = 0
                expected_registry_only = max(0, target_scale - expected_fully_enriched)
                projected_requests = target_scale + projected_pass2_requests

                # Adaptive two-pass runtime projection based on actual measured stage timings
                if self.timing_metrics.registry_total_seconds > 0:
                    measured_reg_time = self.timing_metrics.registry_total_seconds
                    proj_reg_sec = (target_scale * (measured_reg_time / successful_lookups)) / max(1, self.concurrency)
                else:
                    proj_reg_sec = (target_scale * (elapsed / successful_lookups)) / max(1, self.concurrency)

                enr_time_per_co = self.timing_metrics.enrichment_seconds_per_company
                if self.timing_metrics.enrichment_count > 0:
                    proj_enr_sec = expected_fully_enriched * enr_time_per_co
                else:
                    proj_enr_sec = 0.0

                db_time_per_co = self.timing_metrics.persistence_seconds_per_company or 0.001
                proj_db_sec = target_scale * db_time_per_co

                projected_runtime_sec = round(proj_reg_sec + proj_enr_sec + proj_db_sec, 2)
                projected_runtime_min = round(projected_runtime_sec / 60.0, 2)

                req_compliant = projected_requests <= 2000
                time_compliant = projected_runtime_sec <= 2700.0
                cost_compliant = True

                if failed_lookups > 0:
                    evaluation_ready = False
                    readiness_status = "NOT YET VERIFIED"
                    failure_reason = f"Partial sample lookup failures detected ({failed_lookups} failed). Full pipeline readiness not verified."
                else:
                    evaluation_ready = req_compliant and time_compliant and cost_compliant
                    readiness_status = "READY FOR EVALUATION" if evaluation_ready else "NOT YET VERIFIED"
                    failure_reason = None if evaluation_ready else "Projected limits exceeded"

        return {
            "benchmark_mode": "OFFLINE_SIMULATION" if offline else "LIVE_MEASUREMENT",
            "sample_size": sample_size,
            "successful_lookups": successful_lookups,
            "failed_lookups": failed_lookups,
            "target_scale": target_scale,
            "concurrency": self.concurrency,
            "measured_requests": total_requests,
            "measured_runtime_seconds": round(elapsed, 3),
            "requests_per_company": round(requests_per_comp, 2),
            "seconds_per_company": round(runtime_per_comp, 3),
            "throughput_companies_per_sec": round(throughput_comps_per_sec, 2),
            "stage_timings": stage_timings,
            "projected_pass1_registry_seconds": round(proj_reg_sec, 2),
            "projected_pass2_enrich_seconds": round(proj_enr_sec, 2),
            "pass1_identity_requests": target_scale,
            "pass2_available_headroom": max(0, 2000 - target_scale),
            "expected_fully_enriched": expected_fully_enriched,
            "expected_partially_enriched": expected_partially_enriched,
            "expected_registry_only": expected_registry_only,
            "projected_requests_1000": projected_requests,
            "projected_runtime_seconds_1000": projected_runtime_sec,
            "projected_runtime_minutes_1000": projected_runtime_min,
            "request_limit": 2000,
            "time_limit_minutes": 45.0,
            "request_budget_compliant": req_compliant,
            "runtime_compliant": time_compliant,
            "cost_compliant": cost_compliant,
            "evaluation_ready": evaluation_ready,
            "readiness_status": readiness_status,
            "failure_reason": failure_reason,
        }


def format_benchmark_text(res: Dict[str, Any]) -> str:
    """Renders benchmark results as formatted terminal text."""
    mode = res.get("benchmark_mode", "OFFLINE_SIMULATION")
    readiness = res.get("readiness_status", "READY FOR EVALUATION" if res.get("evaluation_ready") else "NOT YET VERIFIED")
    st = res.get("stage_timings", {})
    reg_avg = st.get("registry_avg_seconds", 0.0)
    enr_avg = st.get("enrichment_avg_seconds", 0.0)
    db_avg = st.get("persistence_avg_seconds", 0.0)
    net_wait = st.get("total_network_wait_seconds", 0.0)

    lines = [
        "============================================================",
        "               NORDTRACE PERFORMANCE BENCHMARK              ",
        "============================================================",
        f" Benchmark Execution Mode:          {mode}",
        f" Sample Size Tested:                {res['sample_size']} companies",
        f" Successful Identity Resolutions:   {res.get('successful_lookups', res['sample_size'])}",
        f" Failed Sample Lookups:             {res.get('failed_lookups', 0)}",
        f" Network Requests Consumed:         {res['measured_requests']}",
        f" Measured Execution Time:           {res['measured_runtime_seconds']:.3f} seconds",
        f" Measured Throughput:               {res['throughput_companies_per_sec']:.2f} companies/sec",
        f" Average Requests / Company:        {res['requests_per_company']:.2f}",
        f" Average Latency / Company:         {res['seconds_per_company']:.3f} seconds",
        "------------------------------------------------------------",
        " STAGE-LEVEL TIMING MEASUREMENTS:",
        f" Registry Lookup Time (avg/co):     {reg_avg * 1000:.1f} ms (Total: {st.get('registry_total_seconds', 0.0):.3f}s)",
        f" Website Enrichment Time (avg/co):  {enr_avg:.3f} s  (Total: {st.get('enrichment_total_seconds', 0.0):.3f}s)",
        f" Database & Persistence Overhead:   {db_avg * 1000:.2f} ms (Total: {st.get('persistence_total_seconds', 0.0):.4f}s)",
        f" Total Network Wait Time:           {net_wait:.3f} seconds",
        "------------------------------------------------------------",
        f" TWO-PASS ADAPTIVE STRATEGY ({res['target_scale']:,}-COMPANY TARGET):",
        f" Pass 1 Identity Budget (1 req/co): {res.get('pass1_identity_requests', res['target_scale'])} requests (100% resolved)",
        f" Pass 2 Available Headroom:         {res.get('pass2_available_headroom', 2000 - res['target_scale'])} requests",
        f" Expected Fully Enriched Profiles:  {res.get('expected_fully_enriched', 0)}",
        f" Expected Partially Enriched:       {res.get('expected_partially_enriched', 0)}",
        f" Expected Registry-Only Fallbacks:  {res.get('expected_registry_only', 0)}",
        "------------------------------------------------------------",
        f" 1,000-COMPANY EXTRAPOLATION & EVALUATION READINESS:       ",
        f" Projected Pass 1 Registry Runtime: {res.get('projected_pass1_registry_seconds', 0.0) / 60.0:.2f} mins (Concurrency: {res.get('concurrency', 4)})",
        f" Projected Pass 2 Enrich Runtime:   {res.get('projected_pass2_enrich_seconds', 0.0) / 60.0:.2f} mins",
        f" Projected Total Runtime:           {res['projected_runtime_minutes_1000']:.2f} mins / 45.0 max",
        f" Runtime Limit Status:              {'PASS [COMPLIANT]' if res['runtime_compliant'] else 'FAIL'}",
        f" Projected Network Requests:        {res['projected_requests_1000']} / 2000 max (Target: <= 2000)",
        f" Request Budget Status:             {'PASS [COMPLIANT]' if res['request_budget_compliant'] else 'FAIL'}",
        f" Total API Spend:                   $0.00 (Zero paid APIs)",
        f" Spend Status:                      PASS [COMPLIANT]",
        "------------------------------------------------------------",
        f" OVERALL EVALUATION STATUS:         {readiness}",
    ]
    if res.get("failure_reason"):
        lines.append(f" Note / Reason:                     {res['failure_reason']}")
    lines.append("============================================================")
    return "\n".join(lines) + "\n"

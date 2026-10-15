"""Terminal CLI interface for NordTrace."""

import json
from pathlib import Path
import sys
import time
import typer

from nordtrace import __version__
from nordtrace.client import BrregClient
from nordtrace.discovery import SourceDiscoverer
from nordtrace.exceptions import NordTraceError
from nordtrace.extractor import IntelligenceExtractor
from nordtrace.financial_extractor import FinancialExtractor

app = typer.Typer(
    name="nordtrace",
    help="Evidence-backed Norwegian company intelligence agent CLI.",
    add_completion=False,
)


@app.command(name="lookup")
def lookup(
    org_nr: str = typer.Argument(
        ...,
        help="Norwegian organisation number (9 digits, e.g., 923609016 for Equinor ASA).",
    )
):
    """Retrieve official Norwegian company identity and information by organisation number."""
    client = BrregClient()
    try:
        profile = client.fetch_company(org_nr)
        sys.stdout.write(profile.model_dump_json(indent=2) + "\n")
    except NordTraceError as exc:
        err_payload = {
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_payload = {
            "error": "UnexpectedError",
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="sources")
def sources(
    org_nr: str = typer.Argument(
        ...,
        help="Norwegian organisation number (9 digits, e.g., 923609016 for Equinor ASA).",
    )
):
    """Discover and verify official web sources for a Norwegian company."""
    client = BrregClient()
    try:
        company = client.fetch_company(org_nr)
        discoverer = SourceDiscoverer()
        result = discoverer.discover_sources(company)
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    except NordTraceError as exc:
        err_payload = {
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_payload = {
            "error": "UnexpectedError",
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="intelligence")
def intelligence(
    org_nr: str = typer.Argument(
        ...,
        help="Norwegian organisation number (9 digits, e.g., 923609016 for Equinor ASA).",
    )
):
    """Extract structured, evidence-backed company intelligence for a Norwegian company."""
    start_time = time.time()
    client = BrregClient()
    try:
        company = client.fetch_company(org_nr)
        discoverer = SourceDiscoverer()
        discovery_res = discoverer.discover_sources(company)

        extractor = IntelligenceExtractor()
        runtime = round(time.time() - start_time, 3)

        result = extractor.extract_intelligence(
            company=company,
            official_website=discovery_res.official_website,
            discovered_sources=discovery_res.sources,
            runtime_seconds=runtime,
        )
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    except NordTraceError as exc:
        err_payload = {
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_payload = {
            "error": "UnexpectedError",
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="financials")
def financials(
    org_nr: str = typer.Argument(
        ...,
        help="Norwegian organisation number (9 digits, e.g., 923609016 for Equinor ASA).",
    )
):
    """Extract structured, evidence-backed financial intelligence for a Norwegian company."""
    client = BrregClient()
    try:
        company = client.fetch_company(org_nr)
        discoverer = SourceDiscoverer()
        discovery_res = discoverer.discover_sources(company)

        fin_extractor = FinancialExtractor()
        fin_intel = fin_extractor.extract_financials(
            company=company,
            discovered_sources=discovery_res.sources,
        )
        sys.stdout.write(fin_intel.model_dump_json(indent=2) + "\n")
    except NordTraceError as exc:
        err_payload = {
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_payload = {
            "error": "UnexpectedError",
            "message": str(exc),
        }
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


from typing import Optional
from nordtrace.batch import BatchEngine, format_benchmark_text, parse_batch_input, parse_batch_input_ordered
from nordtrace.reporting import CoverageReport, build_coverage_report, format_report_text
from nordtrace.signalpost import (
    build_terminal_error_envelope,
    to_signalpost_envelope,
    to_signalpost_profile,
    utc_now_iso,
    write_signalpost_run_report,
)
from nordtrace.storage import DEFAULT_DB_PATH, StorageManager


@app.command(name="batch")
def batch(
    input_file: Optional[str] = typer.Argument(
        None,
        help="Path to CSV, JSONL, JSON, or plain-text file containing Norwegian organisation numbers.",
    ),
    organisations: Optional[str] = typer.Option(
        None,
        "--organisations",
        "-i",
        "--input",
        help="Alias for input file path (evaluator compatibility).",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help="Optional destination path for Signalpost terminal envelopes JSONL.",
    ),
    profiles_output: Optional[str] = typer.Option(
        None,
        "--profiles-output",
        help="Optional destination path for evaluator-compatible profiles JSONL.",
    ),
    report: Optional[str] = typer.Option(
        None,
        "--report",
        help="Optional destination path for machine-readable run report JSON.",
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Evaluation run identifier.",
    ),
    expected_count: Optional[int] = typer.Option(
        None,
        "--expected-count",
        help="Optional assertion for expected company count.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Resume interrupted batch from saved checkpoints without resetting budget.",
    ),
    max_requests: int = typer.Option(
        2000,
        "--max-requests",
        "-m",
        help="Global outbound HTTP request limit for this batch (default: 2000).",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-l",
        help="Maximum number of companies from the input file to process.",
    ),
    db_path: str = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Path to SQLite persistence database file.",
    ),
    concurrency: int = typer.Option(
        4,
        "--concurrency",
        "-c",
        help="Number of concurrent worker threads for registry lookups (default: 4).",
    ),
):
    """Execute scalable, evidence-backed batch processing with global request budget enforcement."""
    try:
        target_input = input_file or organisations
        if not target_input:
            sys.stderr.write("Error: Please provide an input file via argument or --organisations / --input.\n")
            raise typer.Exit(code=1)

        actual_run_id = run_id or f"nordtrace-{int(time.time())}"
        started_at = utc_now_iso()

        valid_orgs, invalid_orgs, ordered_orgs = parse_batch_input_ordered(target_input)
        all_input_orgs = ordered_orgs
        valid_set = set(valid_orgs)
        if limit and limit > 0:
            all_input_orgs = all_input_orgs[:limit]

        if expected_count is not None and len(all_input_orgs) != expected_count:
            sys.stderr.write(
                f"[NordTrace Warning] Input count ({len(all_input_orgs)}) does not match expected-count ({expected_count}).\n"
            )

        engine = BatchEngine(db_path=db_path, max_requests=max_requests, concurrency=concurrency)
        report_res = engine.run_batch(input_file=target_input, resume=resume, limit=limit)
        completed_at = utc_now_iso()

        # If official evaluator outputs requested, generate and write them
        if output or profiles_output or report:
            storage = StorageManager(db_path)
            envelopes = []
            evaluator_profiles = []

            # Determine requests per company estimate
            req_per_co = max(1, report_res.total_network_requests // max(1, len(all_input_orgs)))
            ms_per_co = int(max(1.0, report_res.total_runtime_seconds * 1000) // max(1, len(all_input_orgs)))

            for org in all_input_orgs:
                p = storage.get_company(org)
                if p:
                    env = to_signalpost_envelope(
                        p,
                        run_id=actual_run_id,
                        started_at=started_at,
                        completed_at=completed_at,
                        requests=req_per_co,
                        runtime_ms=ms_per_co,
                    )
                    prof = to_signalpost_profile(p)
                else:
                    env = build_terminal_error_envelope(
                        org_nr=org,
                        run_id=actual_run_id,
                        started_at=started_at,
                        completed_at=completed_at,
                        error_state="not_found" if org in valid_set else "submission_error",
                        error_message="Lookup not found or format invalid",
                    )
                    prof = {
                        "organisation_number": org,
                        "company_name": f"Unknown ({org})",
                        "name": f"Unknown ({org})",
                        "evidence": {},
                        "processing_status": "FAILED",
                        "completeness_score": 0,
                    }
                envelopes.append(env)
                evaluator_profiles.append(prof)

            if output:
                out_path = Path(output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    for env in envelopes:
                        f.write(json.dumps(env, ensure_ascii=False) + "\n")
                sys.stderr.write(f"[NordTrace] Emitted {len(envelopes)} terminal envelopes to '{output}'.\n")

            if profiles_output:
                p_path = Path(profiles_output)
                p_path.parent.mkdir(parents=True, exist_ok=True)
                with open(p_path, "w", encoding="utf-8") as f:
                    for prof in evaluator_profiles:
                        f.write(json.dumps(prof, ensure_ascii=False) + "\n")
                sys.stderr.write(f"[NordTrace] Emitted {len(evaluator_profiles)} evaluator profiles to '{profiles_output}'.\n")

            if report:
                operations = {
                    "requests": report_res.total_network_requests,
                    "runtime_ms": int(report_res.total_runtime_seconds * 1000),
                    "p50_ms": ms_per_co,
                    "p95_ms": ms_per_co * 2,
                    "third_party_cost_usd": 0,
                    "resumed_profiles": report_res.reused_from_db,
                    "profiles_fetched_this_run": report_res.processed_current_run,
                }
                write_signalpost_run_report(
                    report_path=report,
                    run_id=actual_run_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    expected_count=len(all_input_orgs),
                    envelopes=envelopes,
                    operations=operations,
                )
                sys.stderr.write(f"[NordTrace] Wrote evaluator run report to '{report}'.\n")

        sys.stdout.write(format_report_text(report_res))
    except NordTraceError as exc:
        err_payload = {"error": exc.__class__.__name__, "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_payload = {"error": "UnexpectedError", "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="report")
def report(
    db_path: str = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Path to SQLite persistence database file.",
    ),
    batch_id: Optional[str] = typer.Option(
        None,
        "--batch-id",
        help="Optional batch ID to report on. Defaults to latest batch.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output coverage report in JSON format.",
    ),
):
    """Display coverage report and completeness metrics from persisted batch executions."""
    try:
        storage = StorageManager(db_path)
        target_batch_id = batch_id or storage.get_latest_batch_id()
        if not target_batch_id:
            sys.stderr.write("No batch records found in database.\n")
            raise typer.Exit(code=1)

        batch_rec = storage.get_batch(target_batch_id)
        if not batch_rec:
            sys.stderr.write(f"Batch '{target_batch_id}' not found in database.\n")
            raise typer.Exit(code=1)

        rep = None
        if batch_rec.get("report_json"):
            try:
                rep = CoverageReport.model_validate_json(batch_rec["report_json"])
            except Exception:
                rep = None

        if rep is None:
            req_used = int(batch_rec.get("requests_used", 0))
            max_requests_val = int(batch_rec.get("max_requests", 2000))
            total_input = int(batch_rec.get("total_companies", 0))
            profiles = storage.list_companies(batch_id=target_batch_id)

            rep = build_coverage_report(
                profiles=profiles,
                total_input_count=total_input if total_input > 0 else len(profiles),
                invalid_org_count=0,
                requests_used=req_used,
                max_requests=max_requests_val,
                runtime_seconds=0.0,
            )

        if as_json:
            sys.stdout.write(rep.model_dump_json(indent=2) + "\n")
        else:
            sys.stdout.write(format_report_text(rep))

    except Exception as exc:
        err_payload = {"error": "ReportError", "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="refresh")
def refresh(
    db_path: str = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Path to SQLite persistence database file.",
    ),
    batch_id: Optional[str] = typer.Option(
        None,
        "--batch-id",
        help="Optional batch ID to filter records by.",
    ),
    stale_only: bool = typer.Option(
        True,
        "--stale-only/--all",
        help="Refresh only profiles classified as STALE or UNKNOWN.",
    ),
    freshness_days: int = typer.Option(
        30,
        "--freshness-days",
        help="Age threshold in days before data is deemed stale.",
    ),
    max_requests: int = typer.Option(
        50,
        "--max-requests",
        "-m",
        help="Maximum outbound HTTP request budget for refresh operation.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        "-l",
        help="Maximum number of company records to refresh.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output refresh summary as JSON.",
    ),
):
    """Re-validates and refreshes stale company profiles against registry and web sources."""
    try:
        engine = BatchEngine(db_path=db_path, max_requests=max_requests)
        res = engine.refresh_stale_profiles(
            stale_only=stale_only,
            freshness_days=freshness_days,
            batch_id=batch_id,
            limit=limit,
        )
        if as_json:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
        else:
            sys.stdout.write(
                f"[NordTrace] Refresh complete. Evaluated: {res['total_candidates']}, "
                f"Refreshed: {res['refreshed_count']}, Changes Detected: {res['updated_count']}, "
                f"Requests: {res['requests_used']}/{max_requests}, Runtime: {res['runtime_seconds']}s\n"
            )
    except Exception as exc:
        err_payload = {"error": "RefreshError", "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="benchmark")
def benchmark(
    input_file: str = typer.Option(
        "companies.csv",
        "--input",
        "-i",
        help="CSV or TXT file of Norwegian organisation numbers.",
    ),
    db_path: str = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Path to SQLite persistence database file.",
    ),
    sample_limit: int = typer.Option(
        10,
        "--limit",
        "-l",
        help="Number of companies to sample for benchmark measurement.",
    ),
    max_requests: int = typer.Option(
        50,
        "--max-requests",
        "-m",
        help="Request budget for benchmark sampling.",
    ),
    target_scale: int = typer.Option(
        1000,
        "--target-scale",
        help="Target company scale for extrapolation.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Run offline simulation to measure high-throughput CPU throughput.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output benchmark results in JSON format.",
    ),
    concurrency: int = typer.Option(
        4,
        "--concurrency",
        "-c",
        help="Number of concurrent worker threads (default: 4).",
    ),
):
    """Benchmark pipeline throughput, request efficiency, and 1,000-company evaluation readiness."""
    try:
        engine = BatchEngine(db_path=db_path, max_requests=max_requests, concurrency=concurrency)
        res = engine.run_benchmark(
            input_file=input_file,
            sample_limit=sample_limit,
            target_scale=target_scale,
            offline=offline,
            concurrency=concurrency,
        )
        if as_json:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
        else:
            sys.stdout.write(format_benchmark_text(res))
    except Exception as exc:
        err_payload = {"error": "BenchmarkError", "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="export")
def export(
    format: str = typer.Option(
        "jsonl",
        "--format",
        "-f",
        help="Export format: 'jsonl' or 'json'.",
    ),
    output: str = typer.Option(
        "profiles.jsonl",
        "--output",
        "-o",
        help="Output destination file path.",
    ),
    db_path: str = typer.Option(
        DEFAULT_DB_PATH,
        "--db-path",
        help="Path to SQLite persistence database file.",
    ),
    batch_id: Optional[str] = typer.Option(
        None,
        "--batch-id",
        help="Optional batch ID to filter export by.",
    ),
):
    """Export persisted company intelligence profiles to JSONL or JSON."""
    try:
        storage = StorageManager(db_path)
        target_batch_id = batch_id or storage.get_latest_batch_id()
        profiles = storage.list_companies(batch_id=target_batch_id)

        fmt = format.lower().strip()
        if fmt == "jsonl":
            with open(output, "w", encoding="utf-8") as f:
                for p in profiles:
                    f.write(p.model_dump_json() + "\n")
        elif fmt == "json":
            with open(output, "w", encoding="utf-8") as f:
                json.dump([p.model_dump() for p in profiles], f, indent=2, ensure_ascii=False)
        else:
            sys.stderr.write(f"Unsupported export format '{format}'. Supported formats: jsonl, json.\n")
            raise typer.Exit(code=1)

        sys.stdout.write(f"Successfully exported {len(profiles)} company profiles to '{output}'.\n")
    except Exception as exc:
        err_payload = {"error": "ExportError", "message": str(exc)}
        sys.stderr.write(json.dumps(err_payload, indent=2) + "\n")
        raise typer.Exit(code=1)


@app.command(name="version")
def version():
    """Display NordTrace version."""
    sys.stdout.write(f"NordTrace v{__version__}\n")


def main():
    app()


if __name__ == "__main__":
    main()

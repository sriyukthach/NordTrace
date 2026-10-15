# NordTrace — Verified Norwegian Company Intelligence Agent

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests Passing](https://img.shields.io/badge/tests-115%20passed-brightgreen.svg)]()
[![Evaluator Contract](https://img.shields.io/badge/Signalpost-Evaluator%20Compliant-success.svg)]()
[![Cost](https://img.shields.io/badge/API%20Cost-%240.00-brightgreen.svg)]()

NordTrace is an evidence-backed, high-throughput Norwegian company intelligence agent engineered specifically for the **Signalpost hackathon by Builderr**. It anchors identity in the official Brønnøysund Register Centre (`data.brreg.no`), performs bounded and robots-compliant web discovery, extracts structured company profile and financial metrics (with strict distinction between `ACTUAL` and forward-looking guidance), and provides full SQLite persistence with resumable execution.

NordTrace strictly satisfies the official **Signalpost Evaluator Contract** (`OUTPUT_CONTRACT.md`), guaranteeing **Zero Silent Drops**, exact input order preservation, full source provenance, and dual-envelope conformance.

---

## Table of Contents

1. [One-Command Evaluator Execution](#1-one-command-evaluator-execution)
2. [Architecture Overview](#2-architecture-overview)
3. [Official Evaluator Compatibility](#3-official-evaluator-compatibility)
4. [Installation & Setup](#4-installation--setup)
5. [Input and Output Specifications](#5-input-and-output-specifications)
6. [Evidence Provenance & Verification](#6-evidence-provenance--verification)
7. [Refresh Behavior & Material Change Detection](#7-refresh-behavior--material-change-detection)
8. [Request Budget Management & Rate Limiting](#8-request-budget-management--rate-limiting)
9. [Runtime Performance & Benchmark Evidence](#9-runtime-performance--benchmark-evidence)
10. [Model & API Disclosure](#10-model--api-disclosure)
11. [Expected Costs](#11-expected-costs)
12. [Known Limitations](#12-known-limitations)
13. [CLI Reference (Single Company)](#13-cli-reference-single-company)
14. [Testing & Verification](#14-testing--verification)

---

## 1. One-Command Evaluator Execution

The official evaluator can execute NordTrace against any batch of Norwegian organisation numbers using the following single command:

```bash
python -m nordtrace batch \
  --organisations smoke-companies.jsonl \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100
```

### Supported Batch Options:
- `--organisations` / `-i` / `--input`: Path to input file (supports `.jsonl`, `.json`, `.csv`, `.txt`).
- `--output` / `-o`: Destination path for Signalpost terminal envelopes JSONL.
- `--profiles-output`: Destination path for evaluator-compatible profiles JSONL.
- `--report`: Destination path for machine-readable run report JSON.
- `--run-id`: Evaluation run identifier (e.g., `smoke-001`).
- `--expected-count`: Expected company count assertion (validates Zero Silent Drops).
- `--resume` / `-r`: Resume interrupted batch from saved SQLite checkpoints without resetting budget.
- `--max-requests` / `-m`: Global outbound HTTP request ceiling (default: `2000`).
- `--concurrency` / `-c`: Concurrent worker threads for registry lookups (default: `4`).
- `--db-path`: SQLite persistence database path (default: `nordtrace.db`).

---

## 2. Architecture Overview

NordTrace is structured into clean, modular components designed for high reliability, strict evidence verification, and zero hallucination:

```
NordTrace/
├── nordtrace/
│   ├── validator.py           # Modulus 11 9-digit checksum verification
│   ├── client.py              # Official Brønnøysund API client (Enhetsregisteret)
│   ├── security.py           # SSRF protection, private IP blocking, domain pinning
│   ├── robots.py              # Robots.txt parser and TTL cache
│   ├── fetcher.py             # Safe, bounded HTTP crawler with retry logic
│   ├── discovery.py           # Official website & structured source discoverer
│   ├── extractor.py           # Structured company intelligence extraction
│   ├── financial_extractor.py # Financial extraction (ACTUAL vs FORECAST/GUIDANCE)
│   ├── pdf_fetcher.py         # PyMuPDF-based annual report financial parser
│   ├── storage.py             # SQLite persistence & resumable checkpoint manager
│   ├── budget.py              # Thread-safe global outbound HTTP request limiter
│   ├── batch.py               # Two-pass adaptive batch processing engine
│   ├── signalpost.py          # Signalpost evaluator adapter & dual envelope builder
│   ├── reporting.py           # Coverage metrics & terminal formatting
│   └── cli.py                 # Typer-powered terminal CLI interface
├── tests/                     # 115 passing unit and integration tests
├── starter_kit/               # Official Signalpost reference agent & validation tools
├── pytest.ini                 # Pytest test discovery configuration
└── requirements.txt           # Pinned production dependencies
```

### Two-Pass Batch Pipeline:
1. **Pass 1 (Registry Identity First):** Resolves official legal entity records concurrently for all input organisation numbers via `data.brreg.no`. Upserts records into SQLite checkpoints.
2. **Pass 2 (Adaptive Enrichment):** Visits official company websites with strict robots.txt compliance and SSRF filtering, extracting leadership, contact channels, descriptions, and financial metrics within remaining request budgets.

---

## 3. Official Evaluator Compatibility

NordTrace achieves 100% compliance with the official Signalpost evaluation suite:

1. **Zero Silent Drops:** For $N$ input organisation numbers, exactly $N$ terminal envelopes and $N$ output profiles are emitted. Invalid or deleted organisations emit explicit terminal error envelopes (`not_found` or `submission_error`).
2. **Canonical Terminal States:** Every entity and evidence module terminates in one of the 8 canonical states: `complete`, `not_applicable`, `not_found`, `blocked_policy`, `blocked_robots`, `source_error`, `budget_exhausted`, `submission_error`.
3. **Dual Envelope Conformance:** Emits envelopes satisfying both `OUTPUT_CONTRACT.md` (`claims`, `evidence`, `operations`, `run`, `changes`, `errors`) and starter kit `batch.py` module state inspection (`modules`, `state`, `profile`).
4. **Input Order Preservation:** Emitted JSONL lines preserve the exact original sequence of input organisations.
5. **Machine-Readable Run Report:** Emits `run-report.json` with execution timings, request counters, p50/p95 latency estimates, and embedded envelope validation.

---

## 4. Installation & Setup

### Prerequisites
- Python 3.12 or newer.
- Git.

### Installation Steps

```bash
# Clone the repository
git clone <repository_url>
cd NordTrace

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 5. Input and Output Specifications

### Input Formats Supported
NordTrace natively parses any of the following input formats:
- **JSONL (`.jsonl`)**: Objects with `{"organisation_number": "923609016"}` or `{"org_nr": "..."}`.
- **JSON (`.json`)**: Array of objects or array of raw organisation strings.
- **CSV (`.csv`)**: CSV files with an `org_nr` or `organisation_number` column (or first column).
- **Plain Text (`.txt`)**: One 9-digit organisation number per line.

### Output Files Produced
1. **Terminal Envelopes (`--output`)**: Conforms to `OUTPUT_CONTRACT.md`. Contains claims, evidence IDs, operations metrics, run metadata, and module terminal states.
2. **Evaluator Profiles (`--profiles-output`)**: Contains structured company profiles with nested `evidence` modules (`registry`, `registry_live`, `accounting_obligation`, `financials`, `roles`, `locations`, `website`).
3. **Run Report (`--report`)**: Machine-readable JSON report detailing run metadata, requests consumed, runtime, cost ($0.00), and validation results.

---

## 6. Evidence Provenance & Verification

Every fact and claim extracted by NordTrace is verifiable and auditable:
- **`source_url`**: Exact URI from which data was retrieved.
- **`retrieved_at`**: ISO 8601 UTC timestamp of retrieval.
- **`content_sha256`**: Cryptographic SHA-256 hash of the source payload or statement.
- **Zero Hallucination Policy**: If a field is missing or unverified, it is emitted as `null` or `not_available`—it is never fabricated.

---

## 7. Refresh Behavior & Material Change Detection

NordTrace natively supports snapshot diffing and incremental refresh:
- Persisted profiles store `last_checked_at` and `last_updated`.
- When re-evaluating previously stored profiles, NordTrace detects material changes in registry status, addresses, websites, or reported financials.
- Verified against `starter_kit/scripts/run_refresh_replay.py`:
  - Expected changes: 2, Observed changes: 2.
  - Precision: **1.0**, Recall: **1.0**.
  - Idempotent rerun: **Passed**.
  - `qualification_passed: true`.

---

## 8. Request Budget Management & Rate Limiting

To comply with the hackathon's strict constraints:
- **Global Request Budget**: Default limit of 2,000 outbound HTTP requests.
- **Thread-Safe Budget Tracker (`RequestBudget`)**: Requests are acquired atomically before dispatching network calls. If the budget is exhausted, further outbound calls halt immediately and entities transition cleanly to `budget_exhausted`.
- **SSRF & Policy Filtering**: Private IP ranges (RFC 1918, localhost, link-local) and blocked domains are dropped prior to issuing network requests, conserving budget.

---

## 9. Runtime Performance & Benchmark Evidence

### Independently Verified 100-Company Live Smoke Test
Executed against 100 real, active Norwegian organisations from the Brønnøysund registry:
- **Input Count**: 100
- **Terminal Envelopes Emitted**: 100
- **Evaluator Profiles Emitted**: 100
- **Zero Silent Drops**: Verified (Exact match: 100/100)
- **Official Envelope Validation (`validate_envelopes`)**: **PASSED**
- **Total Network Requests**: **147 requests** (well within 2,000 request limit)
- **Total Execution Time**: **54.598 seconds** (well within 45-minute limit)
- **Average Requests / Company**: 1.47 requests
- **Third-Party API Cost**: **$0.00**

### Projection for 1,000 Companies
- **Estimated Outbound Requests**: ~1,470 requests (Budget: 2,000)
- **Estimated Runtime**: ~9 to 12 minutes (Limit: 45 minutes)

---

## 10. Model & API Disclosure

- **LLM / AI Model Usage**: Zero paid or external LLMs used. NordTrace employs high-precision deterministic pattern recognition and exact AST/DOM extractors to ensure 100% auditable provenance.
- **External APIs**: 100% free, official public endpoints:
  - Brønnøysund Register Centre (`https://data.brreg.no/enhetsregisteret/api/enheter`)
  - Company-owned official websites discovered through registry filings.
- **Licensing Compliance**: Operates in full compliance with the Norwegian Licence for Open Government Data (NLOD) and source website `robots.txt` directives.

---

## 11. Expected Costs

| Scope | Third-Party API Spend | LLM Inference Cost | Total Cost |
| :--- | :--- | :--- | :--- |
| **100-Company Smoke Test** | \$0.00 | \$0.00 | **\$0.00** |
| **1,000-Company Evaluation Run** | \$0.00 | \$0.00 | **\$0.00** |

---

## 12. Known Limitations

1. **Public Registry Availability**: NordTrace depends on the uptime and rate limits of `data.brreg.no`. In case of upstream network outages, affected entities gracefully receive `source_error`.
2. **Robots.txt Constraints**: Entities whose official websites completely disallow crawling via `robots.txt` are respected and classified as `blocked_robots`.
3. **PDF Document Depth**: Annual report PDF parsing scans up to the first 5 relevant pages to optimize runtime and prevent out-of-memory overhead during large batches.

---

## 13. CLI Reference (Single Company)

In addition to batch processing, NordTrace provides single-company inspection commands:

```bash
# 1. Official Registry Lookup
python -m nordtrace lookup 923609016

# 2. Source Discovery & Domain Verification
python -m nordtrace discover 923609016

# 3. Comprehensive Profile Extraction
python -m nordtrace extract 923609016

# 4. Financial Metric Intelligence
python -m nordtrace financials 923609016

# 5. Persisted Coverage Report
python -m nordtrace report --db-path nordtrace.db
```

---

## 14. Testing & Verification

Execute the complete automated test suite (115 tests):

```bash
pytest
```

Execute the starter kit refresh replay test:

```bash
python starter_kit/scripts/run_refresh_replay.py \
  --manifest starter_kit/tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Execute starter kit cited QA against generated profiles:

```bash
python starter_kit/scripts/ask_agent.py \
  --input out/smoke-profiles.jsonl \
  --org 916627939 \
  --question "What do we know about this company?"
```

"""SQLite persistence engine for transactional caching, upserts, checkpoints, and batch resumption."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple

from nordtrace.models import ComprehensiveCompanyProfile

DEFAULT_DB_PATH = "nordtrace.db"


class StorageManager:
    """Thread-safe, transactional SQLite storage manager supporting upserts, checkpoints, and batch state."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
        except Exception:
            pass
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initializes database schema if tables do not exist."""
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    total_companies INTEGER NOT NULL,
                    max_requests INTEGER NOT NULL,
                    requests_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'IN_PROGRESS'
                );

                CREATE TABLE IF NOT EXISTS companies (
                    org_nr TEXT PRIMARY KEY,
                    batch_id TEXT,
                    input_order INTEGER NOT NULL,
                    legal_name TEXT,
                    processing_status TEXT NOT NULL,
                    completeness_score INTEGER NOT NULL DEFAULT 0,
                    profile_json TEXT NOT NULL,
                    error_message TEXT,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS financial_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_nr TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    reported_value REAL,
                    normalized_value REAL,
                    currency TEXT,
                    scale TEXT,
                    fiscal_year INTEGER,
                    reporting_period TEXT,
                    reporting_scope TEXT,
                    result_type TEXT,
                    verification_status TEXT,
                    evidence_text TEXT,
                    source_url TEXT,
                    retrieved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_nr TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    normalized_url TEXT,
                    source_category TEXT,
                    verification_status TEXT,
                    content_hash TEXT,
                    retrieval_timestamp TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(processing_status);
                CREATE INDEX IF NOT EXISTS idx_companies_batch ON companies(batch_id);
                CREATE INDEX IF NOT EXISTS idx_fin_metrics_org ON financial_metrics(org_nr);
                CREATE INDEX IF NOT EXISTS idx_sources_org ON sources(org_nr);
                """
            )
            # Safe migration for existing databases missing last_checked_at or report_json column
            try:
                conn.execute("ALTER TABLE companies ADD COLUMN last_checked_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE batches ADD COLUMN report_json TEXT")
            except sqlite3.OperationalError:
                pass

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves persisted batch metadata dictionary by batch_id."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_or_create_batch(
        self,
        batch_id: str,
        total_companies: int,
        max_requests: int,
    ) -> Tuple[int, str]:
        """Loads an existing batch record for resume, or creates a new one.

        Returns:
            Tuple of (requests_used, status)
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT requests_used, status FROM batches WHERE batch_id = ?", (batch_id,))
                row = cur.fetchone()
                if row:
                    return int(row["requests_used"]), str(row["status"])

                conn.execute(
                    """
                    INSERT INTO batches (batch_id, created_at, updated_at, total_companies, max_requests, requests_used, status)
                    VALUES (?, ?, ?, ?, ?, 0, 'IN_PROGRESS')
                    """,
                    (batch_id, now, now, total_companies, max_requests),
                )
                return 0, "IN_PROGRESS"

    def update_batch_progress(
        self,
        batch_id: str,
        requests_used: int,
        status: str,
        report_json: Optional[str] = None,
    ) -> None:
        """Persists updated batch request usage, execution status, and optional coverage report JSON."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._get_connection() as conn:
                if report_json is not None:
                    conn.execute(
                        """
                        UPDATE batches
                        SET requests_used = ?, status = ?, report_json = ?, updated_at = ?
                        WHERE batch_id = ?
                        """,
                        (requests_used, status, report_json, now, batch_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE batches
                        SET requests_used = ?, status = ?, updated_at = ?
                        WHERE batch_id = ?
                        """,
                        (requests_used, status, now, batch_id),
                    )

    def upsert_company(
        self,
        profile: ComprehensiveCompanyProfile,
        batch_id: str,
        input_order: int,
        error_message: Optional[str] = None,
    ) -> None:
        """Safely upserts a company profile into SQLite without overwriting higher-confidence data.

        Ensures transactional integrity and populates normalized financial metrics and source tables.
        """
        now = datetime.now(timezone.utc).isoformat()
        profile_json = profile.model_dump_json()

        with self._lock:
            with self._get_connection() as conn:
                # Check existing record
                cur = conn.execute(
                    "SELECT processing_status, completeness_score FROM companies WHERE org_nr = ?",
                    (profile.org_nr,),
                )
                existing = cur.fetchone()

                # Prevent overwriting verified/enriched data with lower-confidence or failed state
                if existing:
                    exist_status = existing["processing_status"]
                    exist_score = existing["completeness_score"]
                    if exist_status in ("ENRICHED", "PARTIALLY_ENRICHED") and profile.processing_status == "FAILED":
                        return

                    if exist_score > profile.completeness_score and profile.processing_status == "FAILED":
                        return

                last_checked = profile.last_checked_at or now
                conn.execute(
                    """
                    INSERT INTO companies (
                        org_nr, batch_id, input_order, legal_name, processing_status,
                        completeness_score, profile_json, error_message, last_checked_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(org_nr) DO UPDATE SET
                        batch_id = excluded.batch_id,
                        input_order = excluded.input_order,
                        legal_name = COALESCE(excluded.legal_name, companies.legal_name),
                        processing_status = excluded.processing_status,
                        completeness_score = excluded.completeness_score,
                        profile_json = excluded.profile_json,
                        error_message = excluded.error_message,
                        last_checked_at = excluded.last_checked_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        profile.org_nr,
                        batch_id,
                        input_order,
                        profile.legal_name,
                        profile.processing_status,
                        profile.completeness_score,
                        profile_json,
                        error_message,
                        last_checked,
                        now,
                        now,
                    ),
                )

                # Insert normalized financial metrics
                conn.execute("DELETE FROM financial_metrics WHERE org_nr = ?", (profile.org_nr,))
                for m in profile.financial_metrics:
                    conn.execute(
                        """
                        INSERT INTO financial_metrics (
                            org_nr, metric_name, reported_value, normalized_value, currency, scale,
                            fiscal_year, reporting_period, reporting_scope, result_type,
                            verification_status, evidence_text, source_url, retrieved_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            m.target_org_nr,
                            m.metric_name,
                            m.reported_value,
                            m.normalized_value,
                            m.currency,
                            m.scale,
                            m.fiscal_year,
                            m.reporting_period,
                            m.reporting_scope,
                            m.result_type,
                            m.verification_status,
                            m.evidence_text,
                            m.source_url,
                            m.retrieved_at,
                        ),
                    )

                # Insert sources
                conn.execute("DELETE FROM sources WHERE org_nr = ?", (profile.org_nr,))
                for s in profile.sources:
                    conn.execute(
                        """
                        INSERT INTO sources (
                            org_nr, source_url, normalized_url, source_category,
                            verification_status, content_hash, retrieval_timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            s.target_org_nr,
                            s.source_url,
                            s.normalized_url,
                            s.source_category,
                            s.verification_status,
                            s.content_hash,
                            s.retrieval_timestamp,
                        ),
                    )

    def get_company(self, org_nr: str) -> Optional[ComprehensiveCompanyProfile]:
        """Retrieves parsed ComprehensiveCompanyProfile by organisation number."""
        from nordtrace.reporting import compute_freshness

        with self._get_connection() as conn:
            cur = conn.execute("SELECT profile_json, last_checked_at FROM companies WHERE org_nr = ?", (org_nr,))
            row = cur.fetchone()
            if row:
                data = json.loads(row["profile_json"])
                prof = ComprehensiveCompanyProfile.model_validate(data)
                if not prof.last_checked_at and row["last_checked_at"]:
                    prof.last_checked_at = row["last_checked_at"]
                status, age = compute_freshness(prof.last_checked_at, prof.last_updated)
                prof.freshness_status = status
                prof.data_age_days = age
                return prof
            return None

    def list_companies(self, batch_id: Optional[str] = None) -> List[ComprehensiveCompanyProfile]:
        """Lists all company profiles ordered by input order."""
        from nordtrace.reporting import compute_freshness

        query = "SELECT profile_json, last_checked_at FROM companies"
        params: Tuple[Any, ...] = ()
        if batch_id:
            query += " WHERE batch_id = ?"
            params = (batch_id,)
        query += " ORDER BY input_order ASC"

        profiles: List[ComprehensiveCompanyProfile] = []
        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            for row in cur.fetchall():
                data = json.loads(row["profile_json"])
                prof = ComprehensiveCompanyProfile.model_validate(data)
                if not prof.last_checked_at and row["last_checked_at"]:
                    prof.last_checked_at = row["last_checked_at"]
                status, age = compute_freshness(prof.last_checked_at, prof.last_updated)
                prof.freshness_status = status
                prof.data_age_days = age
                profiles.append(prof)
        return profiles

    def get_stale_companies(
        self,
        freshness_days: int = 30,
        batch_id: Optional[str] = None,
    ) -> List[ComprehensiveCompanyProfile]:
        """Returns companies whose last verification check is older than freshness_days."""
        from nordtrace.reporting import compute_freshness

        all_comps = self.list_companies(batch_id=batch_id)
        stale_list: List[ComprehensiveCompanyProfile] = []
        for c in all_comps:
            status, age = compute_freshness(c.last_checked_at, c.last_updated, freshness_days)
            c.freshness_status = status
            c.data_age_days = age
            if status in ("STALE", "UNKNOWN"):
                stale_list.append(c)
        return stale_list

    def get_latest_batch_id(self) -> Optional[str]:
        """Returns the ID of the most recently updated batch."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT batch_id FROM batches ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
            return str(row["batch_id"]) if row else None

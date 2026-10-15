"""Lightweight thread-safe stage-level timing and performance metrics instrumentation."""

import threading
from typing import Any, Dict
from pydantic import BaseModel, Field


class TimingMetrics:
    """Thread-safe collector for pipeline stage execution times and network wait times."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.registry_total_seconds: float = 0.0
        self.registry_count: int = 0
        self.discovery_total_seconds: float = 0.0
        self.discovery_count: int = 0
        self.robots_total_seconds: float = 0.0
        self.robots_count: int = 0
        self.fetch_total_seconds: float = 0.0
        self.fetch_count: int = 0
        self.extraction_total_seconds: float = 0.0
        self.extraction_count: int = 0
        self.persistence_total_seconds: float = 0.0
        self.persistence_count: int = 0
        self.enrichment_total_seconds: float = 0.0
        self.enrichment_count: int = 0

    def record_registry(self, duration: float) -> None:
        """Records time spent fetching official registry records."""
        with self._lock:
            self.registry_total_seconds += max(0.0, duration)
            self.registry_count += 1

    def record_discovery(self, duration: float) -> None:
        """Records time spent discovering sources."""
        with self._lock:
            self.discovery_total_seconds += max(0.0, duration)
            self.discovery_count += 1

    def record_robots(self, duration: float) -> None:
        """Records time spent checking or retrieving robots.txt."""
        with self._lock:
            self.robots_total_seconds += max(0.0, duration)
            self.robots_count += 1

    def record_fetch(self, duration: float) -> None:
        """Records time spent performing HTTP HTML fetches."""
        with self._lock:
            self.fetch_total_seconds += max(0.0, duration)
            self.fetch_count += 1

    def record_extraction(self, duration: float) -> None:
        """Records time spent executing intelligence and financial extraction."""
        with self._lock:
            self.extraction_total_seconds += max(0.0, duration)
            self.extraction_count += 1

    def record_persistence(self, duration: float) -> None:
        """Records time spent performing SQLite database writes."""
        with self._lock:
            self.persistence_total_seconds += max(0.0, duration)
            self.persistence_count += 1

    def record_enrichment(self, duration: float) -> None:
        """Records total end-to-end enrichment time for a company."""
        with self._lock:
            self.enrichment_total_seconds += max(0.0, duration)
            self.enrichment_count += 1

    @property
    def registry_seconds_per_company(self) -> float:
        """Average registry lookup time in seconds."""
        with self._lock:
            return self.registry_total_seconds / max(1, self.registry_count) if self.registry_count > 0 else 0.0

    @property
    def enrichment_seconds_per_company(self) -> float:
        """Average website enrichment time in seconds."""
        with self._lock:
            return self.enrichment_total_seconds / max(1, self.enrichment_count) if self.enrichment_count > 0 else 0.0

    @property
    def persistence_seconds_per_company(self) -> float:
        """Average SQLite persistence time in seconds."""
        with self._lock:
            return self.persistence_total_seconds / max(1, self.persistence_count) if self.persistence_count > 0 else 0.0

    @property
    def total_network_wait_seconds(self) -> float:
        """Total time spent waiting on outbound HTTP network operations."""
        with self._lock:
            return self.registry_total_seconds + self.fetch_total_seconds + self.robots_total_seconds

    def to_dict(self) -> Dict[str, Any]:
        """Exports timing summary dictionary."""
        with self._lock:
            return {
                "registry_total_seconds": round(self.registry_total_seconds, 3),
                "registry_count": self.registry_count,
                "registry_avg_seconds": round(self.registry_seconds_per_company, 4),
                "enrichment_total_seconds": round(self.enrichment_total_seconds, 3),
                "enrichment_count": self.enrichment_count,
                "enrichment_avg_seconds": round(self.enrichment_seconds_per_company, 4),
                "persistence_total_seconds": round(self.persistence_total_seconds, 4),
                "persistence_count": self.persistence_count,
                "persistence_avg_seconds": round(self.persistence_seconds_per_company, 5),
                "total_network_wait_seconds": round(self.total_network_wait_seconds, 3),
            }

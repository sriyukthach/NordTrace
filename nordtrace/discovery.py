"""Phase 2 & Phase 3 source discovery and evidence verification orchestration module."""

import time
from typing import List, Optional, Set, Tuple
from nordtrace.budget import RequestBudget
from nordtrace.exceptions import (
    BrregAPIError,
    BudgetExhaustedError,
    RobotsForbiddenError,
    SSRFError,
)
from nordtrace.fetcher import SafeFetcher
from nordtrace.metrics import TimingMetrics
from nordtrace.models import (
    CompanyProfile,
    CrawlSummary,
    DiscoveredSource,
    SourcesDiscoveryResult,
)
from nordtrace.robots import RobotsCache
from nordtrace.url_utils import (
    categorize_url,
    extract_domain,
    is_same_domain,
    normalize_url,
)
from nordtrace.verifier import evaluate_page, parse_html

MAX_PAGES_DEFAULT = 10
MAX_DEPTH_DEFAULT = 2


class SourceDiscoverer:
    """Discovers and verifies web sources for a verified Norwegian company."""

    def __init__(
        self,
        max_pages: int = MAX_PAGES_DEFAULT,
        max_depth: int = MAX_DEPTH_DEFAULT,
        fetcher: Optional[SafeFetcher] = None,
        robots_cache: Optional[RobotsCache] = None,
        budget: Optional[RequestBudget] = None,
        timing_metrics: Optional[TimingMetrics] = None,
    ):
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.budget = budget
        self.timing_metrics = timing_metrics
        self.fetcher = fetcher or SafeFetcher(budget=budget)
        self.robots_cache = robots_cache or RobotsCache(budget=budget)

    def discover_sources(self, company: CompanyProfile) -> SourcesDiscoveryResult:
        """Executes bounded source discovery starting from official registered company website.

        Args:
            company: Verified CompanyProfile instance.

        Returns:
            SourcesDiscoveryResult containing discovered sources and crawl metrics.
        """
        start_time = time.time()
        warnings: List[str] = []

        raw_website = company.website
        if not raw_website:
            return SourcesDiscoveryResult(
                company=company,
                official_website=None,
                verification_status="UNVERIFIED",
                sources=[],
                summary=CrawlSummary(
                    total_sources_discovered=0,
                    verified_count=0,
                    probable_count=0,
                    rejected_count=0,
                    network_requests_count=0,
                    runtime_seconds=0.0,
                    warnings=["No official website registered in Brønnøysund Register Centre."],
                ),
            )

        official_website = normalize_url(raw_website)
        if not official_website:
            return SourcesDiscoveryResult(
                company=company,
                official_website=None,
                verification_status="UNVERIFIED",
                sources=[],
                summary=CrawlSummary(
                    total_sources_discovered=0,
                    verified_count=0,
                    probable_count=0,
                    rejected_count=0,
                    network_requests_count=0,
                    runtime_seconds=0.0,
                    warnings=[f"Registered website '{raw_website}' could not be normalized into a valid URL."],
                ),
            )

        official_domain = extract_domain(official_website)

        queue: List[Tuple[str, int]] = [(official_website, 0)]
        queued_urls: Set[str] = {official_website}
        visited_urls: Set[str] = set()
        discovered_sources: List[DiscoveredSource] = []
        network_requests_count = 0

        while queue and len(discovered_sources) < self.max_pages:
            url, depth = queue.pop(0)
            norm_url = normalize_url(url)
            if not norm_url or norm_url in visited_urls or depth > self.max_depth:
                continue

            visited_urls.add(norm_url)

            # Check robots.txt
            t0_rob = time.time()
            is_allowed = self.robots_cache.is_allowed(norm_url)
            if self.timing_metrics:
                self.timing_metrics.record_robots(time.time() - t0_rob)

            if not is_allowed:
                discovered_sources.append(
                    DiscoveredSource(
                        target_org_nr=company.org_nr,
                        source_url=url,
                        normalized_url=norm_url,
                        page_title=None,
                        source_category=categorize_url(norm_url),
                        verification_status="REJECTED",
                        verification_explanation="Disallowed by domain robots.txt rules.",
                        retrieval_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        content_hash=None,
                        http_status=None,
                        redirect_destination=None,
                        raw_html=None,
                    )
                )
                continue

            try:
                t0_fetch = time.time()
                fetch_res = self.fetcher.fetch(norm_url)
                if self.timing_metrics:
                    self.timing_metrics.record_fetch(time.time() - t0_fetch)
                network_requests_count += 1
            except BudgetExhaustedError:
                raise
            except Exception as exc:
                discovered_sources.append(
                    DiscoveredSource(
                        target_org_nr=company.org_nr,
                        source_url=url,
                        normalized_url=norm_url,
                        page_title=None,
                        source_category=categorize_url(norm_url),
                        verification_status="REJECTED",
                        verification_explanation=f"Fetch failed: {exc}",
                        retrieval_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        content_hash=None,
                        http_status=None,
                        redirect_destination=None,
                        raw_html=None,
                    )
                )
                continue

            # Parse HTML
            page_title, page_text, extracted_links = parse_html(fetch_res.text, fetch_res.final_url)
            source_category = categorize_url(norm_url, page_title or "", page_text)

            is_seed = (norm_url == official_website or fetch_res.final_url == official_website)
            status, explanation = evaluate_page(
                target_org_nr=company.org_nr,
                legal_name=company.name,
                official_domain=official_domain,
                url=fetch_res.final_url,
                status_code=fetch_res.status_code,
                page_title=page_title,
                page_text=page_text,
                is_seed_url=is_seed,
            )

            discovered_sources.append(
                DiscoveredSource(
                    target_org_nr=company.org_nr,
                    source_url=url,
                    normalized_url=norm_url,
                    page_title=page_title,
                    source_category=source_category,
                    verification_status=status,
                    verification_explanation=explanation,
                    retrieval_timestamp=fetch_res.retrieved_at,
                    content_hash=fetch_res.content_hash,
                    http_status=fetch_res.status_code,
                    redirect_destination=fetch_res.final_url if fetch_res.final_url != norm_url else None,
                    raw_html=fetch_res.text,  # Retain raw HTML content in memory for Phase 3 reuse
                )
            )

            # Discover further internal links if depth < max_depth
            if depth < self.max_depth and len(discovered_sources) < self.max_pages:
                high_priority_links: List[str] = []
                standard_links: List[str] = []

                for link_url, link_text in extracted_links:
                    norm_link = normalize_url(link_url)
                    if not norm_link or norm_link in visited_urls:
                        continue
                    if not is_same_domain(norm_link, official_domain):
                        continue

                    category = categorize_url(norm_link, "", link_text)
                    if category != "other":
                        high_priority_links.append(norm_link)
                    else:
                        standard_links.append(norm_link)

                # Queue high priority links first
                for link in high_priority_links:
                    if link not in queued_urls:
                        queue.append((link, depth + 1))
                        queued_urls.add(link)
                for link in standard_links:
                    if link not in queued_urls:
                        queue.append((link, depth + 1))
                        queued_urls.add(link)

        runtime = round(time.time() - start_time, 3)
        if self.timing_metrics:
            self.timing_metrics.record_discovery(runtime)

        verified_cnt = sum(1 for s in discovered_sources if s.verification_status == "VERIFIED")
        probable_cnt = sum(1 for s in discovered_sources if s.verification_status == "PROBABLE")
        rejected_cnt = sum(1 for s in discovered_sources if s.verification_status == "REJECTED")

        overall_status = "VERIFIED" if verified_cnt > 0 else ("PROBABLE" if probable_cnt > 0 else "UNVERIFIED")

        return SourcesDiscoveryResult(
            company=company,
            official_website=official_website,
            verification_status=overall_status,
            sources=discovered_sources,
            summary=CrawlSummary(
                total_sources_discovered=len(discovered_sources),
                verified_count=verified_cnt,
                probable_count=probable_cnt,
                rejected_count=rejected_cnt,
                network_requests_count=network_requests_count,
                runtime_seconds=runtime,
                warnings=warnings,
            ),
        )

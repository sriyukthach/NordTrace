"""Phase 3 structured company intelligence extraction engine."""

import re
from typing import List, Optional, Set, Tuple
from nordtrace.extractor_utils import (
    extract_emails,
    extract_metadata,
    extract_phone_numbers,
    find_evidence_snippet,
)
from nordtrace.models import (
    BusinessLocation,
    CompanyDescription,
    CompanyIntelligence,
    CompanyProfile,
    ContactInfo,
    DiscoveredSource,
    ImportantLink,
    IntelligenceSummary,
    LeadershipRecord,
    ProductService,
)
from nordtrace.reporting import NAV_OR_BOILERPLATE_HEADINGS
from nordtrace.url_utils import is_same_domain

LEADERSHIP_ROLES = [
    ("Chief Executive Officer", r"\b(chief executive officer|ceo|konsernsjef)\b"),
    ("Chief Financial Officer", r"\b(chief financial officer|cfo|finansdirektør)\b"),
    ("Chair of the Board", r"\b(chair of the board|chairperson|styreleder|board chair)\b"),
    ("Board Member", r"\b(board member|styremedlem)\b"),
    ("President", r"\b(president and ceo|president)\b"),
    ("Executive Vice President", r"\b(executive vice president|evp)\b"),
]

FORMER_INDICATORS = [
    "former", "ex-", "previous", "retired", "resigned", "tidligere", "avga", "forrige"
]

COOKIE_OR_LEGAL_KEYWORDS = [
    "cookie", "privacy policy", "terms of use", "all rights reserved", "copyright",
    "personvern", "vilkår", "browser", "javascript"
]


class IntelligenceExtractor:
    """Extracts, ranks, and validates evidence-backed facts from company registry and web sources."""

    def extract_intelligence(
        self,
        company: CompanyProfile,
        official_website: Optional[str],
        discovered_sources: List[DiscoveredSource],
        runtime_seconds: float = 0.0,
    ) -> CompanyIntelligence:
        """Runs the complete intelligence extraction pipeline.

        Args:
            company: Verified registry CompanyProfile.
            official_website: Normalized official website URL.
            discovered_sources: List of discovered sources.
            runtime_seconds: Execution runtime in seconds.

        Returns:
            CompanyIntelligence instance.
        """
        warnings: List[str] = []

        # Parse source HTML contents into reusable metadata containers
        source_parsed_map = {}
        for src in discovered_sources:
            if src.raw_html:
                source_parsed_map[src.normalized_url] = extract_metadata(src.raw_html, src.normalized_url)

        # 1. Company Description Extraction
        description = self._extract_description(company, discovered_sources, source_parsed_map)

        # 2. Contact Information Extraction
        contacts = self._extract_contacts(company, discovered_sources, source_parsed_map)

        # 3. Leadership Extraction
        leadership = self._extract_leadership(company, discovered_sources, source_parsed_map)

        # 4. Business Locations Extraction
        locations = self._extract_locations(company, discovered_sources, source_parsed_map)

        # 5. Products & Services Extraction
        products_services = self._extract_products_services(company, discovered_sources, source_parsed_map)

        # 6. Important Links Classification
        important_links = self._extract_important_links(discovered_sources)

        # Count facts metrics
        facts_extracted = (
            (1 if description else 0)
            + len(contacts)
            + len(leadership)
            + len(locations)
            + len(products_services)
            + len(important_links)
        )
        facts_verified = (
            (1 if description and description.verification_status == "VERIFIED" else 0)
            + sum(1 for c in contacts if c.verification_status == "VERIFIED")
            + sum(1 for l in leadership if l.verification_status == "VERIFIED")
            + sum(1 for loc in locations if loc.verification_status == "VERIFIED")
            + sum(1 for p in products_services if p.verification_status == "VERIFIED")
            + sum(1 for link in important_links if link.verification_status == "VERIFIED")
        )
        facts_ambiguous = facts_extracted - facts_verified

        summary = IntelligenceSummary(
            facts_extracted=facts_extracted,
            facts_verified=facts_verified,
            facts_ambiguous=facts_ambiguous,
            runtime_seconds=runtime_seconds,
            warnings=warnings,
        )

        return CompanyIntelligence(
            company=company,
            official_website=official_website,
            description=description,
            contacts=contacts,
            leadership=leadership,
            locations=locations,
            products_services=products_services,
            important_links=important_links,
            sources=discovered_sources,
            summary=summary,
        )

    def _extract_description(
        self,
        company: CompanyProfile,
        sources: List[DiscoveredSource],
        parsed_map: dict,
    ) -> Optional[CompanyDescription]:
        """Extracts and ranks the best supported company description."""
        candidates = []

        # Target candidates on about/homepage/other verified sources
        priority_sources = [
            s for s in sources if s.verification_status in ("VERIFIED", "PROBABLE")
            and s.source_category in ("about", "homepage", "other")
        ]

        for src in priority_sources:
            parsed = parsed_map.get(src.normalized_url)
            if not parsed:
                continue

            # 1. JSON-LD Organization description
            for item in parsed.json_ld_data:
                desc = item.get("description")
                if desc and isinstance(desc, str) and len(desc.strip()) > 20:
                    desc_clean = desc.strip()
                    if not any(k in desc_clean.lower() for k in COOKIE_OR_LEGAL_KEYWORDS):
                        candidates.append((
                            10,  # Score
                            CompanyDescription(
                                description=desc_clean,
                                source_url=src.normalized_url,
                                evidence_text=find_evidence_snippet(desc_clean, desc_clean[:30]),
                                extraction_method="json_ld_organization",
                                confidence="HIGH",
                                verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                            )
                        ))

            # 2. Open Graph description
            og_desc = parsed.meta_tags.get("og:description") or parsed.meta_tags.get("description")
            if og_desc and len(og_desc.strip()) > 20:
                desc_clean = og_desc.strip()
                if not any(k in desc_clean.lower() for k in COOKIE_OR_LEGAL_KEYWORDS):
                    score = 9 if src.source_category in ("about", "homepage") else 7
                    candidates.append((
                        score,
                        CompanyDescription(
                            description=desc_clean,
                            source_url=src.normalized_url,
                            evidence_text=find_evidence_snippet(desc_clean, desc_clean[:30]),
                            extraction_method="og_meta_description",
                            confidence="HIGH",
                            verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                        )
                    ))

            # 3. Lead Paragraphs on About/Homepage
            if src.source_category in ("about", "homepage"):
                for p in parsed.paragraphs[:3]:
                    if len(p) > 40 and not any(k in p.lower() for k in COOKIE_OR_LEGAL_KEYWORDS):
                        # Verify company brand name or legal name in paragraph
                        brand = company.name.split()[0].lower()
                        if brand in p.lower() or company.name.lower() in p.lower():
                            candidates.append((
                                6,
                                CompanyDescription(
                                    description=p,
                                    source_url=src.normalized_url,
                                    evidence_text=find_evidence_snippet(p, brand),
                                    extraction_method="html_paragraph",
                                    confidence="MEDIUM",
                                    verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                                )
                            ))

        if not candidates:
            return None

        # Sort candidates by score descending
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _extract_contacts(
        self,
        company: CompanyProfile,
        sources: List[DiscoveredSource],
        parsed_map: dict,
    ) -> List[ContactInfo]:
        """Extracts phone numbers, email addresses, contact page URL, and social media links."""
        contacts: List[ContactInfo] = []
        seen_values: Set[str] = set()

        # 1. Registered Contact Page URL
        contact_sources = [s for s in sources if s.source_category == "contact" and s.verification_status in ("VERIFIED", "PROBABLE")]
        if contact_sources:
            c_src = contact_sources[0]
            contacts.append(
                ContactInfo(
                    contact_type="contact_url",
                    value=c_src.normalized_url,
                    label="Official Contact Page",
                    source_url=c_src.normalized_url,
                    evidence_text=f"Official contact page discovered at {c_src.normalized_url}",
                    extraction_method="source_discovery",
                    confidence="HIGH",
                    verification_status="VERIFIED" if c_src.verification_status == "VERIFIED" else "AMBIGUOUS",
                )
            )
            seen_values.add(c_src.normalized_url)

        # 2. Web & JSON-LD Phone & Email extraction
        for src in sources:
            if src.verification_status == "REJECTED":
                continue

            parsed = parsed_map.get(src.normalized_url)
            if not parsed:
                continue

            # Social links
            for link, platform in parsed.social_links:
                if link not in seen_values:
                    seen_values.add(link)
                    contacts.append(
                        ContactInfo(
                            contact_type="social_media",
                            value=link,
                            label=platform.capitalize(),
                            source_url=src.normalized_url,
                            evidence_text=f"Official social media link ({platform}) published on website",
                            extraction_method="social_link_href",
                            confidence="HIGH",
                            verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                        )
                    )

            # Emails
            full_text = " ".join([p for p in parsed.paragraphs] + [h[1] for h in parsed.headings])
            for email, evidence in extract_emails(full_text):
                if email not in seen_values:
                    seen_values.add(email)
                    contacts.append(
                        ContactInfo(
                            contact_type="email",
                            value=email,
                            label="Contact Email",
                            source_url=src.normalized_url,
                            evidence_text=evidence,
                            extraction_method="regex_text_match",
                            confidence="HIGH" if src.source_category == "contact" else "MEDIUM",
                            verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                        )
                    )

            # Phone numbers
            for phone, evidence in extract_phone_numbers(full_text):
                if phone not in seen_values:
                    seen_values.add(phone)
                    contacts.append(
                        ContactInfo(
                            contact_type="phone",
                            value=phone,
                            label="Contact Phone",
                            source_url=src.normalized_url,
                            evidence_text=evidence,
                            extraction_method="regex_phone_match",
                            confidence="HIGH" if src.source_category in ("contact", "homepage") else "MEDIUM",
                            verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                        )
                    )

        return contacts

    def _extract_leadership(
        self,
        company: CompanyProfile,
        sources: List[DiscoveredSource],
        parsed_map: dict,
    ) -> List[LeadershipRecord]:
        """Extracts executive leadership team and board members."""
        records: List[LeadershipRecord] = []
        seen_keys: Set[str] = set()

        leadership_sources = [
            s for s in sources if s.verification_status in ("VERIFIED", "PROBABLE")
            and s.source_category in ("leadership", "about", "homepage")
        ]

        for src in leadership_sources:
            parsed = parsed_map.get(src.normalized_url)
            if not parsed:
                continue

            full_text_blocks = parsed.paragraphs + [h[1] for h in parsed.headings]

            for block in full_text_blocks:
                # Reject former leadership text
                block_lower = block.lower()
                if any(f in block_lower for f in FORMER_INDICATORS):
                    continue

                for role_label, role_pattern in LEADERSHIP_ROLES:
                    if re.search(role_pattern, block_lower):
                        # Extract name pattern (e.g., "Anders Opedal" or "Kjetil Hove")
                        # Match 2 to 3 capitalized words
                        name_matches = re.findall(r"\b[A-Z][a-zæøå]+\s+(?:[A-Z][a-zæøå]+\s+)?[A-Z][a-zæøå]+\b", block)
                        for name in name_matches:
                            # Filter out company names or generic terms
                            if company.name.split()[0].lower() in name.lower() or name.lower() in ("board of", "executive vice"):
                                continue

                            key = f"{name.lower()}:{role_label.lower()}"
                            if key not in seen_keys:
                                seen_keys.add(key)
                                evidence = find_evidence_snippet(block, name)
                                records.append(
                                    LeadershipRecord(
                                        person_name=name,
                                        role=role_label,
                                        company_org_nr=company.org_nr,
                                        source_url=src.normalized_url,
                                        evidence_text=evidence,
                                        confidence="HIGH" if src.source_category == "leadership" else "MEDIUM",
                                        verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                                        retrieved_at=src.retrieval_timestamp,
                                    )
                                )

        return records

    def _extract_locations(
        self,
        company: CompanyProfile,
        sources: List[DiscoveredSource],
        parsed_map: dict,
    ) -> List[BusinessLocation]:
        """Extracts authoritative registry locations and website office locations."""
        locations: List[BusinessLocation] = []

        # Authoritative Brønnøysund Registry Locations
        if company.business_address:
            addr = company.business_address
            locations.append(
                BusinessLocation(
                    location_type="registered_office",
                    address_lines=addr.address_lines,
                    postal_code=addr.postal_code,
                    city=addr.city,
                    country=addr.country or "Norge",
                    source_url=company.source.source_url,
                    evidence_text=f"Official registered business address from Brønnøysund Register Centre: {addr.address_lines}, {addr.postal_code} {addr.city}",
                    extraction_method="bronnoysund_api_registry",
                    confidence="HIGH",
                    verification_status="VERIFIED",
                )
            )

        if company.postal_address:
            addr = company.postal_address
            locations.append(
                BusinessLocation(
                    location_type="postal_address",
                    address_lines=addr.address_lines,
                    postal_code=addr.postal_code,
                    city=addr.city,
                    country=addr.country or "Norge",
                    source_url=company.source.source_url,
                    evidence_text=f"Official postal address from Brønnøysund Register Centre: {addr.address_lines}, {addr.postal_code} {addr.city}",
                    extraction_method="bronnoysund_api_registry",
                    confidence="HIGH",
                    verification_status="VERIFIED",
                )
            )

        return locations

    def _extract_products_services(
        self,
        company: CompanyProfile,
        sources: List[DiscoveredSource],
        parsed_map: dict,
    ) -> List[ProductService]:
        """Extracts products and services descriptions."""
        products: List[ProductService] = []
        seen_names: Set[str] = set()

        product_sources = [
            s for s in sources if s.verification_status in ("VERIFIED", "PROBABLE")
            and s.source_category in ("products_services", "about", "homepage")
        ]

        for src in product_sources:
            parsed = parsed_map.get(src.normalized_url)
            if not parsed:
                continue

            for tag, heading_text in parsed.headings:
                if len(heading_text) > 4 and len(heading_text) < 60:
                    heading_clean = heading_text.strip().lower()
                    if (
                        heading_clean in NAV_OR_BOILERPLATE_HEADINGS
                        or any(k in heading_clean for k in COOKIE_OR_LEGAL_KEYWORDS + ["contact", "careers"])
                        or any(
                            bp in heading_clean
                            for bp in [
                                "useful links", "quick links", "latest news", "site info", "follow us",
                                "all rights reserved", "more news", "forthcoming events", "upcoming events"
                            ]
                        )
                    ):
                        continue

                    name = heading_text.strip()
                    if name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            evidence = find_evidence_snippet(heading_text, heading_text)
                            products.append(
                                ProductService(
                                    name=name,
                                    description=f"Product or service offering extracted from {src.source_category} page.",
                                    category="Product/Service Offering",
                                    source_url=src.normalized_url,
                                    evidence_text=evidence,
                                    extraction_method="html_heading",
                                    confidence="HIGH" if src.source_category == "products_services" else "MEDIUM",
                                    verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                                )
                            )

        return products

    def _extract_important_links(self, sources: List[DiscoveredSource]) -> List[ImportantLink]:
        """Classifies and deduplicates important official links discovered."""
        important_links: List[ImportantLink] = []
        seen_categories: Set[str] = set()

        # Category priority mapping
        category_labels = {
            "homepage": "Official Homepage",
            "about": "About Us",
            "contact": "Contact Information",
            "leadership": "Leadership & Management",
            "investor": "Investor Relations",
            "annual_reports": "Annual Reports",
            "careers": "Careers",
            "news": "News & Press Releases",
            "products_services": "Products & Services",
            "legal": "Legal & Privacy Policy",
        }

        for src in sources:
            if src.verification_status == "REJECTED":
                continue

            cat = src.source_category
            if cat in category_labels and cat not in seen_categories:
                seen_categories.add(cat)
                important_links.append(
                    ImportantLink(
                        label=category_labels[cat],
                        url=src.normalized_url,
                        category=cat,
                        source_url=src.source_url,
                        confidence="HIGH",
                        verification_status="VERIFIED" if src.verification_status == "VERIFIED" else "AMBIGUOUS",
                    )
                )

        return important_links

from datetime import datetime, timezone
import time
from typing import Any, Dict, Optional
import httpx

from nordtrace.budget import RequestBudget
from nordtrace.exceptions import (
    BrregAPIError,
    BudgetExhaustedError,
    CompanyNotFoundError,
    MalformedAPIResponseError,
)
from nordtrace.models import (
    Address,
    CompanyProfile,
    CompanyStatus,
    IndustryCode,
    OrgForm,
    SourceMetadata,
)
from nordtrace.validator import validate_org_nr

BRREG_BASE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
DEFAULT_TIMEOUT = 10.0


class BrregClient:
    """Client for fetching Norwegian company details from Enhetsregisteret API."""

    def __init__(
        self,
        base_url: str = BRREG_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        budget: Optional[RequestBudget] = None,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.budget = budget
        self._client = client

    def _parse_address(self, data: Optional[Dict[str, Any]]) -> Optional[Address]:
        if not data or not isinstance(data, dict):
            return None
        return Address(
            address_lines=data.get("adresse") if isinstance(data.get("adresse"), list) else None,
            postal_code=data.get("postnummer"),
            city=data.get("poststed"),
            municipality=data.get("kommune"),
            municipality_code=data.get("kommunenummer"),
            country=data.get("land"),
            country_code=data.get("landkode"),
        )

    def _parse_org_form(self, data: Optional[Dict[str, Any]]) -> Optional[OrgForm]:
        if not data or not isinstance(data, dict):
            return None
        code = data.get("kode")
        description = data.get("beskrivelse")
        if code and description:
            return OrgForm(code=str(code), description=str(description))
        return None

    def _parse_industry_code(self, data: Optional[Dict[str, Any]]) -> Optional[IndustryCode]:
        if not data or not isinstance(data, dict):
            return None
        code = data.get("kode")
        description = data.get("beskrivelse")
        if code and description:
            return IndustryCode(code=str(code), description=str(description))
        return None

    def _parse_status(self, data: Dict[str, Any]) -> CompanyStatus:
        is_bankrupt = bool(data.get("konkurs", False))
        under_liquidation = bool(data.get("underAvvikling", False))
        under_forced = bool(data.get("underTvangsavviklingEllerTvangsopplosning", False))
        deletion_date = data.get("slettedato")

        if deletion_date:
            summary = f"Deleted ({deletion_date})"
        elif is_bankrupt:
            summary = "Bankrupt"
        elif under_forced:
            summary = "Under Forced Liquidation/Dissolution"
        elif under_liquidation:
            summary = "Under Liquidation"
        else:
            summary = "Active"

        return CompanyStatus(
            is_bankrupt=is_bankrupt,
            under_liquidation=under_liquidation,
            under_forced_liquidation_or_dissolution=under_forced,
            deletion_date=deletion_date,
            summary=summary,
        )

    def fetch_company(self, org_nr: str) -> CompanyProfile:
        """Validates organisation number and retrieves company profile from Brønnøysund API.

        Args:
            org_nr: Norwegian organisation number (9 digits).

        Returns:
            CompanyProfile instance containing extracted structured company information.

        Raises:
            InvalidOrgNumberError: If org_nr fails Modulus 11 validation.
            CompanyNotFoundError: If company does not exist in register (HTTP 404).
            BrregAPIError: If HTTP error, timeout, or network issue occurs.
            MalformedAPIResponseError: If response body is unparseable or unexpected.
        """
        validated_nr = validate_org_nr(org_nr)
        url = f"{self.base_url}/{validated_nr}"
        retrieved_at = datetime.now(timezone.utc).isoformat()

        headers = {"Accept": "application/json"}

        event_hooks = {"request": [self.budget.create_httpx_hook()]} if self.budget else None
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = 1.0
                    if retry_after:
                        try:
                            wait_time = min(5.0, float(retry_after))
                        except ValueError:
                            wait_time = 1.0
                    time.sleep(wait_time)
                    if self.budget and not self.budget.can_request(1):
                        raise BudgetExhaustedError("Budget exhausted while retrying after rate limit (HTTP 429).")
                    response = self._client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=True, event_hooks=event_hooks) as client:
                    response = client.get(url, headers=headers)
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait_time = 1.0
                        if retry_after:
                            try:
                                wait_time = min(5.0, float(retry_after))
                            except ValueError:
                                wait_time = 1.0
                        time.sleep(wait_time)
                        if self.budget and not self.budget.can_request(1):
                            raise BudgetExhaustedError("Budget exhausted while retrying after rate limit (HTTP 429).")
                        response = client.get(url, headers=headers)
        except BudgetExhaustedError:
            raise
        except httpx.TimeoutException as exc:
            raise BrregAPIError(f"HTTP request timed out while contacting Brønnøysund API ({url}).") from exc
        except httpx.RequestError as exc:
            raise BrregAPIError(f"Network error while contacting Brønnøysund API ({url}): {exc}") from exc

        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"Company with organisation number '{validated_nr}' was not found in Brønnøysund Register Centre."
            )

        if response.status_code != 200:
            raise BrregAPIError(
                f"Brønnøysund API returned HTTP status {response.status_code} for org nr '{validated_nr}'."
            )

        try:
            data = response.json()
        except Exception as exc:
            raise MalformedAPIResponseError("Failed to parse response body from Brønnøysund API as JSON.") from exc

        if not isinstance(data, dict):
            raise MalformedAPIResponseError("Brønnøysund API response body must be a JSON object.")

        extracted_org_nr = data.get("organisasjonsnummer")
        legal_name = data.get("navn")

        if not extracted_org_nr or not legal_name:
            raise MalformedAPIResponseError(
                "Missing essential fields ('organisasjonsnummer' or 'navn') in Brønnøysund API response."
            )

        source_meta = SourceMetadata(
            source_name="Brønnøysund Register Centre (Enhetsregisteret)",
            source_url=url,
            retrieved_at=retrieved_at,
        )

        return CompanyProfile(
            org_nr=str(extracted_org_nr),
            name=str(legal_name),
            org_form=self._parse_org_form(data.get("organisasjonsform")),
            registration_date=data.get("registreringsdatoEnhetsregisteret"),
            business_address=self._parse_address(data.get("forretningsadresse")),
            postal_address=self._parse_address(data.get("postadresse")),
            industry_code=self._parse_industry_code(data.get("naeringskode1")),
            website=data.get("hjemmeside"),
            status=self._parse_status(data),
            source=source_meta,
        )

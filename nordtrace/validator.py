"""Norwegian organisation number validator with Modulus 11 checksum verification."""

import re
from nordtrace.exceptions import InvalidOrgNumberError

WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2]


def validate_org_nr(org_nr: str) -> str:
    """Validates a Norwegian organisation number using length, digit-only check, and Modulus 11 checksum.

    Args:
        org_nr: String representation of the organisation number.

    Returns:
        Cleaned 9-digit organisation number string.

    Raises:
        InvalidOrgNumberError: If the organisation number format or checksum is invalid.
    """
    if not isinstance(org_nr, str):
        raise InvalidOrgNumberError("Organisation number must be a string.")

    cleaned = org_nr.strip().replace(" ", "").replace("-", "")

    if not re.fullmatch(r"\d{9}", cleaned):
        raise InvalidOrgNumberError(
            f"Organisation number '{org_nr}' is invalid. It must consist of exactly 9 digits."
        )

    digits = [int(ch) for ch in cleaned]
    weighted_sum = sum(d * w for d, w in zip(digits[:8], WEIGHTS))
    remainder = weighted_sum % 11

    if remainder == 1:
        raise InvalidOrgNumberError(
            f"Organisation number '{cleaned}' failed Modulus 11 checksum validation (remainder 1 is invalid)."
        )

    expected_check_digit = 0 if remainder == 0 else (11 - remainder)

    if digits[8] != expected_check_digit:
        raise InvalidOrgNumberError(
            f"Organisation number '{cleaned}' failed Modulus 11 checksum validation. "
            f"Expected check digit {expected_check_digit}, got {digits[8]}."
        )

    return cleaned

"""Tests for Norwegian organisation number validation and Modulus 11 checksum."""

import pytest
from nordtrace.exceptions import InvalidOrgNumberError
from nordtrace.validator import validate_org_nr


def test_valid_org_numbers():
    """Test valid Norwegian organisation numbers with correct checksums."""
    assert validate_org_nr("923609016") == "923609016"  # EQUINOR ASA
    assert validate_org_nr("984851006") == "984851006"  # DNB BANK ASA
    assert validate_org_nr(" 923 609 016 ") == "923609016"  # whitespace formatting
    assert validate_org_nr("923-609-016") == "923609016"  # hyphen formatting


def test_invalid_length_or_format():
    """Test non-digit input or incorrect length."""
    with pytest.raises(InvalidOrgNumberError, match="must consist of exactly 9 digits"):
        validate_org_nr("12345678")  # 8 digits

    with pytest.raises(InvalidOrgNumberError, match="must consist of exactly 9 digits"):
        validate_org_nr("1234567890")  # 10 digits

    with pytest.raises(InvalidOrgNumberError, match="must consist of exactly 9 digits"):
        validate_org_nr("ABCDEFGHI")  # non-digits

    with pytest.raises(InvalidOrgNumberError, match="must be a string"):
        validate_org_nr(123456789)  # non-string type


def test_checksum_validation_failures():
    """Test organisation numbers with invalid Modulus 11 check digits."""
    # 923609017 has check digit 7 instead of 6
    with pytest.raises(InvalidOrgNumberError, match="failed Modulus 11 checksum validation"):
        validate_org_nr("923609017")

    # Modulus 11 remainder == 1 edge case (invalid org number)
    # Digits: 1 1 1 1 1 1 1 1 => sum = 1*3 + 1*2 + 1*7 + 1*6 + 1*5 + 1*4 + 1*3 + 1*2 = 32. 32 % 11 = 10. 11 - 10 = 1.
    # Digits: 1 0 0 0 0 0 0 0 => sum = 1*3 = 3. 3 % 11 = 3. 11 - 3 = 8.
    # Digits giving remainder 1: 0 0 0 0 0 0 0 4 => sum = 4*2 = 8. 8 % 11 = 8.
    # Digits: 1 0 0 0 0 0 0 1 => sum = 3 + 2 = 5.
    # Let's find digits where weighted sum % 11 == 1: e.g., 0 0 0 0 0 0 4 0 => sum = 4*3 = 12. 12 % 11 = 1.
    with pytest.raises(InvalidOrgNumberError, match="remainder 1 is invalid"):
        validate_org_nr("000000400")

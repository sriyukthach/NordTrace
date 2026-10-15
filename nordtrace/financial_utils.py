"""Financial numeric parsing, scale normalization, scale detection, metric mapping, result type classification, and evidence validation utilities."""

import re
from typing import Optional, Tuple

METRIC_MAP = [
    ("revenue", [r"\brevenue\b", r"\bdriftsinntekter\b", r"\bsalgsinntekter?\b", r"\btotal revenue\b", r"\bturnover\b"]),
    ("operating_revenue", [r"\boperating revenue\b", r"\bdriftsinntekt\b"]),
    ("operating_profit", [r"\boperating profit\b", r"\bdriftsresultat\b", r"\boperating income\b"]),
    ("EBITDA", [r"\bebitda\b"]),
    ("EBIT", [r"\bebit\b"]),
    ("profit_before_tax", [r"\bprofit before tax\b", r"\bresultat før skatt\b", r"\bordinært resultat før skatt\b", r"\bebt\b"]),
    ("net_income", [r"\bnet income\b", r"\bnet profit\b", r"\bårsresultat\b", r"\barsresultat\b", r"\bperiodens resultat\b", r"\bprofit for the year\b"]),
    ("total_assets", [r"\btotal assets\b", r"\bsum eiendeler\b", r"\btotal eiendeler\b"]),
    ("total_equity", [r"\btotal equity\b", r"\bsum egenkapital\b", r"\begenkapital\b"]),
    ("total_liabilities", [r"\btotal liabilities\b", r"\bsum gjeld\b", r"\btotal gjeld\b"]),
    ("cash_and_cash_equivalents", [r"\bcash and cash equivalents\b", r"\bbankinnskudd\b"]),
]

SCALE_MULTIPLIERS = {
    "units": 1.0,
    "thousands": 1_000.0,
    "millions": 1_000_000.0,
    "billions": 1_000_000_000.0,
}

CURRENT_YEAR = 2026


def parse_financial_number(raw_str: str) -> Optional[float]:
    """Parses financial numeric string with support for parentheses negative values, space separators, and decimals.

    Examples:
        '(1,250)' -> -1250.0
        '12 500' -> 12500.0
        '12.5' -> 12.5
        '12,5' -> 12.5
    """
    if not raw_str or not isinstance(raw_str, str):
        return None

    cleaned = raw_str.strip()
    is_negative = False

    if cleaned.startswith("(") and cleaned.endswith(")"):
        is_negative = True
        cleaned = cleaned[1:-1].strip()
    elif cleaned.startswith(("-", "−")):
        is_negative = True
        cleaned = cleaned[1:].strip()

    # Remove internal spaces used as thousand separators
    cleaned = cleaned.replace(" ", "")

    if re.match(r"^\d{1,3}(,\d{3})+(\.\d+)?$", cleaned):
        cleaned = cleaned.replace(",", "")
    elif re.match(r"^\d{1,3}(\.\d{3})+(,\d+)?$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        val = float(cleaned)
        return -val if is_negative else val
    except ValueError:
        return None


def detect_scale_and_currency(text: str) -> Tuple[str, str]:
    """Detects numeric scale (units, thousands, millions, billions) and currency code from context text."""
    text_lower = text.lower()

    if "usd" in text_lower or "$" in text_lower:
        currency = "USD"
    elif "eur" in text_lower or "€" in text_lower:
        currency = "EUR"
    elif "gbp" in text_lower or "£" in text_lower:
        currency = "GBP"
    else:
        currency = "NOK"

    if any(k in text_lower for k in ["billion", "billions", "milliarder", "bnok", "busd"]):
        scale = "billions"
    elif any(k in text_lower for k in ["million", "millions", "millioner", "mnok", "musd", "meur"]):
        scale = "millions"
    elif any(k in text_lower for k in ["thousand", "thousands", "tusen", "knok", "kusd"]):
        scale = "thousands"
    else:
        scale = "units"

    return scale, currency


def detect_result_type(text: str, fiscal_year: int, current_year: int = CURRENT_YEAR) -> str:
    """Classifies the financial period result type: ACTUAL, FORECAST, GUIDANCE, TARGET, ESTIMATE, or UNKNOWN.

    Rule: Any figure for a future fiscal year (fiscal_year > current_year) or accompanied by guidance/forecast/target
    keywords MUST NOT be classified as ACTUAL.
    """
    text_lower = text.lower()

    if "guidance" in text_lower:
        return "GUIDANCE"
    if any(k in text_lower for k in ["target", "aspire", "ambition"]):
        return "TARGET"
    if any(k in text_lower for k in ["forecast", "outlook", "expected", "projected", "forward-looking"]):
        return "FORECAST"
    if any(k in text_lower for k in ["estimate", "estimated", "consensus"]):
        return "ESTIMATE"

    # Future-year figures default to GUIDANCE or FORECAST
    if fiscal_year > current_year:
        return "GUIDANCE"

    if any(k in text_lower for k in ["actual", "audited", "reported", "financial statements", "annual accounts", "resultat"]):
        return "ACTUAL"

    # If fiscal year is past/current and no forward-looking language, default to ACTUAL
    if fiscal_year <= current_year:
        return "ACTUAL"

    return "UNKNOWN"


def calculate_normalized_value(reported_value: float, scale: str) -> float:
    """Calculates absolute normalized value in base units."""
    multiplier = SCALE_MULTIPLIERS.get(scale, 1.0)
    return round(reported_value * multiplier, 2)


def match_metric_name(label_text: str) -> Optional[str]:
    """Matches a line label or table row heading to a supported financial metric name."""
    label_lower = label_text.lower()
    for metric_name, patterns in METRIC_MAP:
        for pat in patterns:
            if re.search(pat, label_lower):
                return metric_name
    return None


def is_evidence_complete(
    evidence_text: str,
    reported_value: float,
    scale: str,
    fiscal_year: int,
    currency: str,
) -> bool:
    """Validates that the raw evidence text contains all material attributes required for high-confidence verification."""
    if not evidence_text or not isinstance(evidence_text, str):
        return False

    ev_lower = evidence_text.lower()

    # Check for fiscal year presence
    if str(fiscal_year) not in ev_lower:
        return False

    # Check for currency indication (e.g. "nok", "usd", "$", "eur", "kr")
    curr_indicators = [currency.lower()]
    if currency == "USD":
        curr_indicators.append("$")
    elif currency == "EUR":
        curr_indicators.append("€")
    elif currency == "NOK":
        curr_indicators.append("kr")

    if not any(ci in ev_lower for ci in curr_indicators):
        return False

    # Check for scale indication if non-units
    if scale != "units":
        scale_indicators = [scale.lower()]
        if scale == "millions":
            scale_indicators.extend(["million", "millioner", "mnok", "musd"])
        elif scale == "billions":
            scale_indicators.extend(["billion", "milliarder", "bnok", "busd"])
        elif scale == "thousands":
            scale_indicators.extend(["thousand", "tusen", "knok"])

        if not any(si in ev_lower for si in scale_indicators):
            return False

    return True

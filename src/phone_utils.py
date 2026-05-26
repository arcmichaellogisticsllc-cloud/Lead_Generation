"""US phone number normalization and extraction utilities."""
from __future__ import annotations

import re

# Matches common US phone formats, with or without country code
_PHONE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(?:\+?1[-.\s]?)?"
    r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
    r"(?!\d)"
)

# Reject numbers that are clearly not real phones
_OBVIOUSLY_BAD = re.compile(r"^(\d)\1{9}$")  # all same digit, e.g. 1111111111


def normalize_phone(raw: str) -> str | None:
    """Normalize any US phone string to E.164 (+1XXXXXXXXXX), or return None."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if _OBVIOUSLY_BAD.match(digits):
        return None
    # Reject numbers where too many digits are the same (e.g. 5550000000)
    if len(set(digits)) < 3:
        return None
    return f"+1{digits}"


def extract_phones_from_text(text: str) -> list[str]:
    """Return all unique US phone numbers found in text, normalized to E.164."""
    seen: set[str] = set()
    results: list[str] = []
    for match in _PHONE_PATTERN.finditer(text):
        normalized = normalize_phone(match.group())
        if normalized and normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results

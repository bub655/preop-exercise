"""Deterministic value parsers. They read only grounded quotes and never guess.

Temperature conversion uses exact rational arithmetic so 38.0 C is exactly
100.4 F, not 100.39999.
"""

from __future__ import annotations

import re
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction


class UnitConflict(ValueError):
    """A dual reading (e.g. '38.6 C (99.0 F)') disagrees with itself."""


_BP_RE = re.compile(r"(?<![\d/.])(\d{2,3})\s*/\s*(\d{2,3})(?![\d/%])")
_DUAL = r"(?:\s*\(\s*(?P<val2>\d{2,3}(?:\.\d+)?)\s*°?\s*(?P<unit2>[cf])\s*\))?"
# A bare "92 F" is not a temperature (it could be an age/sex token). A reading needs a
# label, a degree sign, or a decimal point.
TEMP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:temperature|temp|t)\b\.?\s*:?\s*(?P<val>\d{2,3}(?:\.\d+)?)\s*°?\s*(?P<unit>[cf])\b" + _DUAL, re.IGNORECASE),
    re.compile(r"(?P<val>\d{2,3}\.\d+)\s*°?\s*(?P<unit>[cf])\b" + _DUAL, re.IGNORECASE),
    re.compile(r"(?P<val>\d{2,3})\s*°\s*(?P<unit>[cf])\b" + _DUAL, re.IGNORECASE),
]
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_US_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_ONE_DP = Decimal("0.1")


def q1(x: Decimal) -> Decimal:
    """Round to one decimal place, half up. The single rounding rule for temperatures."""
    return x.quantize(_ONE_DP, rounding=ROUND_HALF_UP)


_q1 = q1


def c_to_f(c: Decimal) -> Decimal:
    f = Fraction(c) * Fraction(9, 5) + 32
    return _q1(Decimal(f.numerator) / Decimal(f.denominator))


def parse_bp(quote: str) -> tuple[int, int] | None:
    for m in _BP_RE.finditer(quote):
        s, d = int(m.group(1)), int(m.group(2))
        if 50 <= s <= 300 and 20 <= d <= 200:
            return s, d
    return None


def _as_f(value: str, unit: str) -> Decimal:
    v = Decimal(value)
    return c_to_f(v) if unit.upper() == "C" else _q1(v)


def iter_temp_matches(text: str):
    """Yield (match, start) for every plausible temperature reading, in text order."""
    found: list[tuple[int, re.Match[str]]] = []
    for pattern in TEMP_PATTERNS:
        for m in pattern.finditer(text):
            unit = m.group("unit").upper()
            num = Decimal(m.group("val"))
            plausible = (30 <= num <= 45) if unit == "C" else (85 <= num <= 115)
            if plausible and not any(s <= m.start("val") < e for s, e in ((f.start("val"), f.end()) for _, f in found)):
                found.append((m.start(), m))
    found.sort(key=lambda t: t[0])
    return found


def parse_temp_f(quote: str) -> tuple[Decimal, str] | None:
    """Return (temperature_f, source_unit) or None. Raises UnitConflict."""
    for _, m in iter_temp_matches(quote):
        value, unit = m.group("val"), m.group("unit").upper()
        temp_f = _as_f(value, unit)
        if m.group("val2"):
            other = _as_f(m.group("val2"), m.group("unit2"))
            if abs(other - temp_f) > Decimal("0.2"):
                raise UnitConflict(f"{quote!r}: {temp_f} F vs {other} F")
            if m.group("unit2").upper() == "F":
                temp_f = other
        return temp_f, unit
    return None


def parse_time(quote: str) -> time | None:
    m = _TIME_RE.search(quote)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def parse_date(quote: str) -> date | None:
    m = _DATE_US_RE.search(quote)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = _DATE_ISO_RE.search(quote)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None

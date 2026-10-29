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
_TEMP_RE = re.compile(
    r"(?:\b(?:temperature|temp|t)\b\.?\s*:?\s*)?"
    r"(\d{2,3}(?:\.\d+)?)\s*°?\s*([cf])\b"
    r"(?:\s*\(\s*(\d{2,3}(?:\.\d+)?)\s*°?\s*([cf])\s*\))?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_US_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_ONE_DP = Decimal("0.1")


def _q1(x: Decimal) -> Decimal:
    return x.quantize(_ONE_DP, rounding=ROUND_HALF_UP)


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


def parse_temp_f(quote: str) -> tuple[Decimal, str] | None:
    """Return (temperature_f, source_unit) or None. Raises UnitConflict."""
    for m in _TEMP_RE.finditer(quote):
        value, unit = m.group(1), m.group(2).upper()
        num = Decimal(value)
        plausible = (30 <= num <= 45) if unit == "C" else (85 <= num <= 115)
        if not plausible:
            continue
        temp_f = _as_f(value, unit)
        if m.group(3):
            other = _as_f(m.group(3), m.group(4))
            if abs(other - temp_f) > Decimal("0.2"):
                raise UnitConflict(f"{quote!r}: {temp_f} F vs {other} F")
            if m.group(4).upper() == "F":
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

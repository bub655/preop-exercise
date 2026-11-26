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


# A systolic/diastolic pair. Dates (02/26/2026), fractions (6/10) and SpO2 percentages are excluded
# by the look-arounds and by the plausibility ranges applied after matching.
BP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:(?:NIBP|B/P|BP|Blood pressure|Re-?measured|Recheck(?:ed)?)\s*[:=]?\s*)?(?<![\d/.])(?P<s>\d{2,3})\s*/\s*(?P<d>\d{2,3})(?![\d/%])(?:\s*mm\s*Hg)?", re.IGNORECASE),
    re.compile(r"SBP\s*[:=]?\s*(?P<s>\d{2,3})[^\n]{0,25}?DBP\s*[:=]?\s*(?P<d>\d{2,3})", re.IGNORECASE),
]
_UNIT = r"(?:°\s*|degrees?\s+|deg\.?\s+)?(?P<unit>celsius|centigrade|fahrenheit|[cf])\b"
_DUAL = r"(?:\s*\(\s*(?P<val2>\d{2,3}(?:\.\d+)?)\s*(?:°\s*|degrees?\s+)?(?P<unit2>[cf])\s*\))?"
_LABEL = r"\b(?:temperature|temp|tmax|t)\b\.?\s*[:=]?\s*"
# A bare "92 F" is not a temperature (it could be an age/sex token). A reading needs a
# label, a degree sign or unit word, or a decimal point. A labeled reading with no unit at
# all ("T=98.6") is accepted and its unit inferred from the plausible range.
TEMP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(_LABEL + r"(?P<val>\d{2,3}(?:\.\d+)?)\s*" + _UNIT + _DUAL, re.IGNORECASE),
    re.compile(r"(?P<val>\d{2,3}\.\d+)\s*" + _UNIT + _DUAL, re.IGNORECASE),
    re.compile(r"(?P<val>\d{2,3})\s*(?:°\s*|degrees?\s+|deg\.?\s+)(?P<unit>celsius|centigrade|fahrenheit|[cf])\b" + _DUAL, re.IGNORECASE),
    re.compile(_LABEL + r"(?P<val>\d{2,3}(?:\.\d+)?)(?P<unit>)(?![\d.%/])" + _DUAL, re.IGNORECASE),  # labeled, unit-less
]


def _unit_letter(raw: str | None, value: Decimal) -> str | None:
    """Normalize a unit token to 'C'/'F'; infer from range when absent. None if implausible."""
    u = (raw or "").strip().lower()
    if u.startswith("c"):
        return "C" if 30 <= value <= 45 else None
    if u.startswith("f"):
        return "F" if 85 <= value <= 115 else None
    if 30 <= value <= 45:
        return "C"
    if 85 <= value <= 115:
        return "F"
    return None
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_US_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_MDY_RE = re.compile(rf"\b((?:{_MONTHS})[a-z]*)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_DATE_DMY_RE = re.compile(rf"\b(\d{{1,2}})\s+((?:{_MONTHS})[a-z]*)\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_MONTH_INDEX = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}

_ONE_DP = Decimal("0.1")


def q1(x: Decimal) -> Decimal:
    """Round to one decimal place, half up. The single rounding rule for temperatures."""
    return x.quantize(_ONE_DP, rounding=ROUND_HALF_UP)


_q1 = q1


def c_to_f(c: Decimal) -> Decimal:
    f = Fraction(c) * Fraction(9, 5) + 32
    return _q1(Decimal(f.numerator) / Decimal(f.denominator))


def iter_bp_matches(text: str) -> list[tuple[int, re.Match[str]]]:
    """Every plausible blood-pressure reading, in text order, without overlaps."""
    found: list[tuple[int, re.Match[str]]] = []
    for pattern in BP_PATTERNS:
        for m in pattern.finditer(text):
            s, d = int(m.group("s")), int(m.group("d"))
            if not (50 <= s <= 300 and 20 <= d <= 200):
                continue
            if any(f.start() <= m.start("s") < f.end() for _, f in found):
                continue
            found.append((m.start(), m))
    found.sort(key=lambda t: t[0])
    return found


def parse_bp(quote: str) -> tuple[int, int] | None:
    for _, m in iter_bp_matches(quote):
        return int(m.group("s")), int(m.group("d"))
    return None


def _as_f(value: str, unit: str) -> Decimal:
    v = Decimal(value)
    return c_to_f(v) if unit.upper() == "C" else _q1(v)


def iter_temp_matches(text: str) -> list[tuple[int, re.Match[str]]]:
    """Every plausible temperature reading, in text order, without overlaps."""
    found: list[tuple[int, re.Match[str]]] = []
    for pattern in TEMP_PATTERNS:
        for m in pattern.finditer(text):
            unit = _unit_letter(m.group("unit"), Decimal(m.group("val")))
            if unit is None:
                continue
            if any(f.start() <= m.start("val") < f.end() for _, f in found):
                continue
            found.append((m.start(), m))
    found.sort(key=lambda t: t[0])
    return found


def parse_temp_f(quote: str) -> tuple[Decimal, str] | None:
    """Return (temperature_f, source_unit) or None. Raises UnitConflict.

    source_unit is 'C' or 'F'; 'C?'/'F?' when the unit was inferred from the value range.
    """
    for _, m in iter_temp_matches(quote):
        value = m.group("val")
        unit = _unit_letter(m.group("unit"), Decimal(value))
        assert unit is not None
        inferred = not (m.group("unit") or "").strip()
        temp_f = _as_f(value, unit)
        if m.group("val2"):
            other = _as_f(m.group("val2"), m.group("unit2"))
            if abs(other - temp_f) > Decimal("0.2"):
                raise UnitConflict(f"{quote!r}: {temp_f} F vs {other} F")
            if m.group("unit2").upper() == "F":
                temp_f = other
        return temp_f, (unit + "?" if inferred else unit)
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
    m = _DATE_MDY_RE.search(quote)
    if m:
        try:
            return date(int(m.group(3)), _MONTH_INDEX[m.group(1)[:3].lower()], int(m.group(2)))
        except (ValueError, KeyError):
            return None
    m = _DATE_DMY_RE.search(quote)
    if m:
        try:
            return date(int(m.group(3)), _MONTH_INDEX[m.group(2)[:3].lower()], int(m.group(1)))
        except (ValueError, KeyError):
            return None
    return None

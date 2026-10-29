from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from preop_triage.parsers import (
    UnitConflict,
    c_to_f,
    parse_bp,
    parse_date,
    parse_temp_f,
    parse_time,
)


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("NIBP 189/112", (189, 112)),
        ("BP: 157/117", (157, 117)),
        ("Blood pressure 131/72 mmHg", (131, 72)),
        ("132/76 mmHg", (132, 76)),
        ("Re-measured 137/78 mmHg following 24 minutes", (137, 78)),
    ],
)
def test_bp_formats(quote: str, expected: tuple[int, int]) -> None:
    assert parse_bp(quote) == expected


def test_bp_ignores_dates_and_fractions() -> None:
    assert parse_bp("last dose 02/26/2026") is None
    assert parse_bp("PAIN: 6/10") is None
    assert parse_bp("no reading") is None


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("T 98.4 F", Decimal("98.4")),
        ("Temperature 99.1 °F (oral)", Decimal("99.1")),
        ("Temp: 97.9F", Decimal("97.9")),
        ("99.0 °F temporal", Decimal("99.0")),
    ],
)
def test_temp_fahrenheit(quote: str, expected: Decimal) -> None:
    assert parse_temp_f(quote) == (expected, "F")


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("T 38.0 C", Decimal("100.4")),
        ("37.9 °C", Decimal("100.2")),
        ("38.6 C", Decimal("101.5")),
        ("Temperature 36.6 °C (oral)", Decimal("97.9")),
        ("Temp: 38.7C", Decimal("101.7")),
    ],
)
def test_temp_celsius_boundary(quote: str, expected: Decimal) -> None:
    assert parse_temp_f(quote) == (expected, "C")


def test_temp_dual_reading_agrees() -> None:
    assert parse_temp_f("38.6 C (101.5 F)") == (Decimal("101.5"), "C")


def test_temp_dual_reading_conflict() -> None:
    with pytest.raises(UnitConflict):
        parse_temp_f("38.6 C (99.0 F)")


def test_temp_no_match() -> None:
    assert parse_temp_f("HR 74, SpO2 96% RA") is None


def test_c_to_f_exact_arithmetic() -> None:
    assert c_to_f(Decimal("38.0")) == Decimal("100.4")
    assert c_to_f(Decimal("37.95")) == Decimal("100.3")


def test_time() -> None:
    assert parse_time("ADDENDUM 08:30: Re-measured") == time(8, 30)
    assert parse_time("no time here") is None


def test_date() -> None:
    assert parse_date("last dose 03/17/2026") == date(2026, 3, 17)
    assert parse_date("Date of service: 2026-03-17") == date(2026, 3, 17)
    assert parse_date("on 3/17/26") is None
    assert parse_date("nothing") is None

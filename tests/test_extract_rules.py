from __future__ import annotations

from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.ground import ground_spans
from preop_triage.model import SpanKind
from preop_triage.policy import load_policy

POLICY = load_policy()
NOTE = """PRE-OP NURSING ASSESSMENT
Visit date: 03/15/2026
Planned procedure: inguinal hernia repair

VITAL SIGNS:
  08:06  NIBP 189/112, T 37.1 C, HR 74, SpO2 96% RA. See addendum.

MEDICATION RECONCILIATION (patient-reported):
  - ASA 81 mg PO daily - taking as prescribed
  - Patient also reports taking Eliquis 5 mg (BID), prescribed by an outside cardiologist; not on the reconciled medication list - flagged for provider review.

RECOMMENDATIONS:
  1. Hold Xarelto for the 2 days before surgery (last dose 02/26/2026; no doses in the 48 hours prior). Resume Xarelto 24-48 hours after surgery once hemostasis is secure.
H&P: not yet on file for this surgical episode; primary care office contacted to complete.
ADDENDUM 08:30: Re-measured 137/78 mmHg following 24 minutes in a quiet room.
"""
FLOWSHEET = "PRE-OP NURSING ASSESSMENT\nVITAL SIGNS: See flowsheet.\n"


def _kinds(spans, kind):
    return [s for s in spans if s.kind == kind]


def test_finds_both_bp_spans() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "Nursing Pre-procedure Assessment", NOTE)
    quotes = [s.quote for s in _kinds(spans, SpanKind.VITAL_BP)]
    assert any("189/112" in q for q in quotes)
    assert any("137/78" in q for q in quotes)
    assert not any("02/26/2026" in q for q in quotes)


def test_finds_temp_span() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    assert any("37.1 C" in s.quote for s in _kinds(spans, SpanKind.VITAL_TEMP))


def test_finds_vitals_deferred() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d2", "x", FLOWSHEET)
    assert [s.quote for s in _kinds(spans, SpanKind.VITALS_DEFERRED)] == ["VITAL SIGNS: See flowsheet"]


def test_finds_med_mention() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    quotes = [s.quote for s in _kinds(spans, SpanKind.MED_MENTION)]
    assert any("reports taking Eliquis" in q for q in quotes)


def test_finds_hold_and_resume() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    assert any("Hold Xarelto for the 2 days before surgery" in s.quote for s in _kinds(spans, SpanKind.ANTICOAG_HOLD))
    assert any("Resume Xarelto 24-48 hours after surgery" in s.quote for s in _kinds(spans, SpanKind.ANTICOAG_RESUME))


def test_finds_doc_absence_and_planned_procedure() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    assert any(s.quote.startswith("H&P: not yet on file") for s in _kinds(spans, SpanKind.DOC_ABSENCE))
    assert [s.quote for s in _kinds(spans, SpanKind.PLANNED_PROCEDURE)] == ["Planned procedure: inguinal hernia repair"]


def test_empty_text_no_spans() -> None:
    assert RuleSpanExtractor(POLICY).extract("d1", "x", "") == []
    assert RuleSpanExtractor(POLICY).extract("d1", "x", None) == []


def test_all_spans_ground_and_have_offsets() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    assert spans
    kept, rejected = ground_spans(spans, {"d1": NOTE})
    assert rejected == []
    assert all(s.offset is not None and NOTE[s.offset:].startswith(s.quote) for s in kept)
    assert all(s.origin == "rules" for s in spans)


def test_spans_are_sorted_by_offset_then_kind() -> None:
    spans = RuleSpanExtractor(POLICY).extract("d1", "x", NOTE)
    keys = [(s.offset, s.kind.value) for s in spans]
    assert keys == sorted(keys)

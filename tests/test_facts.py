from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from preop_triage.facts import spans_to_facts as _spans_to_facts
from preop_triage.model import Doc, Span, SpanKind
from preop_triage.policy import load_policy

POLICY = load_policy()
TEXT = "VITAL SIGNS:\n  08:06  NIBP 189/112, T 37.1 C, HR 74. See addendum.\nADDENDUM 08:30: Re-measured 137/78 mmHg.\nHold Xarelto for the 2 days before surgery (last dose 02/26/2026).\nTemperature 38.6 C (99.0 F)\n"
DOCS = {"d1": Doc("d1", "Nursing Pre-procedure Assessment", "2026-03-15", "RN", TEXT, 0)}


def spans_to_facts(spans, docs, policy):
    """Span facts only; DOC_KIND facts are covered by test_doc_kind_facts_from_docs."""
    facts, flags = _spans_to_facts(spans, docs, policy)
    return [f for f in facts if f.kind != "DOC_KIND"], flags


def _span(kind: SpanKind, quote: str, model_label: str | None = None) -> Span:
    return Span(kind=kind, doc_id="d1", quote=quote, offset=TEXT.index(quote), model_label=model_label)


def test_bp_span_timestamp_uses_doc_date_and_nearest_preceding_time() -> None:
    facts, flags = spans_to_facts([_span(SpanKind.VITAL_BP, "NIBP 189/112"), _span(SpanKind.VITAL_BP, "Re-measured 137/78 mmHg")], DOCS, POLICY)
    assert flags == []
    assert [(f.value, f.observed_at) for f in facts] == [((189, 112), datetime(2026, 3, 15, 8, 6)), ((137, 78), datetime(2026, 3, 15, 8, 30))]
    assert facts[0].doc_id == "d1" and facts[0].quote == "NIBP 189/112" and facts[0].parser == "text_bp"


def test_temp_celsius_converted_with_converted_from() -> None:
    facts, _ = spans_to_facts([_span(SpanKind.VITAL_TEMP, "T 37.1 C")], DOCS, POLICY)
    assert facts[0].kind == "TEMP" and facts[0].value == Decimal("98.8") and facts[0].meta["converted_from"] == "37.1 C"


def test_unparseable_grounded_span_becomes_flag() -> None:
    facts, flags = spans_to_facts([_span(SpanKind.VITAL_TEMP, "Temperature 38.6 C (99.0 F)")], DOCS, POLICY)
    assert facts == []
    assert flags[0].reason == "UNIT_CONFLICT" and flags[0].rule_id == "R4"


def test_hold_span_becomes_fact_with_drug() -> None:
    facts, flags = spans_to_facts([_span(SpanKind.ANTICOAG_HOLD, "Hold Xarelto for the 2 days before surgery (last dose 02/26/2026).")], DOCS, POLICY)
    assert flags == []
    assert facts[0].kind == "ANTICOAG_HOLD" and facts[0].value == {"canonical": "rivaroxaban", "label": "SPECIFIC"}
    assert facts[0].observed_at == datetime(2026, 3, 15)


def test_classification_flag_carries_rule_id() -> None:
    _, flags = spans_to_facts([_span(SpanKind.ANTICOAG_HOLD, "Hold Xarelto for the 2 days before surgery (last dose 02/26/2026).", model_label="VAGUE")], DOCS, POLICY)
    assert flags[0].reason == "MODEL_CODE_DISAGREE" and flags[0].rule_id == "R3"


def test_doc_kind_facts_from_docs() -> None:
    docs = {
        "h": Doc("h", "History and Pyhsical", "2026-02-10", "A", "PREOPERATIVE HISTORY AND PHYSICAL\nPlanned procedure: x", 0),
        "w": Doc("w", "Admission H&P", "2025-06-22", "B", "Encounter: annual wellness / establish care", 1),
        "c": Doc("c", "Surgical Consent", "2026-02-25", "C", "Procedure: thyroidectomy\nPatient signature on file - signed in clinic", 2),
    }
    facts, _ = _spans_to_facts([], docs, POLICY)
    kinds = {f.doc_id: f.value for f in facts if f.kind == "DOC_KIND"}
    assert kinds == {"h": "PREOP_HP", "w": "NON_QUALIFYING_HP", "c": "CONSENT"}

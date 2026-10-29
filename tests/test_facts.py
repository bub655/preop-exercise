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
    assert facts[0].doc_id == "d1" and facts[0].quote == "08:06  NIBP 189/112, T 37.1 C, HR 74. See addendum." and facts[0].parser == "text_bp"


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
    assert facts[0].kind == "ANTICOAG_HOLD" and facts[0].value["canonical"] == "rivaroxaban" and facts[0].value["label"] == "SPECIFIC"
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


def test_value_span_evidence_is_normalized_to_its_line() -> None:
    """Two quote boundaries for the same reading must produce identical evidence (BP and temperature only)."""
    narrow = [_span(SpanKind.VITAL_BP, "NIBP 189/112")]
    wide = [Span(kind=SpanKind.VITAL_BP, doc_id="d1", quote="08:06  NIBP 189/112, T 37.1 C", offset=TEXT.index("08:06"), origin="claude")]
    fa, _ = spans_to_facts(narrow, DOCS, POLICY)
    fb, _ = spans_to_facts(wide, DOCS, POLICY)
    assert (fa[0].value, fa[0].observed_at, fa[0].quote) == (fb[0].value, fb[0].observed_at, fb[0].quote)
    assert fa[0].quote == "08:06  NIBP 189/112, T 37.1 C, HR 74. See addendum."
    ta, _ = spans_to_facts([_span(SpanKind.VITAL_TEMP, "T 37.1 C")], DOCS, POLICY)
    tb, _ = spans_to_facts([Span(kind=SpanKind.VITAL_TEMP, doc_id="d1", quote="NIBP 189/112, T 37.1 C", offset=TEXT.index("NIBP"), origin="claude")], DOCS, POLICY)
    assert (ta[0].value, ta[0].observed_at, ta[0].quote) == (tb[0].value, tb[0].observed_at, tb[0].quote)


def test_non_value_span_quotes_are_not_normalized() -> None:
    facts, _ = spans_to_facts([_span(SpanKind.ANTICOAG_HOLD, "Hold Xarelto for the 2 days before surgery (last dose 02/26/2026).")], DOCS, POLICY)
    assert facts[0].quote == "Hold Xarelto for the 2 days before surgery (last dose 02/26/2026)."


def test_two_readings_on_one_line_keep_their_own_times_and_values() -> None:
    text = "On arrival at 08:02: BP 190/100. Rechecked at 08:35: BP 128/76.\n"
    docs = {"n": Doc("n", "Nursing", "2026-03-02", "RN", text, 0)}
    from preop_triage.extract.rules import RuleSpanExtractor
    from preop_triage.ground import ground_spans

    kept, _ = ground_spans(RuleSpanExtractor(POLICY).extract("n", None, text), {"n": text})
    facts, _ = spans_to_facts(kept, docs, POLICY)
    bps = sorted([(f.observed_at, f.value) for f in facts if f.kind == "BP"])
    assert bps == [(datetime(2026, 3, 2, 8, 2), (190, 100)), (datetime(2026, 3, 2, 8, 35), (128, 76))]

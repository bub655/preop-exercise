"""Regression tests for the whole-branch review findings (C1, C2, I1-I7)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from core import triage_submission
from preop_triage.classify import classify, procedure_matches
from preop_triage.engine import evaluate
from preop_triage.extract.claude import ClaudeSpanExtractor
from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.extract.union import UnionSpanExtractor
from preop_triage.facts import spans_to_facts
from preop_triage.ground import ground_spans
from preop_triage.ingest import ingest
from preop_triage.invariants import check_invariants
from preop_triage.model import Check, Doc, Evidence, Fact, Issue, Span, SpanKind, TriageOutput
from preop_triage.parsers import parse_temp_f
from preop_triage.pipeline import run
from preop_triage.policy import load_policy
from tests.test_engine import F, _med, base_facts, cats

ROOT = Path(__file__).resolve().parent.parent
CASES = [json.loads(l) for l in (ROOT / "data" / "patients_sample_50.jsonl").read_text().splitlines() if l.strip()]
POLICY = load_policy()
RULES = RuleSpanExtractor(POLICY)


class FailingMessages:
    def create(self, **kwargs):
        raise anthropic.APIConnectionError(request=SimpleNamespace())


class QuotingMessages:
    """A fake model that quotes whatever lines we tell it to, with the kinds we say."""

    def __init__(self, spans_by_substring: dict[str, tuple[str, str | None]]) -> None:
        self.spans_by_substring = spans_by_substring

    def create(self, **kwargs):
        text = kwargs["messages"][0]["content"]
        spans = []
        for needle, (kind, label) in self.spans_by_substring.items():
            for line in text.split("\n"):
                if needle in line:
                    spans.append({"kind": kind, "quote": line.strip(), "label": label})
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"spans": spans}))])


# --- C1: extractor failure must fail closed -----------------------------------


def test_c1_claude_failure_is_a_blocking_flag_not_silent() -> None:
    sub = CASES[0]["submission"]
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=SimpleNamespace(messages=FailingMessages()))
    out = run(sub, policy=POLICY, extractor=ext)
    assert out.decision == "NEEDS_FOLLOW_UP"
    assert any(f.reason == "EXTRACTOR_ERROR" for f in out.review_flags)
    assert "MISSING_REQUIRED_DATA" in {i.category for i in out.issues}


def test_c1_union_with_failing_claude_still_flags() -> None:
    sub = CASES[0]["submission"]
    claude = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=SimpleNamespace(messages=FailingMessages()))
    out = run(sub, policy=POLICY, extractor=UnionSpanExtractor(RULES, claude))
    assert out.decision != "READY" and any(f.reason == "EXTRACTOR_ERROR" for f in out.review_flags)


# --- C2: consent and plan patterns must not fail open -------------------------


@pytest.mark.parametrize(
    "quote",
    [
        "Signed by patient: ______________________   Date: __________",
        "The form has not been signed by the patient yet.",
        "Consent form to be signed by the patient on the day of surgery.",
    ],
)
def test_c2_blank_or_negated_signature_is_not_signed(quote: str) -> None:
    label, flag = classify(Span(SpanKind.CONSENT_SIGNATURE, "d", quote, 0), POLICY)
    assert label != "SIGNED"
    assert label == "UNSIGNED" or flag is not None


def test_c2_resume_timing_must_be_in_the_same_sentence() -> None:
    quote = "Resume Xarelto when cleared by the surgeon; timing to be determined. Ice to wrist for 48 hours."
    label, _ = classify(Span(SpanKind.ANTICOAG_RESUME, "d", quote, 0), POLICY)
    assert label != "SPECIFIC"


def test_c2_hold_timing_must_be_in_the_same_sentence() -> None:
    quote = "Hold warfarin per cardiology. Ice to wrist for 2 days before surgery."
    label, _ = classify(Span(SpanKind.ANTICOAG_HOLD, "d", quote, 0), POLICY)
    assert label != "SPECIFIC"


# --- I1: recency inside a document --------------------------------------------


def test_i1_ground_keeps_extractor_offset_for_duplicate_quotes() -> None:
    text = "08:00  BP 128/76\n09:00  BP 186/104\n10:00  BP 128/76\n"
    second = text.rfind("BP 128/76")
    kept, rejected = ground_spans([Span(SpanKind.VITAL_BP, "d", "BP 128/76", second, origin="claude")], {"d": text})
    assert rejected == [] and kept[0].offset == second


def test_i1_duplicate_reading_gets_its_own_time_and_latest_wins() -> None:
    text = "08:00  BP 128/76\n09:00  BP 186/104\n10:00  BP 128/76\n"
    docs = {"d": Doc("d", "Nursing", "2026-03-02", "RN", text, 0)}
    spans = RULES.extract("d", "Nursing", text)
    kept, _ = ground_spans(spans, {"d": text})
    facts, _ = spans_to_facts(kept, docs, POLICY)
    times = sorted(f.observed_at for f in facts if f.kind == "BP")
    assert times == [datetime(2026, 3, 2, 8, 0), datetime(2026, 3, 2, 9, 0), datetime(2026, 3, 2, 10, 0)]


def test_i1_following_time_on_same_line_is_used() -> None:
    text = "Repeat BP 128/76 at 09:05 after rest.\n"
    docs = {"d": Doc("d", "Nursing", "2026-03-02", "RN", text, 0)}
    kept, _ = ground_spans(RULES.extract("d", None, text), {"d": text})
    facts, _ = spans_to_facts(kept, docs, POLICY)
    assert [f.observed_at for f in facts if f.kind == "BP"] == [datetime(2026, 3, 2, 9, 5)]


def test_i1_same_timestamp_text_readings_later_in_text_wins() -> None:
    when = datetime(2026, 3, 3)
    first = F("BP", (188, 112), when, "documents:n", doc_id="n", quote="BP 188/112 on arrival", parser="text_bp", meta={"offset": 10})
    later = F("BP", (128, 76), when, "documents:n", doc_id="n", quote="Repeat BP 128/76", parser="text_bp", meta={"offset": 80})
    res = evaluate(base_facts(bp_at=datetime(2026, 3, 1), extra=[first, later]), [], POLICY)
    assert res.decision == "READY"


# --- I2: bare-unit temperature -------------------------------------------------


def test_i2_bare_number_with_unit_letter_is_not_a_temperature() -> None:
    assert parse_temp_f("Patient: 92 F, ambulatory") is None
    assert parse_temp_f("72 M presenting for evaluation") is None
    assert parse_temp_f("T 92 F") == (Decimal("92.0"), "F")
    assert parse_temp_f("99.0 °F temporal") == (Decimal("99.0"), "F")
    assert parse_temp_f("he was running 38.6 C (101.5 F)") == (Decimal("101.5"), "C")


# --- I3: nothing raises; unknown risk is missing data --------------------------


def test_i3_lowercase_risk_is_normalized_and_unknown_risk_is_missing_data() -> None:
    sub = json.loads(json.dumps(CASES[0]["submission"]))
    sub["procedure"]["procedure_risk"] = "low"
    assert triage_submission(sub, model="m").decision == triage_submission(CASES[0]["submission"], model="m").decision
    sub["procedure"]["procedure_risk"] = "MEDIUM"
    out = triage_submission(sub, model="m")
    assert out.decision == "NEEDS_FOLLOW_UP" and any(i.status == "PROCEDURE_RISK_UNKNOWN" for i in out.issues)


def test_i3_nan_temperature_is_flagged_not_crash() -> None:
    sub = json.loads(json.dumps(CASES[0]["submission"]))
    sub["vitals"].append({"type": "temperature", "value_f": float("nan"), "date": "2026-03-01T10:00:00Z"})
    facts, flags = ingest(sub, POLICY)
    assert any(f.reason == "STRUCTURED_VITAL_INCOMPLETE" for f in flags)
    out = run(sub, policy=POLICY, extractor=RULES)
    assert out.decision == "NEEDS_FOLLOW_UP"


def test_i3_internal_exception_becomes_system_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import preop_triage.pipeline as pipeline_mod

    def boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(pipeline_mod, "evaluate", boom)
    out = run(CASES[0]["submission"], policy=POLICY, extractor=RULES)
    assert out.decision == "NEEDS_FOLLOW_UP" and any(f.reason == "SYSTEM_ERROR" for f in out.review_flags)


def test_i3_malformed_submission_does_not_raise() -> None:
    out = triage_submission({"procedure": {"procedure_risk": 42, "procedure_date": "soon"}, "vitals": "nope"}, model="m")
    assert out.decision == "NEEDS_FOLLOW_UP"


# --- I4: invariant gate strength ----------------------------------------------

DOCS = {"d1": "BP 196/110 at the pre-op clinic.\nPatient signature: pending"}


def _iss(category: str, rule: str, status: str = "X", quote: str | None = "BP 196/110", source: str = "documents:d1", doc_id: str | None = "d1") -> Issue:
    return Issue(category=category, rule_id=rule, status=status, summary="s", evidence=[Evidence(source=source, doc_id=doc_id, quote=quote)])


def test_i4_ready_with_unverified_check_is_system_error() -> None:
    out = TriageOutput(decision="READY", checks=[Check(rule_id="R1.2", status="UNVERIFIED")])
    assert check_invariants(out, DOCS, {}, POLICY).decision == "NEEDS_FOLLOW_UP"


def test_i4_ready_with_skipped_testing_is_system_error_but_skipped_r3_is_fine() -> None:
    bad = TriageOutput(decision="READY", checks=[Check(rule_id="R2", status="SKIPPED")])
    assert check_invariants(bad, DOCS, {}, POLICY).decision == "NEEDS_FOLLOW_UP"
    ok = TriageOutput(decision="READY", checks=[Check(rule_id="R3", status="SKIPPED"), Check(rule_id="R2", status="PASS")])
    assert check_invariants(ok, DOCS, {}, POLICY).decision == "READY"


def test_i4_unverified_issue_with_wrong_category_is_system_error() -> None:
    out = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_iss("REQUIRED_TESTING", "R1", "UNVERIFIED", "Patient signature: pending")])
    assert check_invariants(out, DOCS, {}, POLICY).review_flags[-1].reason == "SYSTEM_ERROR"


def test_i4_not_cleared_needs_an_exclusion_level_structured_vital() -> None:
    sub = {"vitals": [{"type": "blood_pressure", "systolic": 120, "diastolic": 70, "date": "2026-03-24T09:00:00Z"}]}
    out = TriageOutput(decision="NOT_CLEARED", issues=[_iss("ACUTE_SAFETY_EXCLUSION", "R4", "BP_EXCLUSION", None, "vitals[0]", None)])
    assert check_invariants(out, DOCS, sub, POLICY).decision == "NEEDS_FOLLOW_UP"
    sub["vitals"][0]["diastolic"] = 110
    assert check_invariants(out, DOCS, sub, POLICY).decision == "NOT_CLEARED"


def test_i4_not_cleared_needs_an_exclusion_level_quote() -> None:
    docs = {"d1": "BP 120/70 at the pre-op clinic."}
    out = TriageOutput(decision="NOT_CLEARED", issues=[_iss("ACUTE_SAFETY_EXCLUSION", "R4", "BP_EXCLUSION", "BP 120/70")])
    assert check_invariants(out, docs, {}, POLICY).decision == "NEEDS_FOLLOW_UP"


def test_i4_structured_evidence_quote_must_match_the_field() -> None:
    sub = {"medications": [{"name": "apixaban 5 mg PO BID", "active": True}]}
    good = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_iss("ANTICOAGULATION_MANAGEMENT", "R3", "NO_PLAN", "apixaban 5 mg PO BID", "medications[0]", None)])
    assert not any(f.reason == "SYSTEM_ERROR" for f in check_invariants(good, DOCS, sub, POLICY).review_flags)
    bad = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_iss("ANTICOAGULATION_MANAGEMENT", "R3", "NO_PLAN", "warfarin 5 mg", "medications[0]", None)])
    assert check_invariants(bad, DOCS, sub, POLICY).review_flags[-1].reason == "SYSTEM_ERROR"


# --- I5: model spans the tables do not cover are advisory, not scored ---------


def test_i5_model_only_unmatched_span_is_advisory() -> None:
    doc = Doc("d", "Consent", "2026-03-01", "MD", "Electronically signed by Amanda Wong, DO on 03/13/2026.", 0)
    span = Span(SpanKind.CONSENT_SIGNATURE, "d", "Electronically signed by Amanda Wong, DO on 03/13/2026.", 0, None, origin="claude")
    _, flags = spans_to_facts([span], {"d": doc}, POLICY)
    assert flags and flags[0].reason == "NO_PATTERN_MATCH" and flags[0].blocking is False


def test_i5_advisory_flags_do_not_change_decision_or_issues() -> None:
    from preop_triage.model import ReviewFlag

    res = evaluate(base_facts(), [ReviewFlag("NO_PATTERN_MATCH", "d", "x", None, None, "R1", blocking=False)], POLICY)
    assert res.decision == "READY" and res.issues == [] and len(res.review_flags) == 1


def test_i5_union_with_noisy_model_matches_rules_decision_on_sample() -> None:
    noisy = QuotingMessages({"Electronically signed by": ("CONSENT_SIGNATURE", None), "atorvastatin": ("MED_MENTION", "TAKING"), "HR ": ("VITAL_BP", None)})
    claude = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=SimpleNamespace(messages=noisy))
    union = UnionSpanExtractor(RULES, claude)
    for c in CASES[:10]:
        a = run(c["submission"], policy=POLICY, extractor=RULES)
        b = run(c["submission"], policy=POLICY, extractor=union)
        assert (a.decision, sorted({i.category for i in a.issues})) == (b.decision, sorted({i.category for i in b.issues})), c["case_id"]


def test_i5_model_disagreement_with_code_still_blocks() -> None:
    doc = Doc("d", "Consent", "2026-03-01", "MD", "Patient signature: pending", 0)
    span = Span(SpanKind.CONSENT_SIGNATURE, "d", "Patient signature: pending", 0, "SIGNED", origin="claude")
    _, flags = spans_to_facts([span], {"d": doc}, POLICY)
    assert flags[0].reason == "MODEL_CODE_DISAGREE" and flags[0].blocking is True


# --- I6: consistent rounding ---------------------------------------------------


def test_i6_structured_temperature_rounds_half_up() -> None:
    sub = {"procedure": {}, "vitals": [{"type": "temperature", "value_f": 100.45, "date": "2026-03-01T10:00:00Z"}]}
    facts, _ = ingest(sub, POLICY)
    assert [f.value for f in facts if f.kind == "TEMP"] == [Decimal("100.5")]


# --- I7: laterality ------------------------------------------------------------


def test_i7_wrong_side_consent_is_a_mismatch() -> None:
    assert procedure_matches("Elective right carpal tunnel release", "left carpal tunnel release", POLICY) is False
    assert procedure_matches("Elective carpal tunnel release", "left carpal tunnel release", POLICY) is True
    assert procedure_matches("Elective bilateral knee arthroscopy", "bilateral knee arthroscopy", POLICY) is True

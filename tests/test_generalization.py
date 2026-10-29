"""Generalization regressions: phrasings and structures deliberately absent from the 50 sample cases."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from preop_triage.classify import classify, classify_doc_kind, procedure_matches
from preop_triage.engine import evaluate
from preop_triage.facts import spans_to_facts
from preop_triage.ground import ground_spans
from preop_triage.ingest import ingest
from preop_triage.model import Doc, Span, SpanKind
from preop_triage.parsers import parse_bp, parse_date, parse_temp_f
from preop_triage.policy import load_policy
from preop_triage.extract.rules import RuleSpanExtractor
from tests.test_engine import F, _med, base_facts, cats

POLICY = load_policy()
RULES = RuleSpanExtractor(POLICY)


def _label(kind: SpanKind, quote: str) -> str | None:
    return classify(Span(kind, "d", quote, 0), POLICY)[0]


# --- structured ingestion ---------------------------------------------------------


def test_vital_type_aliases_and_value_c() -> None:
    sub = {"procedure": {}, "vitals": [
        {"type": "bp", "systolic": 180, "diastolic": 80, "date": "2026-05-09T09:05:00Z"},
        {"type": "BP", "systolic": 120, "diastolic": 70, "date": "2026-05-01T09:05:00Z"},
        {"type": "temp", "value_c": 38.6, "date": "2026-05-07T09:05:00Z"},
        {"type": "Temperature", "value": 99.1, "unit": "F", "date": "2026-05-06T09:05:00Z"},
        {"type": "heart_rate", "value": 72, "date": "2026-05-06T09:05:00Z"},
    ]}
    facts, flags = ingest(sub, POLICY)
    assert [f.value for f in facts if f.kind == "BP"] == [(180, 80), (120, 70)]
    assert [f.value for f in facts if f.kind == "TEMP"] == [Decimal("101.5"), Decimal("99.1")]
    assert flags == []


def test_unknown_vital_type_is_flagged_not_dropped() -> None:
    sub = {"procedure": {}, "vitals": [{"type": "blood pressur", "systolic": 190, "diastolic": 100, "date": "2026-05-09T09:05:00Z"}]}
    _, flags = ingest(sub, POLICY)
    assert any(f.reason == "UNKNOWN_VITAL_TYPE" and f.rule_id == "R4" for f in flags)


@pytest.mark.parametrize("code,display,expected", [
    ("CBC W DIFF", None, "CBC"), ("cbc", None, "CBC"), ("85025", "CBC with differential", "CBC"), ("XYZ", "Complete Blood Count", "CBC"),
    ("CMP14", None, "CMP"), ("Comprehensive Metabolic Panel", None, "CMP"), ("80053", None, "CMP"), ("Basic Metabolic Panel", None, "BMP"), ("80048", "BMP", "BMP"),
    ("TSH", "Thyroid stimulating hormone", "TSH"),
])
def test_lab_code_normalization(code: str, display: str | None, expected: str) -> None:
    assert POLICY.lab_codes.normalize(code, display) == expected


def test_lab_date_only_effective_at() -> None:
    sub = {"procedure": {}, "labs": [{"code": "CBC", "effective_at": "2026-03-01", "status": "final"}]}
    facts, _ = ingest(sub, POLICY)
    assert [f.observed_at for f in facts if f.kind == "LAB"] == [datetime(2026, 3, 1)]


# --- parsers ------------------------------------------------------------------------


@pytest.mark.parametrize("quote,expected", [
    ("BP=150/95", (150, 95)), ("B/P 150/95", (150, 95)), ("BP 150 / 95", (150, 95)), ("SBP 182, DBP 96", (182, 96)), ("Blood pressure: 150/95", (150, 95)),
])
def test_bp_formats_generalize(quote: str, expected: tuple[int, int]) -> None:
    assert parse_bp(quote) == expected


@pytest.mark.parametrize("quote,expected,unit", [
    ("T=98.6", Decimal("98.6"), "F?"), ("Tmax 101.2 F", Decimal("101.2"), "F"), ("Temperature: 100.9 degrees F", Decimal("100.9"), "F"),
    ("Temperature: 38.0 degrees Celsius", Decimal("100.4"), "C"), ("T 37.2", Decimal("99.0"), "C?"), ("temp 38.6C", Decimal("101.5"), "C"), ("99.1 °F tympanic", Decimal("99.1"), "F"),
])
def test_temp_formats_generalize(quote: str, expected: Decimal, unit: str) -> None:
    assert parse_temp_f(quote) == (expected, unit)


def test_dates_with_month_names() -> None:
    assert parse_date("scheduled for March 17, 2026") == date(2026, 3, 17)
    assert parse_date("last dose 17 Mar 2026") == date(2026, 3, 17)


# --- document kinds -----------------------------------------------------------------


@pytest.mark.parametrize("title", ["Hist and Physical", "History/Physical", "H & P (pre-surgical)", "Preoperative History & Physical", "History and Physical Exam"])
def test_hp_titles_generalize(title: str) -> None:
    assert classify_doc_kind(title, "HISTORY AND PHYSICAL\nPre-op evaluation requested by surgery ahead of cholecystectomy.", POLICY) == "PREOP_HP"


@pytest.mark.parametrize("body", ["PRE-OPERATIVE EVALUATION for mastectomy", "Preoperative risk assessment prior to septoplasty.", "Surgery planned: thyroidectomy on 03/17/2026.", "Scheduled procedure: knee arthroscopy (03/17/2026)."])
def test_hp_body_markers_generalize(body: str) -> None:
    assert classify_doc_kind("H&P", f"HISTORY AND PHYSICAL\n{body}", POLICY) == "PREOP_HP"


@pytest.mark.parametrize("body", ["Encounter type: annual physical / health maintenance.", "Reason for visit: new patient, establish care.", "Routine wellness visit. No surgical planning discussed."])
def test_wellness_markers_generalize(body: str) -> None:
    assert classify_doc_kind("Annual H&P", f"HISTORY AND PHYSICAL\n{body}", POLICY) == "NON_QUALIFYING_HP"


# --- consent signatures ---------------------------------------------------------------


@pytest.mark.parametrize("quote", [
    "Patient signed the consent form on 03/12/2026 at 09:14.", "Consent signed by Ana Torres (patient) on 03/12/2026; witnessed by RN.", "Signature: Ana Torres (patient), 03/12/2026 09:14",
    "Patient signature obtained 03/12/2026.", "Written informed consent was obtained and signed by the patient on 03/12/2026.", "Patient has signed and dated the consent (03/12/2026).",
    "Patient's signature on file (e-signed 03/12/2026).", "Consent executed by patient 03/12/2026; copy scanned to chart.",
])
def test_signed_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.CONSENT_SIGNATURE, quote) == "SIGNED"


@pytest.mark.parametrize("quote", [
    "Patient declined to sign at this time; will revisit on day of surgery.", "Signature pending - patient reviewing with family.", "Awaiting patient signature.", "Consent reviewed; form not yet signed.",
    "Patient will sign on the morning of surgery.", "Form sent via patient portal; awaiting completion.", "Patient has not signed the consent.", "Verbal consent only; written consent to be obtained pre-op.",
    "Consent discussion only. No signature obtained.", "Signed by patient: ______________", "The form has not been signed by the patient yet.",
])
def test_unsigned_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.CONSENT_SIGNATURE, quote) == "UNSIGNED"


def test_physician_attestation_still_not_a_patient_signature() -> None:
    assert _label(SpanKind.CONSENT_SIGNATURE, "Physician attestation: risks explained. Electronically signed by R. Patel, MD on 03/12/2026.") is None


# --- anticoagulation plans ----------------------------------------------------------


@pytest.mark.parametrize("quote", [
    "Discontinue apixaban 48 hours prior to surgery.", "Stop Xarelto 3 days pre-op; last dose 03/14/2026.", "Eliquis to be held x3 days before the procedure.", "Last dose of warfarin on 03/12/2026; no doses thereafter until cleared.",
    "Withhold Pradaxa beginning 2 days prior to surgery.", "Patient instructed to stop apixaban 3 days before surgery.", "dabigatran should be held for 48 hours preoperatively.",
])
def test_hold_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.ANTICOAG_HOLD, quote) == "SPECIFIC"


@pytest.mark.parametrize("quote", [
    "Restart apixaban on POD1 if no bleeding.", "May resume Xarelto 48 hours postoperatively.", "Reinitiate warfarin the evening of surgery.", "Resume Eliquis on post-op day 2 at the home dose.",
    "Pradaxa can be restarted the morning after surgery.", "Anticoagulation to be resumed 48 hours post-op.", "Resume home dose of apixaban the day after surgery.", "Restart Xarelto 72h after the procedure pending surgical clearance.",
])
def test_resume_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.ANTICOAG_RESUME, quote) == "SPECIFIC"


@pytest.mark.parametrize("quote", [
    "Perioperative management of apixaban per the surgical team.", "Defer to surgery regarding interruption of Xarelto.", "Anticoagulation plan TBD; to be discussed with the surgeon.",
    "Will provide specific hold/resume recommendations at a later date.", "Timing of Eliquis interruption to be determined by the operating surgeon.", "Resumption of warfarin will be addressed postoperatively.",
])
def test_deferred_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.ANTICOAG_DEFERRED, quote) == "DEFERRED"


def test_drugless_resume_pairs_with_hold_in_same_document() -> None:
    text = "HPI: maintained on Eliquis 5 mg BID for atrial fibrillation.\nRECOMMENDATIONS:\n1. Hold Eliquis for 2 days before surgery (last dose 03/14/2026).\n2. Anticoagulation to be resumed 48 hours post-op.\n"
    docs = {"p": Doc("p", "Cardiology Consultation", "2026-03-05", "MD", text, 0)}
    kept, _ = ground_spans(RULES.extract("p", None, text), {"p": text})
    tf, _ = spans_to_facts(kept, docs, POLICY)
    facts = base_facts(extra=[_med("Eliquis 5 mg BID", "apixaban", "anticoagulant", True), F("DOC", {"type": "Cardiology Consultation", "date": "2026-03-05", "author": "MD", "index": 5}, datetime(2026, 3, 5), "documents[5]", doc_id="p")] + [f for f in tf if f.kind != "DOC_KIND"])
    res = evaluate(facts, [], POLICY)
    assert res.decision == "READY" and any("names no drug" in a.text for a in res.assumptions)


# --- medication mentions --------------------------------------------------------------


@pytest.mark.parametrize("quote", ["Pt reports taking Eliquis 5 mg BID, prescribed by outside cardiology", "Patient states he takes rivaroxaban 20 mg daily at home.", "Home medications include Pradaxa 150 mg BID", "Currently anticoagulated with apixaban 5 mg BID.", "On chronic Coumadin therapy per patient."])
def test_taking_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.MED_MENTION, quote) == "TAKING"


@pytest.mark.parametrize("quote", ["Xarelto was discontinued by cardiology in 2021", "Off warfarin since 2020 after a provoked DVT course.", "No longer taking Eliquis.", "apixaban stopped last year", "switched from Coumadin"])
def test_not_taking_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.MED_MENTION, quote) == "NOT_TAKING"


@pytest.mark.parametrize("quote", ["Unsure whether still taking Xarelto; pharmacy records requested.", "Patient unable to confirm if apixaban is still being taken.", "Coumadin: compliance uncertain; patient could not confirm current use."])
def test_unknown_phrasings_generalize(quote: str) -> None:
    assert _label(SpanKind.MED_MENTION, quote) == "UNKNOWN"


def test_patient_reported_novel_phrasing_blocks_ready() -> None:
    text = "MEDICATION RECONCILIATION:\n  - lisinopril 10 mg daily - taking as prescribed\n  - Patient states he takes Eliquis 5 mg BID daily at home.\n"
    docs = {"n": Doc("n", "Pre-op Nursing Intake", "2026-03-02", "RN", text, 0)}
    kept, _ = ground_spans(RULES.extract("n", None, text), {"n": text})
    tf, _ = spans_to_facts(kept, docs, POLICY)
    res = evaluate(base_facts(extra=[f for f in tf if f.kind != "DOC_KIND"]), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"]


# --- procedures -----------------------------------------------------------------------


@pytest.mark.parametrize("scheduled,consent,expected", [
    ("Elective laparoscopic cholecystectomy", "laparoscopic cholecystectomy", True),
    ("Elective Whipple procedure", "pancreaticoduodenectomy (Whipple)", True),
    ("Elective hysterectomy", "total abdominal hysterectomy", True),
    ("Elective mastectomy", "laparoscopic cholecystectomy", False),
    ("Elective left shoulder arthroscopy", "right shoulder arthroscopy", False),
    ("Elective septoplasty", "septoplasty", True),
])
def test_procedure_matching_generalizes(scheduled: str, consent: str, expected: bool) -> None:
    assert procedure_matches(scheduled, consent, POLICY) is expected


# --- vitals in notes ----------------------------------------------------------------------


def test_vitals_deferred_phrasings_generalize() -> None:
    for q in ["Vitals: refer to nursing flowsheet.", "VS deferred to day of surgery.", "Vital signs not obtained (telephone visit)."]:
        assert _label(SpanKind.VITALS_DEFERRED, q) == "DEFERRED"


def test_sbp_dbp_text_reading_reaches_engine() -> None:
    text = "VITAL SIGNS:\n  08:15  SBP 182, DBP 96, HR 80, T=98.6\n"
    docs = {"n": Doc("n", "PAT Visit Note", "2026-03-03", "RN", text, 0)}
    kept, _ = ground_spans(RULES.extract("n", None, text), {"n": text})
    tf, flags = spans_to_facts(kept, docs, POLICY)
    assert flags == []
    res = evaluate(base_facts(bp_at=datetime(2026, 3, 1), temp_at=datetime(2026, 3, 1), extra=[f for f in tf if f.kind != "DOC_KIND"]), [], POLICY)
    assert res.decision == "NOT_CLEARED"
    assert any("unit" in a.text.lower() for a in res.assumptions)


def test_attribution_to_surgical_team_is_not_a_deferral() -> None:
    assert _label(SpanKind.ANTICOAG_RESUME, "Resume Xarelto 24-48 hours after surgery once hemostasis is secure, per surgical team.") == "SPECIFIC"
    assert _label(SpanKind.ANTICOAG_DEFERRED, "Perioperative management of apixaban per the surgical team.") == "DEFERRED"


def test_deferral_phrased_as_resume_is_not_specific() -> None:
    assert _label(SpanKind.ANTICOAG_RESUME, "Resumption of apixaban will be addressed postoperatively.") != "SPECIFIC"
    assert _label(SpanKind.ANTICOAG_HOLD, "Hold Eliquis 2 days before surgery; final timing to be determined by the surgical team.") != "SPECIFIC"


def test_invariants_accept_structured_celsius_exclusion() -> None:
    from preop_triage.invariants import check_invariants
    from preop_triage.model import Evidence, Issue, TriageOutput

    sub = {"vitals": [{"type": "temp", "value_c": 38.6, "date": "2026-03-24T09:00:00Z"}]}
    out = TriageOutput(decision="NOT_CLEARED", issues=[Issue(category="ACUTE_SAFETY_EXCLUSION", rule_id="R4", status="TEMP_EXCLUSION", summary="s", evidence=[Evidence(source="vitals[0]")])])
    assert check_invariants(out, {}, sub, POLICY).decision == "NOT_CLEARED"


# --- guardian / proxy signatures -----------------------------------------------------


@pytest.mark.parametrize("quote", [
    "Signed by: Maria Lopez, legal guardian and healthcare proxy, on behalf of the patient",
    "Signed by Jose Ortiz, healthcare proxy, on 03/12/2026.",
    "Consent signed on behalf of the patient by her daughter (POA).",
])
def test_guardian_or_proxy_signature_is_signed(quote: str) -> None:
    assert _label(SpanKind.CONSENT_SIGNATURE, quote) == "SIGNED"


def test_proxy_signature_fact_records_signer() -> None:
    text = "INFORMED CONSENT\nProcedure: carpal tunnel release\nSigned by: Maria Lopez, legal guardian and healthcare proxy, on behalf of the patient\n"
    docs = {"c": Doc("c", "Surgical Consent", "2026-02-25", "MD", text, 0)}
    kept, _ = ground_spans(RULES.extract("c", None, text), {"c": text})
    facts, flags = spans_to_facts(kept, docs, POLICY)
    sig = [f for f in facts if f.kind == "CONSENT_SIG"]
    assert flags == [] and sig and sig[0].value == "SIGNED" and sig[0].meta.get("signer") == "proxy"


# --- med mentions stay anchored to a reporting verb ------------------------------------


def test_no_longer_takes_is_not_taking() -> None:
    assert _label(SpanKind.MED_MENTION, "no longer takes Eliquis") == "NOT_TAKING"
    assert _label(SpanKind.MED_MENTION, "She no longer takes Eliquis since the bleed.") == "NOT_TAKING"


def test_bare_takes_without_reporting_verb_is_not_a_mention() -> None:
    label, flag = classify(Span(SpanKind.MED_MENTION, "d", "takes Eliquis", 0), POLICY)
    assert label is None and flag is not None


def test_reporting_verb_still_anchors_taking() -> None:
    assert _label(SpanKind.MED_MENTION, "Pt states she takes Eliquis 5 mg twice daily") == "TAKING"
    assert _label(SpanKind.MED_MENTION, "patient reports he takes Xarelto") == "TAKING"


# --- a decimal dose must not end the sentence (held-out case 04) -------------------------


def test_decimal_dose_inside_plan_sentence_is_still_specific() -> None:
    assert _label(SpanKind.ANTICOAG_HOLD, "Hold apixaban 2.5 mg for 2 days before surgery (last dose 03/02/2026).") == "SPECIFIC"
    assert _label(SpanKind.ANTICOAG_RESUME, "Resume apixaban 2.5 mg BID 48 hours after surgery once hemostasis is confirmed.") == "SPECIFIC"


def test_period_between_digits_only_does_not_cross_sentences() -> None:
    assert _label(SpanKind.ANTICOAG_RESUME, "Resume Xarelto when cleared by the surgeon; timing to be determined. Ice to wrist for 48 hours.") != "SPECIFIC"
    assert _label(SpanKind.ANTICOAG_HOLD, "Hold warfarin per cardiology.2 days before surgery is the usual.") != "SPECIFIC"


# --- CONTINUE plans (held-out case 01) ----------------------------------------------------

CONTINUE_LINE = "Continue Eliquis through surgery without interruption; no doses are to be held before or after the procedure."


@pytest.mark.parametrize("quote", [
    CONTINUE_LINE,
    "Continue apixaban perioperatively.",
    "Continue warfarin without interruption.",
    "Patient may continue Xarelto before and after the procedure.",
    "Continue Pradaxa through surgery.",
])
def test_continue_with_perioperative_phrase_is_specific(quote: str) -> None:
    assert _label(SpanKind.ANTICOAG_CONTINUE, quote) == "SPECIFIC"


@pytest.mark.parametrize("quote", ["Continue apixaban.", "Continue Eliquis 5 mg BID.", "Continue home Xarelto."])
def test_bare_continue_is_vague(quote: str) -> None:
    assert _label(SpanKind.ANTICOAG_CONTINUE, quote) == "VAGUE"


def _plan_facts(text: str):
    docs = {"p": Doc("p", "Cardiology Consultation", "2026-02-22", "MD", text, 0)}
    kept, _ = ground_spans(RULES.extract("p", None, text), {"p": text})
    tf, flags = spans_to_facts(kept, docs, POLICY)
    assert flags == []
    doc_fact = F("DOC", {"type": "Cardiology Consultation", "date": "2026-02-22", "author": "MD", "index": 5}, datetime(2026, 2, 22), "documents[5]", doc_id="p")
    return [doc_fact] + [f for f in tf if f.kind != "DOC_KIND"]


def test_specific_continue_plan_satisfies_r3_with_assumption() -> None:
    text = f"CARDIOLOGY CONSULTATION\nHPI: maintained on Eliquis 5 mg BID.\nRECOMMENDATIONS:\n  1. {CONTINUE_LINE}\n  2. No bridging required.\n"
    res = evaluate(base_facts(extra=[_med("apixaban 5 mg PO BID", "apixaban", "anticoagulant", True)] + _plan_facts(text)), [], POLICY)
    assert res.decision == "READY"
    assert {c.rule_id: c.status for c in res.checks}["R3"] == "PASS"
    assert any("continue" in a.text.lower() and CONTINUE_LINE in a.text for a in res.assumptions)
    assert any(e.quote == CONTINUE_LINE for a in res.assumptions for e in a.evidence)


def test_vague_continue_stays_incomplete_plan() -> None:
    text = "CARDIOLOGY CONSULTATION\nHPI: maintained on Eliquis 5 mg BID.\nRECOMMENDATIONS:\n  1. Continue Eliquis.\n"
    res = evaluate(base_facts(extra=[_med("apixaban 5 mg PO BID", "apixaban", "anticoagulant", True)] + _plan_facts(text)), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "INCOMPLETE_PLAN"

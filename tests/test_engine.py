from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from preop_triage.engine import evaluate
from preop_triage.model import Fact, ReviewFlag
from preop_triage.policy import load_policy

POLICY = load_policy()
D = date(2026, 3, 5)


def F(kind: str, value, observed_at=None, source: str = "s", doc_id=None, quote=None, parser="structured", meta=None) -> Fact:
    return Fact(kind, value, observed_at, source, doc_id, quote, parser, meta or {})


def base_facts(**kw) -> list[Fact]:
    """A package that is READY unless overridden."""
    facts = [
        F("PROCEDURE_DATE", kw.get("pdate", D), source="procedure.procedure_date"),
        F("PROCEDURE_RISK", kw.get("risk", "MODERATE"), source="procedure.procedure_risk"),
        F("PROCEDURE_TYPE", kw.get("ptype", "Elective thyroidectomy"), source="procedure.procedure_type"),
        F("REVIEW_TIME", kw.get("review", datetime(2026, 3, 5, 14, 0)), source="metadata.submission_received_at"),
        F("DOC", {"type": "H&P", "date": "2026-02-24", "author": "A", "index": 0}, datetime(2026, 2, 24), "documents[0]", doc_id="hp"),
        F("DOC", {"type": "Surgical Consent", "date": "2026-03-03", "author": "B", "index": 1}, datetime(2026, 3, 3), "documents[1]", doc_id="c1"),
    ]
    if kw.get("hp", True):
        facts.append(F("DOC_KIND", "PREOP_HP", datetime(2026, 2, 24), "documents[0]", doc_id="hp", parser="doc_kind"))
    if kw.get("consent", True):
        facts += [
            F("DOC_KIND", "CONSENT", datetime(2026, 3, 3), "documents[1]", doc_id="c1", parser="doc_kind"),
            F("CONSENT_SIG", kw.get("sig", "SIGNED"), datetime(2026, 3, 3), "documents[1]", doc_id="c1", quote="Patient signature on file - signed in clinic 03/03/2026", parser="text"),
        ]
        if kw.get("consent_proc", True):
            facts.append(F("CONSENT_PROC", kw.get("proc_line", "thyroidectomy"), datetime(2026, 3, 3), "documents[1]", doc_id="c1", quote="Procedure: thyroidectomy", parser="text"))
    if kw.get("cbc", True):
        facts.append(F("LAB", {"code": "CBC", "status": kw.get("cbc_status", "final"), "display": "CBC"}, kw.get("cbc_at", datetime(2026, 2, 20, 8, 0)), "labs[0]"))
    for lab in kw.get("labs", []):
        facts.append(lab)
    if kw.get("vitals", True):
        facts += [
            F("BP", kw.get("bp", (120, 80)), kw.get("bp_at", datetime(2026, 3, 2, 9, 0)), "vitals[0]"),
            F("TEMP", Decimal(str(kw.get("temp", "98.6"))), kw.get("temp_at", datetime(2026, 3, 2, 9, 5)), "vitals[1]"),
        ]
    facts += kw.get("extra", [])
    return facts


def cats(res) -> list[str]:
    return sorted({i.category for i in res.issues})


def test_ready_baseline() -> None:
    res = evaluate(base_facts(), [], POLICY)
    assert res.decision == "READY" and res.issues == []
    assert {c.rule_id: c.status for c in res.checks} == {"R0": "PASS", "R1.1": "PASS", "R1.2": "PASS", "R2": "PASS", "R3": "SKIPPED", "R4": "PASS"}


def test_hp_30_days_ready_and_31_outdated() -> None:
    f30 = [f for f in base_facts() if f.doc_id != "hp"] + [
        F("DOC", {"type": "H&P", "date": "2026-02-03", "author": "A", "index": 0}, datetime(2026, 2, 3), "documents[0]", doc_id="hp"),
        F("DOC_KIND", "PREOP_HP", datetime(2026, 2, 3), "documents[0]", doc_id="hp", parser="doc_kind"),
    ]
    assert evaluate(f30, [], POLICY).decision == "READY"
    f31 = [f for f in base_facts() if f.doc_id != "hp"] + [
        F("DOC", {"type": "H&P", "date": "2026-02-02", "author": "A", "index": 0}, datetime(2026, 2, 2), "documents[0]", doc_id="hp"),
        F("DOC_KIND", "PREOP_HP", datetime(2026, 2, 2), "documents[0]", doc_id="hp", parser="doc_kind"),
    ]
    res = evaluate(f31, [], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["REQUIRED_DOCUMENTATION"]
    issue = res.issues[0]
    assert issue.status == "OUTDATED" and issue.computed.days_before == 31 and issue.computed.window_days == 30


def test_hp_missing() -> None:
    res = evaluate(base_facts(hp=False), [], POLICY)
    assert cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "MISSING"


def test_hp_only_wellness_non_qualifying() -> None:
    facts = base_facts(hp=False) + [F("DOC_KIND", "NON_QUALIFYING_HP", datetime(2026, 2, 24), "documents[0]", doc_id="hp", parser="doc_kind")]
    res = evaluate(facts, [], POLICY)
    assert cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "NON_QUALIFYING"
    assert any(fl.reason == "ONLY_NON_QUALIFYING_HP" for fl in res.review_flags)


def test_consent_missing() -> None:
    res = evaluate(base_facts(consent=False), [], POLICY)
    assert cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "MISSING"


def test_consent_unsigned() -> None:
    res = evaluate(base_facts(sig="UNSIGNED"), [], POLICY)
    assert cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "UNSIGNED"


def test_consent_unsigned_then_signed_ready() -> None:
    facts = base_facts(sig="UNSIGNED") + [
        F("DOC", {"type": "Consent", "date": "2026-03-04", "author": "B", "index": 2}, datetime(2026, 3, 4), "documents[2]", doc_id="c2"),
        F("DOC_KIND", "CONSENT", datetime(2026, 3, 4), "documents[2]", doc_id="c2", parser="doc_kind"),
        F("CONSENT_SIG", "SIGNED", datetime(2026, 3, 4), "documents[2]", doc_id="c2", quote="Signed by patient", parser="text"),
        F("CONSENT_PROC", "thyroidectomy", datetime(2026, 3, 4), "documents[2]", doc_id="c2", quote="Procedure: thyroidectomy", parser="text"),
    ]
    assert evaluate(facts, [], POLICY).decision == "READY"


def test_consent_wrong_procedure() -> None:
    res = evaluate(base_facts(proc_line="coronary artery bypass grafting"), [], POLICY)
    assert cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "WRONG_PROCEDURE"


def test_consent_signed_without_procedure_line_unverified() -> None:
    res = evaluate(base_facts(consent_proc=False), [], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "UNVERIFIED"


def test_cbc_15_days_high_fails_and_14_passes() -> None:
    cmp_ok = F("LAB", {"code": "CMP", "status": "final", "display": "CMP"}, datetime(2026, 3, 1), "labs[1]")
    res = evaluate(base_facts(risk="HIGH", cbc_at=datetime(2026, 2, 18, 8, 0), labs=[cmp_ok]), [], POLICY)
    assert cats(res) == ["REQUIRED_TESTING"] and res.issues[0].computed.days_before == 15 and res.issues[0].computed.window_days == 14
    res = evaluate(base_facts(risk="HIGH", cbc_at=datetime(2026, 2, 19, 8, 0), labs=[cmp_ok]), [], POLICY)
    assert res.decision == "READY"


def test_bmp_not_cmp() -> None:
    bmp = F("LAB", {"code": "BMP", "status": "final", "display": "BMP"}, datetime(2026, 3, 1), "labs[1]")
    res = evaluate(base_facts(risk="HIGH", cbc_at=datetime(2026, 3, 1), labs=[bmp]), [], POLICY)
    assert cats(res) == ["REQUIRED_TESTING"] and res.issues[0].status == "MISSING" and "CMP" in res.issues[0].summary


def test_entered_in_error_newest_lab_skipped() -> None:
    bad = F("LAB", {"code": "CBC", "status": "entered-in-error", "display": "CBC"}, datetime(2026, 3, 4), "labs[1]")
    res = evaluate(base_facts(cbc_at=datetime(2026, 1, 20), labs=[bad]), [], POLICY)
    assert cats(res) == ["REQUIRED_TESTING"] and res.issues[0].status == "OUTDATED"
    assert any(e.source == "labs[1]" for e in res.issues[0].evidence)


def test_cbc_missing() -> None:
    res = evaluate(base_facts(cbc=False), [], POLICY)
    assert cats(res) == ["REQUIRED_TESTING"] and res.issues[0].status == "MISSING"


def _med(name: str, canonical: str | None, cls: str, active: bool | None) -> Fact:
    return F("MED", {"name": name, "canonical": canonical, "class": cls, "active": active}, source="medications[0]")


def _plan(kind: str, canonical: str | None, label: str, doc_id: str = "plan", when=datetime(2026, 2, 26)) -> Fact:
    return F(kind, {"canonical": canonical, "label": label}, when, "documents[5]", doc_id=doc_id, quote=f"{kind} {canonical}", parser="text")


PLAN_DOC = F("DOC", {"type": "Cardiology Consultation", "date": "2026-02-26", "author": "C", "index": 5}, datetime(2026, 2, 26), "documents[5]", doc_id="plan")


def test_anticoag_no_plan() -> None:
    res = evaluate(base_facts(extra=[_med("Xarelto 20 mg", "rivaroxaban", "anticoagulant", True)]), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "NO_PLAN"


def test_anticoag_full_plan_ready() -> None:
    extra = [_med("Xarelto", "rivaroxaban", "anticoagulant", True), PLAN_DOC, _plan("ANTICOAG_HOLD", "rivaroxaban", "SPECIFIC"), _plan("ANTICOAG_RESUME", "rivaroxaban", "SPECIFIC")]
    res = evaluate(base_facts(extra=extra), [], POLICY)
    assert res.decision == "READY" and {c.rule_id: c.status for c in res.checks}["R3"] == "PASS"


def test_anticoag_hold_only_incomplete() -> None:
    extra = [_med("Coumadin", "warfarin", "anticoagulant", True), PLAN_DOC, _plan("ANTICOAG_HOLD", "warfarin", "SPECIFIC"), _plan("ANTICOAG_DEFERRED", None, "DEFERRED")]
    res = evaluate(base_facts(extra=extra), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "INCOMPLETE_PLAN"


def test_anticoag_deferred_only_incomplete() -> None:
    extra = [_med("Pradaxa", "dabigatran", "anticoagulant", True), PLAN_DOC, _plan("ANTICOAG_DEFERRED", "dabigatran", "DEFERRED")]
    res = evaluate(base_facts(extra=extra), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "INCOMPLETE_PLAN"


def test_anticoag_vague_hold_and_resume_incomplete() -> None:
    extra = [_med("Xarelto", "rivaroxaban", "anticoagulant", True), PLAN_DOC, _plan("ANTICOAG_HOLD", "rivaroxaban", "VAGUE"), _plan("ANTICOAG_RESUME", "rivaroxaban", "VAGUE")]
    res = evaluate(base_facts(extra=extra), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"]


def test_anticoag_stale_plan_other_drug() -> None:
    extra = [_med("apixaban", "apixaban", "anticoagulant", True), _med("warfarin", "warfarin", "anticoagulant", False), PLAN_DOC, _plan("ANTICOAG_HOLD", "warfarin", "SPECIFIC"), _plan("ANTICOAG_RESUME", "warfarin", "SPECIFIC")]
    res = evaluate(base_facts(extra=extra), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "STALE_PLAN"


def test_antiplatelet_needs_no_plan() -> None:
    res = evaluate(base_facts(extra=[_med("ASA 81", "aspirin", "antiplatelet", True), _med("clopidogrel", "clopidogrel", "antiplatelet", True)]), [], POLICY)
    assert res.decision == "READY"


def test_inactive_anticoag_needs_no_plan() -> None:
    res = evaluate(base_facts(extra=[_med("warfarin", "warfarin", "anticoagulant", False)]), [], POLICY)
    assert res.decision == "READY"


def test_patient_reported_anticoag_counts_with_assumption() -> None:
    mention = F("MED_MENTION", {"canonical": "apixaban", "class": "anticoagulant", "label": "TAKING"}, datetime(2026, 3, 1), "documents[3]", doc_id="n", quote="also reports taking Eliquis", parser="text")
    res = evaluate(base_facts(extra=[_med("ASA 81", "aspirin", "antiplatelet", True), mention]), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"]
    assert any("patient-reported" in a.text for a in res.assumptions)


def test_unknown_active_is_missing_data_not_anticoag() -> None:
    res = evaluate(base_facts(extra=[_med("Coumadin 5 mg", "warfarin", "anticoagulant", None)]), [], POLICY)
    assert cats(res) == ["MISSING_REQUIRED_DATA"] and res.issues[0].status == "ANTICOAG_STATUS_UNKNOWN"


def test_unknown_active_resolved_by_mention() -> None:
    mention = F("MED_MENTION", {"canonical": "warfarin", "class": "anticoagulant", "label": "NOT_TAKING"}, datetime(2026, 3, 1), "documents[3]", doc_id="n", quote="warfarin was stopped", parser="text")
    res = evaluate(base_facts(extra=[_med("Coumadin 5 mg", "warfarin", "anticoagulant", None), mention]), [], POLICY)
    assert res.decision == "READY" and res.assumptions


def test_dbp_110_not_cleared_and_109_ready() -> None:
    res = evaluate(base_facts(bp=(148, 110)), [], POLICY)
    assert res.decision == "NOT_CLEARED" and cats(res) == ["ACUTE_SAFETY_EXCLUSION"] and res.issues[0].computed.threshold == "diastolic >= 110"
    assert evaluate(base_facts(bp=(148, 109)), [], POLICY).decision == "READY"


def test_sbp_180_not_cleared() -> None:
    assert evaluate(base_facts(bp=(180, 90)), [], POLICY).decision == "NOT_CLEARED"


def test_temp_100_4_ready_and_100_5_not_cleared() -> None:
    assert evaluate(base_facts(temp="100.4"), [], POLICY).decision == "READY"
    res = evaluate(base_facts(temp="100.5"), [], POLICY)
    assert res.decision == "NOT_CLEARED" and res.issues[0].computed.observed_value == "100.5 F"


def test_latest_vital_wins_recheck_clears() -> None:
    later = F("BP", (137, 78), datetime(2026, 3, 3, 8, 30), "documents[2]", doc_id="n", quote="Re-measured 137/78 mmHg", parser="text_bp")
    res = evaluate(base_facts(bp=(189, 112), bp_at=datetime(2026, 3, 3, 8, 6), extra=[later]), [], POLICY)
    assert res.decision == "READY"


def test_text_vital_newer_than_structured() -> None:
    later = F("BP", (157, 117), datetime(2026, 3, 3, 10, 0), "documents[2]", doc_id="n", quote="BP 157/117", parser="text_bp")
    res = evaluate(base_facts(extra=[later]), [], POLICY)
    assert res.decision == "NOT_CLEARED" and res.issues[0].evidence[0].quote == "BP 157/117"


def test_tie_structured_wins_with_assumption() -> None:
    tie = F("BP", (190, 120), datetime(2026, 3, 2, 9, 0), "documents[2]", doc_id="n", quote="BP 190/120", parser="text_bp")
    res = evaluate(base_facts(extra=[tie]), [], POLICY)
    assert res.decision == "READY" and any("tie" in a.text.lower() for a in res.assumptions)


def test_missing_temp_is_missing_data() -> None:
    facts = [f for f in base_facts() if f.kind != "TEMP"]
    res = evaluate(facts, [], POLICY)
    assert cats(res) == ["MISSING_REQUIRED_DATA"] and res.issues[0].status == "TEMP_MISSING"


def test_no_vitals_is_single_missing_data_issue() -> None:
    res = evaluate(base_facts(vitals=False), [], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["MISSING_REQUIRED_DATA"]
    assert {i.status for i in res.issues} == {"BP_MISSING", "TEMP_MISSING"}


def test_vital_after_review_time_ignored() -> None:
    future = F("BP", (200, 120), datetime(2026, 3, 6, 9, 0), "vitals[2]")
    res = evaluate(base_facts(extra=[future]), [], POLICY)
    assert res.decision == "READY" and any("after review time" in a.text for a in res.assumptions)


def test_null_date_skips_windows_but_checks_presence() -> None:
    res = evaluate(base_facts(pdate=None, consent=False), [], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["MISSING_REQUIRED_DATA", "REQUIRED_DOCUMENTATION"]
    assert {c.rule_id: c.status for c in res.checks}["R2"] == "SKIPPED"
    assert evaluate(base_facts(pdate=None), [], POLICY).issues[0].status == "PROCEDURE_DATE_MISSING"


def test_null_risk_skips_r2() -> None:
    res = evaluate(base_facts(risk=None, cbc=False), [], POLICY)
    assert cats(res) == ["MISSING_REQUIRED_DATA"] and res.issues[0].status == "PROCEDURE_RISK_MISSING"


def test_review_time_fallback_assumption() -> None:
    res = evaluate(base_facts(review=None), [], POLICY)
    assert res.decision == "READY" and any("review time" in a.text.lower() for a in res.assumptions)


def test_not_cleared_plus_documentation_lists_both() -> None:
    res = evaluate(base_facts(bp=(196, 110), sig="UNSIGNED"), [], POLICY)
    assert res.decision == "NOT_CLEARED" and cats(res) == ["ACUTE_SAFETY_EXCLUSION", "REQUIRED_DOCUMENTATION"]


def test_review_flag_forces_needs_follow_up_with_rule_category() -> None:
    res = evaluate(base_facts(), [ReviewFlag("MODEL_CODE_DISAGREE", "c1", "x", "SIGNED", "UNSIGNED", "R1")], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["REQUIRED_DOCUMENTATION"] and res.issues[0].status == "UNVERIFIED"


def test_r4_flag_routes_to_missing_data() -> None:
    res = evaluate(base_facts(), [ReviewFlag("UNIT_CONFLICT", "n", "x", None, None, "R4")], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["MISSING_REQUIRED_DATA"]


def test_vital_without_timestamp_is_unverified() -> None:
    nots = F("BP", (120, 80), None, "documents[2]", doc_id="n", quote="BP 120/80", parser="text_bp")
    res = evaluate(base_facts(extra=[nots]), [], POLICY)
    assert res.decision == "NEEDS_FOLLOW_UP" and cats(res) == ["MISSING_REQUIRED_DATA"]


def _mention(canonical: str, label: str, when: datetime, quote: str) -> Fact:
    return F("MED_MENTION", {"canonical": canonical, "class": "anticoagulant", "label": label}, when, "documents[3]", doc_id="n", quote=quote, parser="text")


def test_deferred_without_anticoag_context_is_ignored() -> None:
    boilerplate = F("ANTICOAG_DEFERRED", {"canonical": None, "label": "DEFERRED"}, datetime(2026, 3, 1), "documents[3]", doc_id="n", quote="formal consent per surgical team.", parser="text")
    res = evaluate(base_facts(extra=[_med("Xarelto", "rivaroxaban", "anticoagulant", True), boilerplate]), [], POLICY)
    assert cats(res) == ["ANTICOAGULATION_MANAGEMENT"] and res.issues[0].status == "NO_PLAN"


def test_deferred_with_anticoag_context_counts() -> None:
    deferred = F("ANTICOAG_DEFERRED", {"canonical": None, "label": "DEFERRED"}, datetime(2026, 3, 1), "documents[3]", doc_id="n", quote="Perioperative anticoagulation to be determined by the surgical team.", parser="text")
    res = evaluate(base_facts(extra=[_med("Xarelto", "rivaroxaban", "anticoagulant", True), deferred]), [], POLICY)
    assert res.issues[0].status == "INCOMPLETE_PLAN"


def test_older_taking_mention_superseded_no_conflict() -> None:
    old = _mention("warfarin", "TAKING", datetime(2026, 2, 6), "maintained on warfarin")
    new = _mention("warfarin", "NOT_TAKING", datetime(2026, 3, 17), "warfarin was stopped")
    res = evaluate(base_facts(extra=[_med("warfarin", "warfarin", "anticoagulant", False), old, new]), [], POLICY)
    assert res.decision == "READY" and res.review_flags == []


def test_latest_taking_mention_conflicts_with_inactive_list() -> None:
    new = _mention("warfarin", "TAKING", datetime(2026, 3, 17), "maintained on warfarin")
    res = evaluate(base_facts(extra=[_med("warfarin", "warfarin", "anticoagulant", False), new]), [], POLICY)
    assert any(f.reason == "MED_STATUS_CONFLICT" and f.quote == "maintained on warfarin" for f in res.review_flags)


def test_unlisted_drug_latest_mention_wins() -> None:
    old = _mention("apixaban", "TAKING", datetime(2026, 2, 6), "maintained on Eliquis")
    new = _mention("apixaban", "NOT_TAKING", datetime(2026, 3, 17), "Eliquis was stopped")
    res = evaluate(base_facts(extra=[old, new]), [], POLICY)
    assert res.decision == "READY"

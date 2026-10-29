from __future__ import annotations

import json

from preop_triage.invariants import check_invariants
from preop_triage.model import Check, Evidence, Issue, TriageOutput

DOCS = {"d1": "Most recent BP 196/110 at the pre-op clinic.\nPatient signature: pending"}
SUB = {"procedure": {"procedure_date": None}, "vitals": [], "labs": []}


def _issue(category: str, rule: str, quote: str | None = "BP 196/110", source: str = "documents:d1", doc_id: str | None = "d1") -> Issue:
    return Issue(category=category, rule_id=rule, status="X", summary="s", evidence=[Evidence(source=source, doc_id=doc_id, quote=quote)])


def test_valid_output_passes_unchanged() -> None:
    out = TriageOutput(decision="NOT_CLEARED", issues=[_issue("ACUTE_SAFETY_EXCLUSION", "R4")], checks=[Check(rule_id="R4", status="FAIL")])
    assert check_invariants(out, DOCS, SUB) == out


def test_ready_with_issue_becomes_system_error() -> None:
    out = TriageOutput(decision="READY", issues=[_issue("REQUIRED_DOCUMENTATION", "R1.2", "Patient signature: pending")])
    fixed = check_invariants(out, DOCS, SUB)
    assert fixed.decision == "NEEDS_FOLLOW_UP"
    assert fixed.review_flags[-1].reason == "SYSTEM_ERROR" and "READY" in (fixed.review_flags[-1].quote or "")


def test_ready_with_review_flag_becomes_system_error() -> None:
    from preop_triage.model import ReviewFlagOut

    out = TriageOutput(decision="READY", review_flags=[ReviewFlagOut(reason="MODEL_CODE_DISAGREE")])
    assert check_invariants(out, DOCS, SUB).decision == "NEEDS_FOLLOW_UP"


def test_not_cleared_without_r4_evidence_becomes_system_error() -> None:
    out = TriageOutput(decision="NOT_CLEARED", issues=[_issue("ACUTE_SAFETY_EXCLUSION", "R4", quote=None, source="vitals", doc_id=None)])
    fixed = check_invariants(out, DOCS, SUB)
    assert fixed.decision == "NEEDS_FOLLOW_UP" and fixed.review_flags[-1].reason == "SYSTEM_ERROR"


def test_not_cleared_with_structured_vital_evidence_passes() -> None:
    sub = {"vitals": [{"type": "blood_pressure", "systolic": 196, "diastolic": 110, "date": "2026-03-24T09:00:00Z"}]}
    out = TriageOutput(decision="NOT_CLEARED", issues=[_issue("ACUTE_SAFETY_EXCLUSION", "R4", quote=None, source="vitals[0]", doc_id=None)])
    assert check_invariants(out, DOCS, sub).decision == "NOT_CLEARED"


def test_evidence_quote_must_reground() -> None:
    out = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_issue("REQUIRED_DOCUMENTATION", "R1.2", "signature: forged")])
    fixed = check_invariants(out, DOCS, SUB)
    assert fixed.review_flags[-1].reason == "SYSTEM_ERROR" and "reground" in (fixed.review_flags[-1].quote or "")


def test_issue_without_evidence_is_system_error() -> None:
    out = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[Issue(category="REQUIRED_TESTING", rule_id="R2", status="MISSING", summary="s")])
    assert check_invariants(out, DOCS, SUB).review_flags[-1].reason == "SYSTEM_ERROR"


def test_category_must_match_rule() -> None:
    out = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_issue("REQUIRED_TESTING", "R1.2", "Patient signature: pending")])
    assert check_invariants(out, DOCS, SUB).review_flags[-1].reason == "SYSTEM_ERROR"


def test_serialization_is_stable() -> None:
    out = TriageOutput(decision="NEEDS_FOLLOW_UP", issues=[_issue("REQUIRED_DOCUMENTATION", "R1.2", "Patient signature: pending")])
    a = check_invariants(out, DOCS, SUB)
    b = check_invariants(out, DOCS, SUB)
    assert json.dumps(a.model_dump(), sort_keys=True) == json.dumps(b.model_dump(), sort_keys=True)

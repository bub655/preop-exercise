from __future__ import annotations

from datetime import datetime

from preop_triage.ingest import ingest, parse_iso
from preop_triage.policy import load_policy

POLICY = load_policy()


def _sub(**overrides: object) -> dict:
    base = {
        "patient": {"id": "p1"},
        "procedure": {"case_id": "c1", "procedure_type": "Elective thyroidectomy", "procedure_risk": "MODERATE", "procedure_date": "2026-03-05"},
        "vitals": [
            {"type": "blood_pressure", "systolic": 117, "diastolic": 72, "date": "2026-02-17T09:05:00Z", "source": "Pre-op clinic"},
            {"type": "temperature", "value_f": 98.8, "date": "2026-02-24T10:15:00Z", "source": "Pre-op clinic"},
        ],
        "labs": [{"id": "l1", "code": "LAB-CBC", "effective_at": "2026-02-21T08:10:00Z", "status": "final"}],
        "medications": [{"name": "Xarelto 20 mg PO daily", "active": True}],
        "conditions": [],
        "documents": [{"doc_id": "d1", "type": "H&P", "date": "2026-02-19", "author": "A", "text": "x"}],
        "metadata": {"submission_received_at": "2026-03-01T14:22:10Z"},
    }
    base.update(overrides)
    return base


def _facts(kind: str, facts: list) -> list:
    return [f for f in facts if f.kind == kind]


def test_parse_iso_naive_utc() -> None:
    assert parse_iso("2026-02-17T09:05:00Z") == datetime(2026, 2, 17, 9, 5)
    assert parse_iso("2026-02-19") == datetime(2026, 2, 19, 0, 0)
    assert parse_iso(None) is None
    assert parse_iso("garbage") is None


def test_vitals_become_facts_with_timestamps() -> None:
    facts, flags = ingest(_sub(), POLICY)
    bp = _facts("BP", facts)[0]
    assert bp.value == (117, 72) and bp.observed_at == datetime(2026, 2, 17, 9, 5) and bp.source == "vitals[0]"
    temp = _facts("TEMP", facts)[0]
    assert str(temp.value) == "98.8" and temp.source == "vitals[1]"
    assert flags == []


def test_bp_missing_diastolic_flagged() -> None:
    sub = _sub(vitals=[{"type": "blood_pressure", "systolic": 150, "diastolic": None, "date": "2026-02-17T09:05:00Z"}])
    facts, flags = ingest(sub, POLICY)
    assert _facts("BP", facts) == []
    assert flags[0].reason == "STRUCTURED_VITAL_INCOMPLETE" and flags[0].rule_id == "R4"


def test_lab_code_normalized() -> None:
    facts, _ = ingest(_sub(), POLICY)
    lab = _facts("LAB", facts)[0]
    assert lab.value["code"] == "CBC" and lab.value["status"] == "final" and lab.observed_at == datetime(2026, 2, 21, 8, 10)


def test_entered_in_error_status_preserved() -> None:
    sub = _sub(labs=[{"code": "CBC", "effective_at": "2026-02-21T08:10:00Z", "status": "entered-in-error"}])
    facts, _ = ingest(sub, POLICY)
    assert _facts("LAB", facts)[0].value["status"] == "entered-in-error"


def test_med_classified() -> None:
    facts, _ = ingest(_sub(), POLICY)
    med = _facts("MED", facts)[0]
    assert med.value == {"name": "Xarelto 20 mg PO daily", "canonical": "rivaroxaban", "class": "anticoagulant", "active": True}


def test_med_unknown_active_preserved() -> None:
    sub = _sub(medications=[{"name": "Coumadin 5 mg PO daily", "active": None}])
    facts, _ = ingest(sub, POLICY)
    assert _facts("MED", facts)[0].value["active"] is None


def test_procedure_and_review_time_facts() -> None:
    facts, _ = ingest(_sub(), POLICY)
    assert _facts("PROCEDURE_DATE", facts)[0].value == datetime(2026, 3, 5).date()
    assert _facts("PROCEDURE_RISK", facts)[0].value == "MODERATE"
    assert _facts("PROCEDURE_TYPE", facts)[0].value == "Elective thyroidectomy"
    assert _facts("REVIEW_TIME", facts)[0].value == datetime(2026, 3, 1, 14, 22, 10)


def test_null_procedure_date_fact_value_none() -> None:
    sub = _sub(procedure={"case_id": "c1", "procedure_type": "x", "procedure_risk": None, "procedure_date": None})
    facts, _ = ingest(sub, POLICY)
    assert _facts("PROCEDURE_DATE", facts)[0].value is None
    assert _facts("PROCEDURE_RISK", facts)[0].value is None


def test_doc_facts() -> None:
    facts, _ = ingest(_sub(), POLICY)
    doc = _facts("DOC", facts)[0]
    assert doc.doc_id == "d1" and doc.value["type"] == "H&P" and doc.observed_at == datetime(2026, 2, 19)


def test_unparseable_vital_date_flagged() -> None:
    sub = _sub(vitals=[{"type": "blood_pressure", "systolic": 150, "diastolic": 90, "date": "yesterday"}])
    facts, flags = ingest(sub, POLICY)
    assert _facts("BP", facts) == []
    assert flags[0].reason == "STRUCTURED_DATE_UNPARSEABLE"

"""Hand-written held-out cases that the 50-case sample does not cover.

Each case targets one seam in the pipeline (a phrasing, a unit, a boundary, a
code map) and carries the decision I would expect from reading the policy, not
the decision the engine happens to produce. Run:

    uv run scripts/make_heldout_cases.py            # writes data/heldout_cases.jsonl
    make baseline evals INPUT=data/heldout_cases.jsonl OUTPUT=data/heldout_outputs.jsonl REPORT=data/heldout_report.json
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "heldout_cases.jsonl"

PROC_DATE = "2026-03-05"
RECEIVED = "2026-03-01T14:00:00Z"


def hp(text_extra: str = "", date: str = "2026-02-20", procedure: str = "carpal tunnel release") -> dict:
    return {
        "doc_id": f"hp-{date}",
        "type": "History and Physical",
        "date": date,
        "author": "Brian O'Connor, MD",
        "text": (
            "PREOPERATIVE HISTORY AND PHYSICAL\n"
            f"Date of service: {date}\n"
            f"Planned procedure: {procedure} (scheduled for 03/05/2026)\n\n"
            "HISTORY OF PRESENT ILLNESS: Referred for pre-operative evaluation. Denies recent illness.\n"
            "PAST MEDICAL HISTORY: Hypertension.\n"
            "EXAM: Well-appearing. Heart regular. Lungs clear.\n"
            "ASSESSMENT/PLAN: Acceptable candidate for the planned procedure.\n" + text_extra
        ),
    }


def consent(signature_line: str = "Patient signature: /s/ Grace Lopez", date: str = "2026-02-25", procedure: str = "carpal tunnel release") -> dict:
    return {
        "doc_id": f"consent-{date}",
        "type": "Surgical Consent",
        "date": date,
        "author": "Kevin A. Lee, MD",
        "text": (
            "INFORMED CONSENT FOR SURGICAL PROCEDURE\n"
            f"Procedure: {procedure}\n"
            "Surgeon: Kevin A. Lee, MD\n\n"
            "I authorize the surgeon named above to perform the procedure listed. Risks, benefits and alternatives were explained.\n\n"
            f"{signature_line}\n"
            f"Date signed: {date}\n"
            "Witness: Andrew Carter, RN\n"
        ),
    }


def note(doc_id: str, kind: str, date: str, text: str, author: str = "Lauren Q. Chen, DO") -> dict:
    return {"doc_id": doc_id, "type": kind, "date": date, "author": author, "text": text}


def lab(code: str, when: str, status: str = "final", display: str | None = None, lab_id: str | None = None) -> dict:
    return {"id": lab_id or f"lab-{code}-{when[:10]}", "code": code, "display": display or code, "effective_at": when, "status": status, "source": "Internal Hospital Lab"}


def base(case_id: str, *, risk: str = "LOW", procedure: str = "Elective carpal tunnel release", meds=None, labs=None, vitals=None, docs=None) -> dict:
    return {
        "patient": {"id": f"heldout-{case_id}", "mrn": f"MRN-H{case_id}", "name": {"given": "Grace", "family": "Lopez"}, "dob": "1951-04-02", "sex": "F"},
        "procedure": {"case_id": case_id, "procedure_type": procedure, "procedure_risk": risk, "procedure_date": PROC_DATE, "is_elective": True, "location": "Cadence Surgical Center - Main OR"},
        "vitals": vitals if vitals is not None else [
            {"type": "blood_pressure", "systolic": 128, "diastolic": 78, "date": "2026-02-26T09:05:00Z", "source": "Pre-op clinic"},
            {"type": "temperature", "value_f": 98.1, "date": "2026-02-26T09:06:00Z", "source": "Pre-op clinic"},
        ],
        "labs": labs if labs is not None else [lab("CBC", "2026-02-20T08:10:00Z", display="Complete Blood Count")],
        "medications": meds if meds is not None else [{"name": "lisinopril 10 mg PO daily", "active": True}],
        "conditions": [{"name": "Hypertension", "active": True}],
        "documents": docs if docs is not None else [hp(), consent()],
        "metadata": {"submission_received_at": RECEIVED, "source_system": "Cadence Intake API v2"},
    }


def case(case_id: str, decision: str, categories: list[str], rationale: str, submission: dict) -> dict:
    return {"case_id": case_id, "submission": submission, "label": {"decision": decision, "categories": categories, "rationale": rationale}}


CASES = [
    case(
        "heldout_01_continue_through_surgery", "READY", [],
        "Eliquis is active. The cardiology note says to continue it without interruption, before and after. That is a "
        "complete plan under Rule 3: it clearly describes how the drug is managed before and after the procedure. "
        "Tests whether the engine only recognizes hold/restart plans.",
        base("heldout_01", meds=[{"name": "apixaban 5 mg PO BID", "active": True}], docs=[hp(), consent(), note(
            "cards-01", "Cardiology Consultation", "2026-02-22",
            "CARDIOLOGY CONSULTATION - PERIOPERATIVE\nHPI: Atrial fibrillation maintained on Eliquis 5 mg BID.\n"
            "ASSESSMENT: Minor procedure with negligible bleeding risk.\n"
            "RECOMMENDATIONS:\n  1. Continue Eliquis through surgery without interruption; no doses are to be held before or after the procedure.\n"
            "  2. No bridging required.\n")]),
    ),
    case(
        "heldout_02_celsius_without_unit", "NOT_CLEARED", ["ACUTE_SAFETY_EXCLUSION"],
        "The nursing note on 02/28 records 'Temp 38.7' with no unit. A reading of 38.7 can only be Celsius, which is "
        "101.7 F and above the 100.4 F threshold. It is later than the structured 98.1 F reading. Tests whether a "
        "unit-free temperature is read or silently dropped.",
        base("heldout_02", docs=[hp(), consent(), note(
            "nurse-02", "Nursing Pre-procedure Assessment", "2026-02-28",
            "PRE-ADMISSION TESTING VISIT\nVisit date: 02/28/2026\nPlanned procedure: carpal tunnel release\n\n"
            "VITAL SIGNS 10:15: BP 131/80, HR 76, Temp 38.7, SpO2 97% RA.\nPatient reports feeling warm since last night.\n",
            author="Andrew Carter, RN")]),
    ),
    case(
        "heldout_03_guardian_signed_consent", "READY", [],
        "The consent is signed by the patient's legal guardian and healthcare proxy. The policy requires a signed "
        "surgical consent; it does not require the patient personally to sign. Tests the 'signed by patient' pattern.",
        base("heldout_03", docs=[hp(), consent(signature_line="Signed by: Maria Lopez, legal guardian and healthcare proxy, on behalf of the patient")]),
    ),
    case(
        "heldout_04_decimal_dose_in_plan", "READY", [],
        "Apixaban is active and the plan gives both timings: hold 2 days before (last dose 03/02/2026) and resume 48 "
        "hours after surgery. The dose '2.5 mg' appears inside the hold and resume sentences. Tests the known "
        "sentence-boundary bug from the write-up.",
        base("heldout_04", meds=[{"name": "apixaban 2.5 mg PO BID", "active": True}], docs=[hp(), consent(), note(
            "cards-04", "Cardiology Consultation", "2026-02-22",
            "CARDIOLOGY CONSULTATION - PERIOPERATIVE\nHPI: Atrial fibrillation on apixaban 2.5 mg BID.\n"
            "RECOMMENDATIONS:\n  1. Hold apixaban 2.5 mg for 2 days before surgery (last dose 03/02/2026).\n"
            "  2. Resume apixaban 2.5 mg BID 48 hours after surgery once hemostasis is confirmed.\n")]),
    ),
    case(
        "heldout_05_hp_31_days_cbc_30_days", "NEEDS_FOLLOW_UP", ["REQUIRED_DOCUMENTATION"],
        "H&P dated 02/02 is 31 days before the 03/05 procedure, one day outside the window. CBC on 02/03 is exactly "
        "30 days before and still counts (I treat windows as inclusive). Tests both boundaries at once.",
        base("heldout_05", labs=[lab("CBC", "2026-02-03T08:10:00Z", display="Complete Blood Count")], docs=[hp(date="2026-02-02"), consent()]),
    ),
    case(
        "heldout_06_unusual_lab_code_for_cmp", "READY", [],
        "HIGH-risk procedure. CBC 10 days out. The CMP arrives with the source code 'CMP14' and display 'Comprehensive "
        "Metabolic Panel (14)' dated 8 days out. A human reads the display and counts it. Tests the lab-code map.",
        base("heldout_06", risk="HIGH", procedure="Elective colectomy", labs=[
            lab("CBC", "2026-02-23T08:10:00Z", display="Complete Blood Count"),
            lab("CMP14", "2026-02-25T08:10:00Z", display="Comprehensive Metabolic Panel (14)"),
        ], docs=[hp(procedure="colectomy"), consent(procedure="colectomy")]),
    ),
    case(
        "heldout_07_warfarin_bridge", "READY", [],
        "Warfarin is active. The plan stops it 5 days before (last dose 02/28/2026), bridges with enoxaparin, and "
        "resumes warfarin the evening of surgery. Hold and restart are both specific. Tests that a bridging agent "
        "mentioned in the plan does not get treated as a second untreated anticoagulant.",
        base("heldout_07", meds=[{"name": "warfarin 5 mg PO daily", "active": True}], docs=[hp(), consent(), note(
            "cards-07", "Cardiology Consultation", "2026-02-22",
            "CARDIOLOGY CONSULTATION - PERIOPERATIVE\nHPI: Mechanical mitral valve maintained on warfarin, INR goal 2.5-3.5.\n"
            "RECOMMENDATIONS:\n  1. Stop warfarin 5 days before surgery (last dose 02/28/2026).\n"
            "  2. Begin enoxaparin 1 mg/kg SC BID bridge starting 3 days before surgery; last bridging dose the morning before surgery.\n"
            "  3. Resume warfarin the evening of surgery at the usual dose; continue enoxaparin until INR is therapeutic.\n")]),
    ),
    case(
        "heldout_08_antiplatelet_only", "READY", [],
        "Patient takes aspirin and clopidogrel, no anticoagulant, and there is no perioperative plan. Rule 3 applies "
        "to anticoagulants only, so no plan is required. Tests that antiplatelets are scoped out rather than in.",
        base("heldout_08", meds=[{"name": "aspirin 81 mg PO daily", "active": True}, {"name": "clopidogrel 75 mg PO daily", "active": True}]),
    ),
    case(
        "heldout_09_meniscectomy_consent_wording", "READY", [],
        "Scheduled: 'Arthroscopic partial medial meniscectomy, right knee'. Consent: 'Right knee arthroscopy with "
        "partial meniscectomy', signed. Same operation, different word order. Tests the procedure-noun table.",
        base("heldout_09", procedure="Arthroscopic partial medial meniscectomy, right knee", docs=[
            hp(procedure="right knee arthroscopy with partial meniscectomy"),
            consent(procedure="Right knee arthroscopy with partial meniscectomy"),
        ]),
    ),
    case(
        "heldout_10_patient_states_she_takes_eliquis", "NEEDS_FOLLOW_UP", ["ANTICOAGULATION_MANAGEMENT"],
        "Eliquis is not on the medication list. The nursing intake says 'Pt states she takes Eliquis 5 mg twice "
        "daily'. There is no perioperative plan anywhere. A patient-reported anticoagulant counts as active, so Rule "
        "3 fails. Tests a phrasing of 'patient reports taking' the pattern table does not list.",
        base("heldout_10", docs=[hp(), consent(), note(
            "nurse-10", "Nursing Pre-procedure Assessment", "2026-02-27",
            "PRE-ADMISSION TESTING VISIT\nVisit date: 02/27/2026\nPlanned procedure: carpal tunnel release\n\n"
            "MEDICATION RECONCILIATION (patient-reported):\n  - lisinopril 10 mg PO daily - taking as prescribed\n"
            "  - Pt states she takes Eliquis 5 mg twice daily, prescribed by outside cardiology; not on the reconciled list.\n",
            author="Andrew Carter, RN")]),
    ),
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for c in CASES:
            fh.write(json.dumps(c) + "\n")
    print(f"Wrote {len(CASES)} cases -> {OUT}")


if __name__ == "__main__":
    main()

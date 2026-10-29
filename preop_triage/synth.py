"""Synthetic held-out case generator.

Composes submission packages from randomized scenarios and phrasing banks that
were written from general clinical documentation, not copied from the 50
sample cases. Ground truth comes from the scenario parameters, never from the
engine, so the generator measures generalization rather than memorization.

Two error classes are reported separately:
- fail-open: the system cleared something it should not have (expected
  NOT_CLEARED but got anything else; expected a follow-up but got READY; an
  expected category is missing). These must be zero.
- fail-closed: the system asked for follow-up it did not need (expected READY
  but got NEEDS_FOLLOW_UP; an extra category). This is the recall cost.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

# --- phrasing banks (novel relative to the sample) ----------------------------

HP_TITLES = ["Preoperative History & Physical", "H&P - Surgical", "History and Physical Exam", "Pre-Op H&P", "HISTORY & PHYSICAL", "Hist and Physical", "H & P (pre-surgical)", "Preop History and Physical Note", "History/Physical"]
HP_BODY_MARKERS = [
    "PRE-OPERATIVE EVALUATION for {proc}",
    "Preoperative risk assessment prior to {proc}.",
    "Pre-op evaluation requested by surgery ahead of {proc}.",
    "Surgery planned: {proc} on {pdate_us}.",
    "Scheduled procedure: {proc} ({pdate_us}).",
    "Planned procedure: {proc}",
]
WELLNESS_TITLES = ["History and Physical", "Admission H&P", "H&P", "Clinic H&P", "Annual H&P"]
WELLNESS_BODY = [
    "Encounter type: annual physical / health maintenance.",
    "Reason for visit: new patient, establish care.",
    "Routine wellness visit. No surgical planning discussed.",
    "Encounter: annual wellness / establish care",
]
CONSENT_TITLES = ["Consent", "Surgical Consent Form", "Informed Consent", "Consent for Operation", "Procedure Consent (scanned)", "Consent - Elective Surgery", "Consent Note"]
SIGNED_LINES = [
    "Patient signed the consent form on {d_us} at {t}.",
    "Consent signed by {name} (patient) on {d_us}; witnessed by RN.",
    "Signature: {name} (patient), {d_us} {t}",
    "Patient signature obtained {d_us}.",
    "Written informed consent was obtained and signed by the patient on {d_us}.",
    "Patient has signed and dated the consent ({d_us}).",
    "Signed by patient: {name}, {d_us} {t}. Witness: RN.",
    "Patient signature: /s/ {name} (electronically signed {d_us} {t})",
    "Patient's signature on file (e-signed {d_us}).",
    "Consent executed by patient {d_us}; copy scanned to chart.",
]
UNSIGNED_LINES = [
    "Patient declined to sign at this time; will revisit on day of surgery.",
    "Signature pending - patient reviewing with family.",
    "Awaiting patient signature.",
    "Consent reviewed; form not yet signed.",
    "Patient will sign on the morning of surgery.",
    "Patient signature: ____________   Date: ________",
    "Form sent via patient portal; awaiting completion.",
    "Patient has not signed the consent.",
    "Verbal consent only; written consent to be obtained pre-op.",
    "Consent discussion only. No signature obtained.",
    "Signed by patient: ______________",
    "The form has not been signed by the patient yet.",
]
ATTESTATION = "Physician attestation: risks, benefits and alternatives explained. Electronically signed by {md} on {d_us}."
BOILERPLATE = [
    "Risks and benefits of surgery discussed at a high level; formal consent per surgical team.",
    "ADVANCE DIRECTIVE: not on file; information provided.",
    "NPO after midnight. Hold oral hypoglycemics morning of surgery.",
    "STOP-BANG: 3. No known OSA.",
    "Pain 2/10. Fall risk low. Ride home arranged.",
]
BP_FORMATS = [
    "BP {s}/{d}", "BP: {s}/{d} mmHg", "Blood pressure {s}/{d} mm Hg", "NIBP {s}/{d}", "B/P {s}/{d}", "BP={s}/{d}",
    "{s}/{d} mmHg (seated)", "Blood pressure: {s}/{d}", "BP {s} / {d}", "SBP {s}, DBP {d}",
]
TEMP_F_FORMATS = ["T {f} F", "Temp {f} °F", "Temperature {f} F (oral)", "T={f}", "Tmax {f} F", "{f} °F tympanic", "temp: {f}F", "Temperature: {f} degrees F"]
TEMP_C_FORMATS = ["T {c} C", "Temp {c} °C", "Temperature {c} C (oral)", "T={c} C", "{c} °C", "Temperature: {c} degrees Celsius", "temp {c}C"]
VITALS_DEFERRED = ["VITAL SIGNS: See flowsheet.", "Vitals: refer to nursing flowsheet.", "VS deferred to day of surgery.", "Vital signs not obtained (telephone visit)."]
NOTE_TITLES = ["Pre-Admission Testing Visit", "Nursing Pre-procedure Assessment", "Pre-op Nursing Intake", "PAT Visit Note", "Preoperative Nursing Note", "Anesthesia Pre-Assessment"]
HOLD_LINES = [
    "Hold {brand} for {n} days before surgery (last dose {ld_us}).",
    "Discontinue {generic} {h} hours prior to surgery.",
    "Stop {brand} {n} days pre-op; last dose {ld_us}.",
    "{brand} to be held x{n} days before the procedure.",
    "Last dose of {generic} on {ld_us}; no doses thereafter until cleared.",
    "Withhold {brand} beginning {n} days prior to surgery.",
    "Patient instructed to stop {generic} {n} days before surgery.",
    "{generic} should be held for {h} hours preoperatively.",
]
RESUME_LINES = [
    "Resume {brand} 24-48 hours after surgery once hemostasis is secure.",
    "Restart {generic} on POD1 if no bleeding.",
    "May resume {brand} {h} hours postoperatively.",
    "Reinitiate {generic} the evening of surgery.",
    "Resume {brand} on post-op day 2 at the home dose.",
    "{brand} can be restarted the morning after surgery.",
    "Anticoagulation to be resumed 48 hours post-op.",
    "Resume home dose of {generic} the day after surgery.",
    "Restart {brand} 72h after the procedure pending surgical clearance.",
]
CONTINUE_LINES = [
    "Continue {brand} through surgery without interruption; no doses are to be held before or after the procedure.",
    "Continue {generic} perioperatively; the procedure carries negligible bleeding risk.",
    "{brand} may be continued uninterrupted through the perioperative period.",
    "Do not hold {generic} before or after the procedure.",
]
CONTINUE_VAGUE_LINES = ["Continue {brand}.", "Continue home {generic} {dose}.", "{brand} to be continued."]
DEFERRED_LINES = [
    "Perioperative management of {generic} per the surgical team.",
    "Defer to surgery regarding interruption of {brand}.",
    "Anticoagulation plan TBD; to be discussed with the surgeon.",
    "Will provide specific hold/resume recommendations at a later date.",
    "Timing of {brand} interruption to be determined by the operating surgeon.",
    "Resumption of {generic} will be addressed postoperatively.",
]
TAKING_LINES = [
    "Pt reports taking {brand} {dose}, prescribed by outside cardiology; not on the reconciled med list.",
    "Patient states he takes {generic} {dose} daily at home.",
    "Home medications include {brand} {dose} (not previously documented).",
    "Currently anticoagulated with {generic} {dose}.",
    "On chronic {brand} therapy per patient.",
]
NOT_TAKING_LINES = [
    "{brand} was discontinued by cardiology in 2021; not currently anticoagulated.",
    "Off {generic} since 2020 after a provoked DVT course.",
    "No longer taking {brand}.",
    "{generic} stopped last year; denies any blood thinners now.",
    "Completed 6 months of {generic}, discontinued by hematology.",
]
UNKNOWN_LINES = [
    "Unsure whether still taking {brand}; pharmacy records requested.",
    "Patient unable to confirm if {generic} is still being taken.",
    "{brand}: compliance uncertain; patient could not confirm current use.",
]
ANTICOAGS = [("warfarin", "Coumadin", "5 mg"), ("apixaban", "Eliquis", "5 mg BID"), ("rivaroxaban", "Xarelto", "20 mg"), ("dabigatran", "Pradaxa", "150 mg BID"), ("edoxaban", "Savaysa", "60 mg"), ("enoxaparin", "Lovenox", "40 mg SC daily"), ("fondaparinux", "Arixtra", "2.5 mg SC daily")]
ANTIPLATELETS = ["aspirin 81 mg PO daily", "ASA 325 mg daily", "clopidogrel 75 mg daily", "Plavix 75 mg", "ticagrelor 90 mg BID", "dipyridamole 75 mg TID"]
OTHER_MEDS = ["lisinopril 10 mg daily", "metformin 500 mg BID", "atorvastatin 20 mg nightly", "levothyroxine 50 mcg daily", "omeprazole 20 mg daily", "amlodipine 5 mg daily", "sertraline 50 mg daily", "gabapentin 300 mg TID"]
PROCEDURES = [
    ("LOW", "Elective cataract extraction", "cataract extraction"),
    ("LOW", "Elective carpal tunnel release", "carpal tunnel release"),
    ("LOW", "Elective laparoscopic cholecystectomy", "laparoscopic cholecystectomy"),
    ("LOW", "Elective inguinal hernia repair", "inguinal hernia repair"),
    ("LOW", "Elective septoplasty", "septoplasty"),
    ("MODERATE", "Elective total knee arthroplasty", "total knee arthroplasty"),
    ("MODERATE", "Elective lumbar laminectomy", "lumbar laminectomy"),
    ("MODERATE", "Elective thyroidectomy", "thyroidectomy"),
    ("MODERATE", "Elective hysterectomy", "total abdominal hysterectomy"),
    ("MODERATE", "Elective mastectomy", "mastectomy"),
    ("MODERATE", "Elective shoulder arthroscopy", "shoulder arthroscopy"),
    ("HIGH", "Elective coronary artery bypass grafting", "coronary artery bypass grafting"),
    ("HIGH", "Elective abdominal aortic aneurysm repair", "abdominal aortic aneurysm repair"),
    ("HIGH", "Elective esophagectomy", "esophagectomy"),
    ("HIGH", "Elective Whipple procedure", "pancreaticoduodenectomy (Whipple)"),
    ("HIGH", "Elective hepatic lobectomy", "hepatic lobectomy"),
]
WRONG_PROC = [("Elective thyroidectomy", "knee arthroscopy"), ("Elective cataract extraction", "coronary artery bypass grafting"), ("Elective inguinal hernia repair", "tonsillectomy"), ("Elective mastectomy", "laparoscopic cholecystectomy")]
LAB_CODE_VARIANTS = {"CBC": ["CBC", "LAB-CBC", "CBC W DIFF", "cbc", "85025"], "CMP": ["CMP", "LAB-CMP", "CMP14", "Comprehensive Metabolic Panel"], "BMP": ["BMP", "LAB-BMP", "Basic Metabolic Panel"]}
LAB_DISPLAY = {"CBC": ["Complete Blood Count", "CBC with differential", "CBC (Complete Blood Count)"], "CMP": ["Comprehensive Metabolic Panel", "CMP (14 analytes)"], "BMP": ["Basic Metabolic Panel", "BMP"]}
NAMES = ["Ana Torres", "Wei Zhang", "Priya Nair", "Omar Haddad", "Grace Kim", "Luis Ortega", "Fatima Rahman", "Noah Fischer"]
MDS = ["R. Patel, MD", "S. Okafor, DO", "L. Nguyen, MD", "J. Weiss, MD"]
RNS = ["K. Alvarez, RN", "M. Brown, BSN, RN", "T. Osei, RN"]

HP_OUTCOMES = ["ok", "ok", "ok", "boundary_ok", "boundary_fail", "outdated", "missing", "wellness_only"]
CONSENT_OUTCOMES = ["signed", "signed", "signed", "unsigned", "missing", "wrong_procedure", "superseded"]
LAB_OUTCOMES = ["ok", "ok", "ok", "boundary_ok", "boundary_fail", "outdated", "missing", "eie_fallback_ok", "eie_fallback_outdated", "bmp_only"]
ANTICOAG_OUTCOMES = ["none", "none", "antiplatelet", "full_plan", "full_plan", "no_plan", "hold_only", "deferred", "stale", "inactive", "patient_reported_no_plan", "patient_reported_plan", "unknown_status", "continue_plan", "continue_vague"]
VITAL_OUTCOMES = ["normal", "normal", "normal", "bp_text_excl", "bp_struct_excl", "recheck_clears", "recheck_still_high", "temp_c_excl", "temp_c_borderline_ok", "temp_f_excl", "missing_temp", "missing_all", "flowsheet_struct_ok", "dbp_exact_110", "sbp_exact_180", "temp_exact_100_4", "old_high_newer_normal"]
DATE_OUTCOMES = ["ok"] * 9 + ["missing"]
RISK_OUTCOMES = ["known"] * 8 + ["missing", "unknown_string"]


@dataclass
class Scenario:
    seed: int
    hp: str
    consent: str
    labs: str
    anticoag: str
    vitals: str
    date: str
    risk: str
    structured_c: bool
    laterality: bool
    expected_decision: str = ""
    expected_categories: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _us(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _iso(d: date, hour: int = 9, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doc(dtype: str, d: date, author: str, text: str) -> dict[str, Any]:
    return {"doc_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{dtype}|{d}|{text[:40]}")), "type": dtype, "date": d.isoformat(), "author": author, "text": text}


def generate(seed: int) -> tuple[dict[str, Any], Scenario]:
    rng = random.Random(seed)
    sc = Scenario(
        seed=seed,
        hp=rng.choice(HP_OUTCOMES),
        consent=rng.choice(CONSENT_OUTCOMES),
        labs=rng.choice(LAB_OUTCOMES),
        anticoag=rng.choice(ANTICOAG_OUTCOMES),
        vitals=rng.choice(VITAL_OUTCOMES),
        date=rng.choice(DATE_OUTCOMES),
        risk=rng.choice(RISK_OUTCOMES),
        structured_c=rng.random() < 0.3,
        laterality=rng.random() < 0.3,
    )
    risk, ptype, pnoun = rng.choice(PROCEDURES)
    if sc.labs == "bmp_only":
        risk, ptype, pnoun = rng.choice([p for p in PROCEDURES if p[0] == "HIGH"])
    side = rng.choice(["left", "right"]) if sc.laterality and any(w in pnoun for w in ("knee", "shoulder", "carpal", "cataract", "hernia", "mastectomy")) else None
    if side:
        ptype = ptype.replace("Elective ", f"Elective {side} ")
        pnoun = f"{side} {pnoun}"
    if sc.date == "missing":
        sc.hp = rng.choice(["ok", "missing"])
        sc.labs = rng.choice(["ok", "missing"])
    if sc.risk != "known":
        sc.labs = "ok"
    pdate = date(2026, rng.randint(2, 6), rng.randint(1, 28))
    received = datetime(pdate.year, pdate.month, pdate.day, 14, 22)
    hp_window, lab_window = 30, (14 if risk == "HIGH" else 30)
    name, md, rn = rng.choice(NAMES), rng.choice(MDS), rng.choice(RNS)
    docs: list[dict[str, Any]] = []
    vitals: list[dict[str, Any]] = []
    labs: list[dict[str, Any]] = []
    meds: list[dict[str, Any]] = [{"name": m, "active": True} for m in rng.sample(OTHER_MEDS, 3)]
    cats: set[str] = set()
    not_cleared = False

    # --- H&P ---------------------------------------------------------------------
    def hp_doc(days_before: int) -> dict[str, Any]:
        d = pdate - timedelta(days=days_before)
        marker = rng.choice(HP_BODY_MARKERS).format(proc=pnoun, pdate_us=_us(pdate) if sc.date == "ok" else "TBD")
        body = f"{rng.choice(['PREOPERATIVE HISTORY AND PHYSICAL', 'HISTORY AND PHYSICAL', 'PRE-OP H&P'])}\nDate of service: {_us(d)}\n{marker}\nHPI: {name} presents for pre-operative evaluation. Denies recent illness.\nPMH: reviewed.\nMEDICATIONS: reviewed and reconciled.\nEXAM: well appearing. Heart RRR. Lungs clear.\n{rng.choice(BOILERPLATE)}\nASSESSMENT/PLAN: Acceptable risk for planned {pnoun}."
        return _doc(rng.choice(HP_TITLES), d, md, body)

    wellness = _doc(rng.choice(WELLNESS_TITLES), pdate - timedelta(days=rng.randint(200, 400)), md, f"HISTORY AND PHYSICAL\n{rng.choice(WELLNESS_BODY)}\nHPI: no acute complaints. Referred to surgery for evaluation.\nPLAN: routine health maintenance.")
    if sc.hp == "ok":
        docs.append(hp_doc(rng.randint(3, 25)))
    elif sc.hp == "boundary_ok":
        docs.append(hp_doc(hp_window))
    elif sc.hp == "boundary_fail":
        docs.append(hp_doc(hp_window + 1)); cats.add("REQUIRED_DOCUMENTATION")
    elif sc.hp == "outdated":
        docs.append(hp_doc(rng.randint(35, 90))); cats.add("REQUIRED_DOCUMENTATION")
    elif sc.hp == "missing":
        cats.add("REQUIRED_DOCUMENTATION")
    elif sc.hp == "wellness_only":
        cats.add("REQUIRED_DOCUMENTATION")
    if sc.hp in ("wellness_only",) or rng.random() < 0.5:
        docs.append(wellness)

    # --- consent -----------------------------------------------------------------
    def consent_doc(days_before: int, signed: bool, proc_line: str) -> dict[str, Any]:
        d = pdate - timedelta(days=days_before)
        sig = rng.choice(SIGNED_LINES if signed else UNSIGNED_LINES).format(d_us=_us(d), t=f"{rng.randint(8, 16):02d}:{rng.randint(0, 59):02d}", name=name)
        body = f"INFORMED CONSENT FOR SURGICAL PROCEDURE\nProcedure: {proc_line}\nSurgeon: {md}\nI understand the risks, benefits and alternatives as explained to me.\nAnesthesia: I consent to anesthesia as deemed appropriate.\n{sig}\n{ATTESTATION.format(md=md, d_us=_us(d))}"
        return _doc(rng.choice(CONSENT_TITLES), d, md, body)

    if sc.consent == "signed":
        docs.append(consent_doc(rng.randint(1, 20), True, pnoun))
    elif sc.consent == "unsigned":
        docs.append(consent_doc(rng.randint(1, 20), False, pnoun)); cats.add("REQUIRED_DOCUMENTATION")
    elif sc.consent == "missing":
        cats.add("REQUIRED_DOCUMENTATION")
    elif sc.consent == "wrong_procedure":
        if side:
            other = pnoun.replace(side, "right" if side == "left" else "left")
        else:
            other = next((w for p, w in WRONG_PROC if p == ptype), "tonsillectomy")
        docs.append(consent_doc(rng.randint(1, 20), True, other)); cats.add("REQUIRED_DOCUMENTATION")
    elif sc.consent == "superseded":
        docs.append(consent_doc(rng.randint(10, 20), False, pnoun))
        docs.append(consent_doc(rng.randint(1, 9), True, pnoun))

    # --- labs --------------------------------------------------------------------
    def lab(code: str, days_before: int, status: str = "final") -> dict[str, Any]:
        d = pdate - timedelta(days=days_before)
        return {"id": str(uuid.uuid4()), "code": rng.choice(LAB_CODE_VARIANTS[code]), "display": rng.choice(LAB_DISPLAY[code]), "effective_at": rng.choice([_iso(d, 8, 10), d.isoformat()]), "status": status, "source": "Lab"}

    required = ["CBC", "CMP"] if risk == "HIGH" else ["CBC"]
    labs.append(lab("TSH" if False else "BMP", rng.randint(1, 20)))  # distractor BMP
    if sc.labs == "ok":
        labs += [lab(c, rng.randint(1, lab_window - 1)) for c in required]
    elif sc.labs == "boundary_ok":
        labs += [lab(c, lab_window) for c in required]
    elif sc.labs == "boundary_fail":
        labs += [lab("CBC", lab_window + 1)] + [lab(c, 2) for c in required if c != "CBC"]; cats.add("REQUIRED_TESTING")
    elif sc.labs == "outdated":
        labs += [lab("CBC", lab_window + rng.randint(5, 40))] + [lab(c, 2) for c in required if c != "CBC"]; cats.add("REQUIRED_TESTING")
    elif sc.labs == "missing":
        labs += [lab(c, 2) for c in required if c != "CBC"]; cats.add("REQUIRED_TESTING")
    elif sc.labs == "eie_fallback_ok":
        labs += [lab("CBC", 1, "entered-in-error"), lab("CBC", rng.randint(2, lab_window - 1))] + [lab(c, 2) for c in required if c != "CBC"]
    elif sc.labs == "eie_fallback_outdated":
        labs += [lab("CBC", 1, "entered-in-error"), lab("CBC", lab_window + 10)] + [lab(c, 2) for c in required if c != "CBC"]; cats.add("REQUIRED_TESTING")
    elif sc.labs == "bmp_only":
        labs += [lab("CBC", 3)]; cats.add("REQUIRED_TESTING")  # HIGH risk needs CMP; only BMP present
    if sc.risk != "known":
        cats.discard("REQUIRED_TESTING")

    # --- anticoagulation ---------------------------------------------------------
    generic, brand, dose = rng.choice(ANTICOAGS)
    plan_date = pdate - timedelta(days=rng.randint(5, 20))
    last_dose = pdate - timedelta(days=rng.randint(2, 5))

    def plan_doc(lines: list[str]) -> dict[str, Any]:
        body = f"CARDIOLOGY / ANTICOAGULATION CONSULT\nDate: {_us(plan_date)}\nReason: perioperative anticoagulation management ahead of {pnoun}.\nHPI: maintained on {rng.choice([brand, generic])} {dose} for atrial fibrillation.\nRECOMMENDATIONS:\n" + "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
        return _doc(rng.choice(["Cardiology Consultation", "Anticoagulation Clinic Note", "Perioperative Medicine Consult", "Hematology Consult"]), plan_date, md, body)

    def fmt(line: str) -> str:
        return line.format(brand=brand, generic=generic, n=rng.randint(2, 5), h=rng.choice([24, 48, 72]), ld_us=_us(last_dose), dose=dose)

    nursing_med_lines: list[str] = []
    if sc.anticoag == "antiplatelet":
        meds.append({"name": rng.choice(ANTIPLATELETS), "active": True})
    elif sc.anticoag == "full_plan":
        meds.append({"name": f"{rng.choice([brand, generic])} {dose}", "active": True})
        docs.append(plan_doc([fmt(rng.choice(HOLD_LINES)), fmt(rng.choice(RESUME_LINES))]))
    elif sc.anticoag == "continue_plan":
        meds.append({"name": f"{rng.choice([brand, generic])} {dose}", "active": True})
        docs.append(plan_doc([fmt(rng.choice(CONTINUE_LINES)), "No bridging required."]))
    elif sc.anticoag == "continue_vague":
        meds.append({"name": f"{rng.choice([brand, generic])} {dose}", "active": True})
        docs.append(plan_doc([fmt(rng.choice(CONTINUE_VAGUE_LINES))])); cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "no_plan":
        meds.append({"name": f"{rng.choice([brand, generic])} {dose}", "active": True}); cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "hold_only":
        meds.append({"name": f"{brand} {dose}", "active": True})
        docs.append(plan_doc([fmt(rng.choice(HOLD_LINES)), fmt(rng.choice(DEFERRED_LINES))])); cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "deferred":
        meds.append({"name": f"{generic} {dose}", "active": True})
        docs.append(plan_doc([fmt(rng.choice(DEFERRED_LINES))])); cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "stale":
        g2, b2, d2 = rng.choice([a for a in ANTICOAGS if a[0] != generic])
        meds.append({"name": f"{generic} {dose}", "active": True})
        meds.append({"name": f"{b2} {d2}", "active": False})
        docs.append(plan_doc([rng.choice(HOLD_LINES).format(brand=b2, generic=g2, n=3, h=48, ld_us=_us(last_dose), dose=d2), rng.choice(RESUME_LINES).format(brand=b2, generic=g2, n=3, h=48, ld_us=_us(last_dose), dose=d2)]))
        nursing_med_lines.append(f"Note: patient switched from {b2} to {generic} last month per cardiology.")
        cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "inactive":
        meds.append({"name": f"{generic} ({brand}) {dose}", "active": False})
        nursing_med_lines.append(fmt(rng.choice(NOT_TAKING_LINES)))
    elif sc.anticoag == "patient_reported_no_plan":
        nursing_med_lines.append(fmt(rng.choice(TAKING_LINES))); cats.add("ANTICOAGULATION_MANAGEMENT")
    elif sc.anticoag == "patient_reported_plan":
        nursing_med_lines.append(fmt(rng.choice(TAKING_LINES)))
        docs.append(plan_doc([fmt(rng.choice(HOLD_LINES)), fmt(rng.choice(RESUME_LINES))]))
    elif sc.anticoag == "unknown_status":
        meds.append({"name": f"{brand} {dose}", "active": None})
        nursing_med_lines.append(fmt(rng.choice(UNKNOWN_LINES))); cats.add("MISSING_REQUIRED_DATA")

    # --- vitals ------------------------------------------------------------------
    def struct_bp(s: int, d: int, when: datetime) -> dict[str, Any]:
        return {"type": rng.choice(["blood_pressure", "blood_pressure", "BP", "bp"]), "systolic": s, "diastolic": d, "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "Clinic"}

    def struct_temp(f: float, when: datetime) -> dict[str, Any]:
        if sc.structured_c:
            c = round((f - 32) * 5 / 9, 1)
            return {"type": rng.choice(["temperature", "temp"]), "value_c": c, "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "Clinic"}
        return {"type": rng.choice(["temperature", "temp"]), "value_f": f, "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "Clinic"}

    def bp_text(s: int, d: int) -> str:
        return rng.choice(BP_FORMATS).format(s=s, d=d)

    def temp_text(f: float | None = None, c: float | None = None) -> str:
        if c is not None:
            return rng.choice(TEMP_C_FORMATS).format(c=c)
        return rng.choice(TEMP_F_FORMATS).format(f=f)

    def nursing_doc(days_before: int, vital_lines: list[str], extra: list[str] | None = None) -> dict[str, Any]:
        d = pdate - timedelta(days=days_before)
        med_section = "\n".join(f"  - {m['name']} - taking as prescribed" for m in meds if m.get("active")) + ("\n  - " + "\n  - ".join(nursing_med_lines) if nursing_med_lines else "")
        body = f"PRE-OP NURSING ASSESSMENT\nVisit date: {_us(d)}\nPlanned procedure: {pnoun}\n\nVITAL SIGNS:\n" + "\n".join(f"  {l}" for l in vital_lines) + f"\n\nMEDICATION RECONCILIATION (patient-reported):\n{med_section}\n\n{rng.choice(BOILERPLATE)}\n" + ("\n".join(extra or []))
        return _doc(rng.choice(NOTE_TITLES), d, rn, body)

    early = datetime(pdate.year, pdate.month, pdate.day, 9, 5) - timedelta(days=rng.randint(6, 12))
    late_days = rng.randint(1, 4)
    late = datetime(pdate.year, pdate.month, pdate.day, 0, 0) - timedelta(days=late_days)
    normal_bp = (rng.randint(105, 139), rng.randint(60, 85))
    normal_f = round(rng.uniform(97.0, 99.4), 1)
    v = sc.vitals
    if v == "normal":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"{late.replace(hour=8, minute=15).strftime('%H:%M')}  {bp_text(*normal_bp)}, HR 72, {temp_text(f=normal_f)}, SpO2 97% RA"]))
    elif v == "bp_text_excl":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        s, d = rng.choice([(rng.randint(180, 205), rng.randint(90, 105)), (rng.randint(150, 175), rng.randint(110, 125))])
        docs.append(nursing_doc(late_days, [f"08:15  {bp_text(s, d)}, HR 80, {temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "bp_struct_excl":
        s, d = rng.randint(181, 210), rng.randint(95, 125)
        latest = late.replace(hour=10, minute=30)  # strictly after the nursing note below
        vitals += [struct_bp(*normal_bp, early), struct_bp(s, d, latest), struct_temp(normal_f, latest)]
        docs.append(nursing_doc(late_days + 2, [f"{bp_text(*normal_bp)}, {temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "recheck_clears":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"08:06  {bp_text(rng.randint(182, 195), rng.randint(100, 115))}, HR 88, {temp_text(f=normal_f)}. See addendum."], [f"ADDENDUM 08:31: Recheck after 25 minutes of rest: {bp_text(*normal_bp)}, HR 74."]))
    elif v == "recheck_still_high":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"On arrival at 08:02: {bp_text(rng.randint(190, 205), rng.randint(100, 112))}. Rechecked at 08:35 after rest: {bp_text(rng.randint(181, 189), rng.randint(90, 104))}.", f"{temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "temp_c_excl":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        c = rng.choice([38.1, 38.4, 38.6, 39.0, 38.9])
        docs.append(nursing_doc(late_days, [f"08:10  {bp_text(*normal_bp)}, {temp_text(c=c)}, HR 96. Reports chills since yesterday."])); not_cleared = True
    elif v == "temp_c_borderline_ok":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        c = rng.choice([37.8, 37.9, 38.0])  # 100.0, 100.2, 100.4 F: none exceed 100.4
        docs.append(nursing_doc(late_days, [f"08:10  {bp_text(*normal_bp)}, {temp_text(c=c)}, HR 76"]))
    elif v == "temp_f_excl":
        f = rng.choice([100.5, 100.9, 101.3, 102.0])
        latest = late.replace(hour=10, minute=30)
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early), struct_temp(f, latest)]
        docs.append(nursing_doc(late_days + 2, [f"{bp_text(*normal_bp)}, {temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "missing_temp":
        vitals += [struct_bp(*normal_bp, early)]
        docs.append(nursing_doc(late_days, [f"08:15  {bp_text(*normal_bp)}, HR 72, SpO2 98%"])); cats.add("MISSING_REQUIRED_DATA")
    elif v == "missing_all":
        docs.append(nursing_doc(late_days, [rng.choice(VITALS_DEFERRED)])); cats.add("MISSING_REQUIRED_DATA")
    elif v == "flowsheet_struct_ok":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [rng.choice(VITALS_DEFERRED)]))
    elif v == "dbp_exact_110":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"08:15  {bp_text(rng.randint(140, 160), 110)}, {temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "sbp_exact_180":
        latest = late.replace(hour=10, minute=30)
        vitals += [struct_bp(180, rng.randint(80, 95), latest), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days + 2, [f"{bp_text(*normal_bp)}, {temp_text(f=normal_f)}"])); not_cleared = True
    elif v == "temp_exact_100_4":
        vitals += [struct_bp(*normal_bp, early), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"08:15  {bp_text(*normal_bp)}, {temp_text(f=100.4)}"]))
    elif v == "old_high_newer_normal":
        vitals += [struct_bp(rng.randint(185, 199), rng.randint(110, 118), early - timedelta(days=3)), struct_temp(normal_f, early)]
        docs.append(nursing_doc(late_days, [f"08:15  {bp_text(*normal_bp)}, {temp_text(f=normal_f)}"]))
    if not_cleared:
        cats.add("ACUTE_SAFETY_EXCLUSION")

    # --- date / risk ---------------------------------------------------------------
    proc_date_value: str | None = rng.choice([pdate.isoformat(), _iso(pdate, 7, 30)])
    if sc.date == "missing":
        proc_date_value = None; cats.add("MISSING_REQUIRED_DATA")
    risk_value: str | None = rng.choice([risk, risk.lower(), risk.capitalize()])
    if sc.risk == "missing":
        risk_value = None; cats.add("MISSING_REQUIRED_DATA")
    elif sc.risk == "unknown_string":
        risk_value = rng.choice(["Intermediate", "MEDIUM", "unknown", "TBD"]); cats.add("MISSING_REQUIRED_DATA")

    # distractors
    docs.append(_doc(rng.choice(["Nutrition Consult", "Patient Portal Message", "Physical Therapy Evaluation", "Social Work Note"]), pdate - timedelta(days=rng.randint(2, 30)), md, rng.choice(["Dietary counseling provided; no barriers identified.", "Patient asks about parking and visitor hours. Reply sent.", "Home exercise program reviewed.", "Discharge planning: family support available."])))
    rng.shuffle(docs); rng.shuffle(vitals); rng.shuffle(labs)

    sc.expected_decision = "NOT_CLEARED" if not_cleared else ("NEEDS_FOLLOW_UP" if cats else "READY")
    sc.expected_categories = sorted(cats)
    submission = {
        "patient": {"id": str(uuid.uuid4()), "mrn": f"MRN-{seed:07d}", "name": {"given": name.split()[0], "family": name.split()[1]}, "dob": "1960-01-01", "sex": rng.choice(["F", "M"])},
        "procedure": {"case_id": f"synth_{seed:05d}", "procedure_type": ptype, "procedure_risk": risk_value, "procedure_date": proc_date_value, "is_elective": True, "location": "Cadence Surgical Center - Main OR"},
        "vitals": vitals,
        "labs": labs,
        "medications": meds,
        "conditions": [{"name": "Atrial fibrillation", "active": True}] if sc.anticoag not in ("none", "antiplatelet") else [],
        "documents": docs,
        "metadata": {"submission_received_at": received.strftime("%Y-%m-%dT%H:%M:%SZ"), "source_system": "synthetic"},
    }
    return submission, sc


def classify_error(expected_decision: str, expected_cats: list[str], actual_decision: str, actual_cats: list[str]) -> str:
    """'ok', 'fail_open', or 'fail_closed'."""
    exp, act = set(expected_cats), set(actual_cats)
    if expected_decision == actual_decision and exp == act:
        return "ok"
    if expected_decision == "NOT_CLEARED" and actual_decision != "NOT_CLEARED":
        return "fail_open"
    if expected_decision != "READY" and actual_decision == "READY":
        return "fail_open"
    if exp - act:
        return "fail_open"
    return "fail_closed"

from __future__ import annotations

import pytest

from preop_triage.classify import classify, classify_doc_kind, procedure_matches
from preop_triage.model import Span, SpanKind
from preop_triage.policy import load_policy

POLICY = load_policy()


def _span(kind: SpanKind, quote: str, model_label: str | None = None) -> Span:
    return Span(kind=kind, doc_id="d", quote=quote, offset=0, model_label=model_label)


def test_hp_typo_title_is_preop_hp() -> None:
    assert classify_doc_kind("History and Pyhsical", "PREOPERATIVE HISTORY AND PHYSICAL\nPlanned procedure: x", POLICY) == "PREOP_HP"


def test_hp_pdf_title_is_preop_hp() -> None:
    assert classify_doc_kind("H&P [PDF]", "PREOPERATIVE HISTORY AND PHYSICAL", POLICY) == "PREOP_HP"


def test_wellness_hp_is_non_qualifying() -> None:
    assert classify_doc_kind("Admission H&P", "HISTORY AND PHYSICAL\nEncounter: annual wellness / establish care", POLICY) == "NON_QUALIFYING_HP"


def test_hp_title_without_body_marker_is_non_qualifying() -> None:
    assert classify_doc_kind("History and Physical", "Prior pre-op H&P retained for longitudinal chart context.", POLICY) == "PREOP_HP"
    assert classify_doc_kind("Clinic H&P", "Routine visit. No surgical planning.", POLICY) == "NON_QUALIFYING_HP"


@pytest.mark.parametrize("title", ["Surgery Consent (scanned)", "Pre-op Consent Discussion", "Informed Consent - Surgical Procedure", "Procedure Consent Form"])
def test_consent_titles(title: str) -> None:
    assert classify_doc_kind(title, "INFORMED CONSENT FOR SURGICAL PROCEDURE", POLICY) == "CONSENT"


def test_other_doc() -> None:
    assert classify_doc_kind("Nutrition Consult", "Dietary counseling provided.", POLICY) == "OTHER"


@pytest.mark.parametrize(
    "quote",
    [
        "Patient signature on file - signed in clinic 03/13/2026 12:06 (scanned copy attached).",
        "Electronic consent obtained and signed by patient for procedure.",
        "Patient signature: /s/ Mia Patel (electronically signed 03/08/2026 09:12)",
        "Signed by patient: Ava Patel, 02/19/2026 10:00. Witness: Christopher Rodriguez.",
    ],
)
def test_signature_signed(quote: str) -> None:
    label, flag = classify(_span(SpanKind.CONSENT_SIGNATURE, quote), POLICY)
    assert label == "SIGNED" and flag is None


@pytest.mark.parametrize(
    "quote",
    [
        "Patient signature: pending",
        "E-signature request sent to patient portal 02/24/2026 09:00; status: NOT COMPLETED as of this note.",
        "[Form printed for patient review; not yet signed]",
        "Patient signature: ______________________   Date/Time: __________",
    ],
)
def test_signature_unsigned(quote: str) -> None:
    label, flag = classify(_span(SpanKind.CONSENT_SIGNATURE, quote), POLICY)
    assert label == "UNSIGNED" and flag is None


def test_physician_attestation_is_not_patient_signature() -> None:
    label, flag = classify(_span(SpanKind.CONSENT_SIGNATURE, "Electronically signed by Amanda Wong, DO on 03/13/2026."), POLICY)
    assert label is None and flag is not None and flag.reason == "NO_PATTERN_MATCH"


def test_hold_specific() -> None:
    label, _ = classify(_span(SpanKind.ANTICOAG_HOLD, "Hold Xarelto for the 2 days before surgery (last dose 02/26/2026"), POLICY)
    assert label == "SPECIFIC"


def test_hold_vague() -> None:
    label, _ = classify(_span(SpanKind.ANTICOAG_HOLD, "Hold warfarin per surgical team"), POLICY)
    assert label == "VAGUE"


@pytest.mark.parametrize(
    "quote",
    ["Resume Xarelto 24-48 hours after surgery once hemostasis is secure", "Restart dabigatran on postoperative day 1 at the full dose", "resume warfarin the evening of surgery"],
)
def test_resume_specific(quote: str) -> None:
    label, _ = classify(_span(SpanKind.ANTICOAG_RESUME, quote), POLICY)
    assert label == "SPECIFIC"


@pytest.mark.parametrize(
    "quote",
    ["Periprocedural management of dabigatran to be determined by the surgical team", "we will follow up with specific recommendations", "Resumption will be addressed after surgery"],
)
def test_deferred(quote: str) -> None:
    label, _ = classify(_span(SpanKind.ANTICOAG_DEFERRED, quote), POLICY)
    assert label == "DEFERRED"


def test_model_disagreement_flags() -> None:
    label, flag = classify(_span(SpanKind.CONSENT_SIGNATURE, "Patient signature: pending", model_label="SIGNED"), POLICY)
    assert label is None
    assert flag is not None and flag.reason == "MODEL_CODE_DISAGREE"
    assert flag.model_label == "SIGNED" and flag.code_label == "UNSIGNED"


def test_model_agreement_passes() -> None:
    label, flag = classify(_span(SpanKind.CONSENT_SIGNATURE, "Patient signature: pending", model_label="UNSIGNED"), POLICY)
    assert label == "UNSIGNED" and flag is None


def test_med_mention_labels() -> None:
    assert classify(_span(SpanKind.MED_MENTION, "Patient also reports taking Eliquis 5 mg (BID)"), POLICY)[0] == "TAKING"
    assert classify(_span(SpanKind.MED_MENTION, "Discontinued: warfarin (Coumadin)"), POLICY)[0] == "NOT_TAKING"
    assert classify(_span(SpanKind.MED_MENTION, "Coumadin: patient unable to confirm whether still taking"), POLICY)[0] == "UNKNOWN"


@pytest.mark.parametrize(
    "scheduled,consent,expected",
    [
        ("Elective coronary artery bypass grafting", "coronary artery bypass grafting", True),
        ("Elective thyroidectomy", "coronary artery bypass grafting", False),
        ("Elective total knee arthroplasty", "total knee arthroplasty", True),
        ("Elective Roux-en-Y gastric bypass", "coronary artery bypass grafting", False),
        ("Elective inguinal hernia repair", "inguinal hernia repair", True),
    ],
)
def test_procedure_match(scheduled: str, consent: str, expected: bool) -> None:
    assert procedure_matches(scheduled, consent, POLICY) is expected


def test_procedure_match_unlisted_nouns_fall_back_to_words() -> None:
    assert procedure_matches("Elective frobnication", "frobnication", POLICY) is True
    assert procedure_matches("Elective frobnication", "widget removal", POLICY) is False
    assert procedure_matches("Elective", "", POLICY) is None


def test_med_mention_stripped_quote_still_classifies() -> None:
    assert classify(_span(SpanKind.MED_MENTION, "warfarin, discontinued by"), POLICY)[0] == "NOT_TAKING"

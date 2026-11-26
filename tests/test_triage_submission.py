from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import PatientSubmission, TriageOutput, triage_submission

ROOT = Path(__file__).resolve().parent.parent
FIRST = json.loads((ROOT / "data" / "patients_sample_50.jsonl").read_text().splitlines()[0])["submission"]


def test_triage_submission_returns_structured_output_offline() -> None:
    out = triage_submission(FIRST, model="unused", extractor="rules")
    assert isinstance(out, TriageOutput)
    assert out.decision in ("READY", "NEEDS_FOLLOW_UP", "NOT_CLEARED")
    assert out.provenance.extractor == "rules"
    assert all(i.category for i in out.issues)


def test_triage_submission_accepts_validated_model() -> None:
    out = triage_submission(PatientSubmission.model_validate(FIRST), model="unused", extractor="rules")
    assert out.decision == triage_submission(FIRST, model="unused", extractor="rules").decision


def test_unknown_extractor_rejected() -> None:
    with pytest.raises(ValueError):
        triage_submission(FIRST, model="unused", extractor="magic")


def test_output_schema_rejects_bad_decision() -> None:
    with pytest.raises(ValidationError):
        TriageOutput.model_validate({"decision": "MAYBE", "issues": [], "explanation": ""})

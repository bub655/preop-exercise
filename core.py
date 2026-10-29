"""Public entry point and submission schema for the pre-op triage exercise.

`triage_submission` keeps the starter signature. The implementation lives in
the `preop_triage` package: a deterministic, grounded pipeline where a model
(optionally) locates text and code makes every decision.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from preop_triage.model import Decision, Issue, IssueCategory, ProcedureRisk, TriageOutput
from preop_triage.pipeline import make_extractor, run
from preop_triage.policy import Policy, load_policy

__all__ = [
    "Decision",
    "IssueCategory",
    "ProcedureRisk",
    "PatientSubmission",
    "TriageOutput",
    "TriageIssue",
    "triage_submission",
]

TriageIssue = Issue

ISSUE_CATEGORIES: list[str] = [
    "REQUIRED_DOCUMENTATION",
    "REQUIRED_TESTING",
    "ANTICOAGULATION_MANAGEMENT",
    "ACUTE_SAFETY_EXCLUSION",
    "MISSING_REQUIRED_DATA",
]


# -------------------------
# Submission schema (unchanged shape from the starter)
# -------------------------


class PatientName(BaseModel):
    given: str | None = None
    family: str | None = None


class PatientInfo(BaseModel):
    id: str | None = None
    mrn: str | None = None
    name: PatientName | None = None
    dob: str | None = None
    sex: str | None = None


class ProcedureInfo(BaseModel):
    case_id: str | None = None
    procedure_type: str | None = None
    procedure_risk: str | None = None  # normalized and validated by the engine; unknown values are missing data
    procedure_date: str | None = None
    is_elective: bool | None = None
    location: str | None = None


class BloodPressureVital(BaseModel):
    type: str | None = None
    systolic: float | int | None = None
    diastolic: float | int | None = None
    date: str | None = None
    source: str | None = None


class TemperatureVital(BaseModel):
    type: str | None = None
    value_f: float | int | None = None
    date: str | None = None
    source: str | None = None


class GenericVital(BaseModel):
    type: str | None = None
    date: str | None = None
    source: str | None = None


Vital = BloodPressureVital | TemperatureVital | GenericVital


class LabResult(BaseModel):
    id: str | None = None
    code: str | None = None
    display: str | None = None
    effective_at: str | None = None
    status: str | None = None
    source: str | None = None


class Medication(BaseModel):
    name: str | None = None
    active: bool | None = None


class Condition(BaseModel):
    name: str | None = None
    active: bool | None = None


class Document(BaseModel):
    doc_id: str | None = None
    type: str | None = None
    date: str | None = None
    author: str | None = None
    text: str | None = None


class SubmissionMetadata(BaseModel):
    submission_received_at: str | None = None
    source_system: str | None = None


class PatientSubmission(BaseModel):
    """Single submission package shape from the take-home prompt."""

    patient: PatientInfo | None = None
    procedure: ProcedureInfo | None = None
    vitals: list[Vital] = Field(default_factory=list)
    labs: list[LabResult] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    metadata: SubmissionMetadata | None = None


def triage_output_json_schema() -> dict[str, object]:
    return TriageOutput.model_json_schema()


# -------------------------
# Entry point
# -------------------------

_POLICY: Policy | None = None


def _policy() -> Policy:
    global _POLICY
    if _POLICY is None:
        _POLICY = load_policy()
    return _POLICY


ExtractorName = Literal["rules", "claude", "union"]


def triage_submission(
    submission: dict[str, object] | PatientSubmission,
    *,
    model: str,
    extractor: str = "rules",
    use_cache: bool = True,
    policy: Policy | None = None,
) -> TriageOutput:
    """Triage one submission package.

    extractor: "rules" (offline, deterministic), "claude" (model locates spans,
    cached by document hash), or "union" (both). Every span passes the same
    grounding, parsing, classification, policy, and invariant gates.
    """
    pol = policy or _policy()
    ext = make_extractor(extractor, policy=pol, model=model, use_cache=use_cache)
    if isinstance(submission, PatientSubmission):
        payload: dict[str, object] = submission.model_dump()
    else:
        try:
            payload = PatientSubmission.model_validate(submission).model_dump()
        except ValidationError:
            # Malformed input is still triaged: ingestion flags each bad field and the
            # result fails closed, instead of the caller getting an exception.
            payload = dict(submission)
    return run(payload, policy=pol, extractor=ext)

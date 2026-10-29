"""Core data types shared across the pipeline.

Spans are untrusted model (or rule) output. Facts are trusted, code-derived
values. Everything the engine decides on is a Fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["READY", "NEEDS_FOLLOW_UP", "NOT_CLEARED"]
IssueCategory = Literal[
    "REQUIRED_DOCUMENTATION",
    "REQUIRED_TESTING",
    "ANTICOAGULATION_MANAGEMENT",
    "ACUTE_SAFETY_EXCLUSION",
    "MISSING_REQUIRED_DATA",
]
ProcedureRisk = Literal["LOW", "MODERATE", "HIGH"]


class SpanKind(StrEnum):
    VITAL_BP = "VITAL_BP"
    VITAL_TEMP = "VITAL_TEMP"
    VITALS_DEFERRED = "VITALS_DEFERRED"
    DOC_TITLE = "DOC_TITLE"
    DOC_ENCOUNTER_KIND = "DOC_ENCOUNTER_KIND"
    PLANNED_PROCEDURE = "PLANNED_PROCEDURE"
    CONSENT_PROCEDURE = "CONSENT_PROCEDURE"
    CONSENT_SIGNATURE = "CONSENT_SIGNATURE"
    MED_MENTION = "MED_MENTION"
    ANTICOAG_HOLD = "ANTICOAG_HOLD"
    ANTICOAG_RESUME = "ANTICOAG_RESUME"
    ANTICOAG_DEFERRED = "ANTICOAG_DEFERRED"
    DOC_ABSENCE = "DOC_ABSENCE"
    PROCEDURE_DATE_STATEMENT = "PROCEDURE_DATE_STATEMENT"
    PROCEDURE_RISK_STATEMENT = "PROCEDURE_RISK_STATEMENT"


@dataclass(frozen=True)
class Span:
    """A candidate quote located in a document. Untrusted until grounded."""

    kind: SpanKind
    doc_id: str
    quote: str
    offset: int | None = None
    model_label: str | None = None
    origin: str = "rules"


@dataclass(frozen=True)
class Rejection:
    doc_id: str
    quote: str
    reason: str
    origin: str = "rules"


@dataclass(frozen=True)
class Fact:
    """A trusted, code-derived value with its provenance."""

    kind: str
    value: Any
    observed_at: datetime | None
    source: str
    doc_id: str | None = None
    quote: str | None = None
    parser: str = "structured"


@dataclass(frozen=True)
class ReviewFlag:
    reason: str
    doc_id: str | None = None
    quote: str | None = None
    model_label: str | None = None
    code_label: str | None = None
    rule_id: str | None = None


class Evidence(BaseModel):
    source: str
    doc_id: str | None = None
    date: str | None = None
    author: str | None = None
    quote: str | None = None


class Computed(BaseModel):
    procedure_date: str | None = None
    observed_date: str | None = None
    days_before: int | None = None
    window_days: int | None = None
    threshold: str | None = None
    observed_value: str | None = None
    converted_from: str | None = None


class Check(BaseModel):
    rule_id: str
    status: Literal["PASS", "FAIL", "SKIPPED", "UNVERIFIED"]
    summary: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    computed: Computed = Field(default_factory=Computed)


class Issue(BaseModel):
    category: IssueCategory
    rule_id: str
    status: str
    summary: str
    evidence: list[Evidence] = Field(default_factory=list)
    computed: Computed = Field(default_factory=Computed)
    action_needed: str = ""


class ReviewFlagOut(BaseModel):
    reason: str
    doc_id: str | None = None
    quote: str | None = None
    model_label: str | None = None
    code_label: str | None = None
    rule_id: str | None = None


class Assumption(BaseModel):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)


class RejectionOut(BaseModel):
    doc_id: str
    quote: str
    reason: str
    origin: str


class Provenance(BaseModel):
    policy_version: str = ""
    extractor: str = ""
    model: str | None = None
    prompt_version: str | None = None
    cache_hits: int = 0
    pipeline_version: str = ""


class TriageOutput(BaseModel):
    """Output contract. decision/issues/explanation are the scored core."""

    decision: Decision
    issues: list[Issue] = Field(default_factory=list)
    explanation: str = ""
    checks: list[Check] = Field(default_factory=list)
    review_flags: list[ReviewFlagOut] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    rejections: list[RejectionOut] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)


@dataclass
class EngineResult:
    decision: Decision
    issues: list[Issue] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    review_flags: list[ReviewFlag] = field(default_factory=list)

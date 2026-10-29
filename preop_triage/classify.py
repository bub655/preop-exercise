"""Classification gate: code decides what a grounded quote means.

The model may suggest a label; the pattern tables in the policy decide. A
disagreement or a quote no pattern covers becomes a review flag, never a fact.
"""

from __future__ import annotations

import re

from .model import ReviewFlag, Span, SpanKind
from .policy import Policy

# SpanKind -> pattern group in policy.yaml
GROUP_FOR_KIND: dict[SpanKind, str] = {
    SpanKind.CONSENT_SIGNATURE: "consent_signature",
    SpanKind.ANTICOAG_HOLD: "anticoag_hold",
    SpanKind.ANTICOAG_RESUME: "anticoag_resume",
    SpanKind.ANTICOAG_DEFERRED: "anticoag_deferred",
    SpanKind.MED_MENTION: "med_mention",
    SpanKind.VITALS_DEFERRED: "vitals_deferred",
    SpanKind.DOC_ABSENCE: "doc_absence",
    SpanKind.PROCEDURE_DATE_STATEMENT: "procedure_date_statement",
    SpanKind.PROCEDURE_RISK_STATEMENT: "procedure_risk_statement",
    SpanKind.DOC_ENCOUNTER_KIND: "encounter_kind",
    SpanKind.PLANNED_PROCEDURE: "planned_procedure",
    SpanKind.CONSENT_PROCEDURE: "consent_procedure",
}

# Groups whose labels are ordered most-specific first; the first match wins.
FIRST_MATCH_WINS = {"anticoag_hold", "anticoag_resume"}

VALUE_KINDS = {SpanKind.VITAL_BP, SpanKind.VITAL_TEMP, SpanKind.DOC_TITLE}


def _matching_labels(group: str, text: str, policy: Policy) -> list[str]:
    hits: list[str] = []
    for label in policy.labels(group):
        if any(p.search(text) for p in policy.compiled(group, label)):
            hits.append(label)
    return hits


def classify(span: Span, policy: Policy) -> tuple[str | None, ReviewFlag | None]:
    """Label a grounded span. Returns (label, None) or (None, flag)."""
    if span.kind in VALUE_KINDS:
        return "VALUE", None
    group = GROUP_FOR_KIND.get(span.kind)
    if group is None:
        return None, ReviewFlag("UNKNOWN_SPAN_KIND", span.doc_id, span.quote, span.model_label, None)
    hits = _matching_labels(group, span.quote, policy)
    if not hits:
        return None, ReviewFlag("NO_PATTERN_MATCH", span.doc_id, span.quote, span.model_label, None)
    if len(hits) > 1 and group not in FIRST_MATCH_WINS:
        return None, ReviewFlag("AMBIGUOUS_PATTERN_MATCH", span.doc_id, span.quote, span.model_label, "|".join(hits))
    code_label = hits[0]
    if span.model_label is not None and span.model_label != code_label:
        return None, ReviewFlag("MODEL_CODE_DISAGREE", span.doc_id, span.quote, span.model_label, code_label)
    return code_label, None


def classify_doc_kind(title: str | None, text: str | None, policy: Policy) -> str:
    """PREOP_HP | NON_QUALIFYING_HP | CONSENT | OTHER, from title then body."""
    title = title or ""
    text = text or ""
    title_hits = _matching_labels("doc_title", title, policy)
    if "CONSENT" in title_hits:
        return "CONSENT"
    if "HP" in title_hits:
        body = _matching_labels("doc_body", text, policy)
        if "WELLNESS" in body:
            return "NON_QUALIFYING_HP"
        if "PREOP_HP" in body:
            return "PREOP_HP"
        return "NON_QUALIFYING_HP"
    return "OTHER"


def _nouns_in(text: str, policy: Policy) -> set[str]:
    lowered = text.lower()
    found: set[str] = set()
    for noun in policy.procedure_nouns:  # multi-word nouns listed first in config
        if re.search(rf"\b{re.escape(noun)}\b", lowered):
            # drop any shorter noun that is a substring of this one (e.g. "bypass")
            if not any(noun != f and noun in f for f in found):
                found.add(noun)
    return found


def procedure_matches(scheduled: str | None, consent_line: str | None, policy: Policy) -> bool | None:
    """True/False when both sides name known procedures; None when unverifiable."""
    if not scheduled or not consent_line:
        return None
    a = _nouns_in(scheduled, policy)
    b = _nouns_in(consent_line, policy)
    if not a or not b:
        return None
    return a == b

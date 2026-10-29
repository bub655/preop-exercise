"""Turn grounded spans into trusted Facts via the parse and classify gates.

Timestamps for text facts come from the document date plus the nearest
HH:MM on the same line before the reading. A span that cannot be parsed or
classified becomes a ReviewFlag tagged with the rule it would have fed.
"""

from __future__ import annotations

import re
from datetime import datetime

from .classify import classify, classify_doc_kind
from .ingest import parse_iso
from .model import Doc, Fact, ReviewFlag, Span, SpanKind
from .parsers import UnitConflict, parse_bp, parse_date, parse_temp_f, parse_time
from .policy import Policy

RULE_FOR_KIND: dict[SpanKind, str] = {
    SpanKind.VITAL_BP: "R4",
    SpanKind.VITAL_TEMP: "R4",
    SpanKind.VITALS_DEFERRED: "R4",
    SpanKind.DOC_TITLE: "R1",
    SpanKind.DOC_ENCOUNTER_KIND: "R1",
    SpanKind.PLANNED_PROCEDURE: "R1",
    SpanKind.CONSENT_PROCEDURE: "R1",
    SpanKind.CONSENT_SIGNATURE: "R1",
    SpanKind.DOC_ABSENCE: "R1",
    SpanKind.MED_MENTION: "R3",
    SpanKind.ANTICOAG_HOLD: "R3",
    SpanKind.ANTICOAG_RESUME: "R3",
    SpanKind.ANTICOAG_DEFERRED: "R3",
    SpanKind.PROCEDURE_DATE_STATEMENT: "R0",
    SpanKind.PROCEDURE_RISK_STATEMENT: "R0",
}

_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _observed_at(doc: Doc, span: Span) -> datetime | None:
    base = parse_iso(doc.date)
    if base is None:
        return None
    text = doc.text or ""
    t = None
    if span.offset is not None:
        line_start = text.rfind("\n", 0, span.offset) + 1
        prefix = text[line_start : span.offset + len(span.quote)]
        times = [m for m in _TIME_RE.finditer(prefix) if m.start() < (span.offset - line_start) + len(span.quote)]
        # nearest HH:MM at or before the reading on the same line
        before = [m for m in times if m.start() <= span.offset - line_start + len(span.quote)]
        if before:
            t = parse_time(before[-1].group(0))
    if t is None:
        t = parse_time(span.quote)
    return base.replace(hour=t.hour, minute=t.minute) if t else base


def _flag(span: Span, reason: str, code_label: str | None = None) -> ReviewFlag:
    return ReviewFlag(reason, span.doc_id, span.quote, span.model_label, code_label, RULE_FOR_KIND.get(span.kind))


def _text_fact(kind: str, value: object, span: Span, when: datetime | None, parser: str, meta: dict | None = None) -> Fact:
    return Fact(kind, value, when, f"documents:{span.doc_id}", span.doc_id, span.quote, parser, meta or {})


def spans_to_facts(spans: list[Span], docs: dict[str, Doc], policy: Policy) -> tuple[list[Fact], list[ReviewFlag]]:
    facts: list[Fact] = []
    flags: list[ReviewFlag] = []

    for doc in sorted(docs.values(), key=lambda d: d.index):
        kind = classify_doc_kind(doc.title, doc.text, policy)
        facts.append(Fact("DOC_KIND", kind, parse_iso(doc.date), f"documents[{doc.index}]", doc.doc_id, None, "doc_kind"))

    for span in spans:
        doc = docs.get(span.doc_id)
        if doc is None:
            flags.append(_flag(span, "UNKNOWN_DOC"))
            continue
        when = _observed_at(doc, span)
        if span.kind == SpanKind.VITAL_BP:
            bp = parse_bp(span.quote)
            if bp is None:
                flags.append(_flag(span, "UNPARSEABLE_BP"))
            else:
                facts.append(_text_fact("BP", bp, span, when, "text_bp"))
            continue
        if span.kind == SpanKind.VITAL_TEMP:
            try:
                parsed = parse_temp_f(span.quote)
            except UnitConflict:
                flags.append(_flag(span, "UNIT_CONFLICT"))
                continue
            if parsed is None:
                flags.append(_flag(span, "UNPARSEABLE_TEMP"))
                continue
            temp_f, unit = parsed
            meta = {}
            if unit == "C":
                m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*°?\s*([cC])\b", span.quote)
                meta["converted_from"] = f"{m.group(1)} C" if m else span.quote
            facts.append(_text_fact("TEMP", temp_f, span, when, "text_temp", meta))
            continue

        label, flag = classify(span, policy)
        if flag is not None:
            flags.append(ReviewFlag(flag.reason, flag.doc_id, flag.quote, flag.model_label, flag.code_label, RULE_FOR_KIND.get(span.kind)))
            continue
        if span.kind == SpanKind.CONSENT_SIGNATURE:
            facts.append(_text_fact("CONSENT_SIG", label, span, when, "text"))
        elif span.kind == SpanKind.CONSENT_PROCEDURE:
            line = re.sub(r"^Procedure:\s*", "", span.quote, flags=re.IGNORECASE).strip()
            facts.append(_text_fact("CONSENT_PROC", line, span, when, "text"))
        elif span.kind == SpanKind.MED_MENTION:
            canonical, cls = policy.drugs.classify(span.quote)
            if canonical is None and re.search(r"anticoagul", span.quote, re.IGNORECASE):
                cls = "anticoagulant"
            facts.append(_text_fact("MED_MENTION", {"canonical": canonical, "class": cls, "label": label}, span, when, "text"))
        elif span.kind in (SpanKind.ANTICOAG_HOLD, SpanKind.ANTICOAG_RESUME, SpanKind.ANTICOAG_DEFERRED):
            canonical, _ = policy.drugs.classify(span.quote)
            facts.append(_text_fact(span.kind.value, {"canonical": canonical, "label": label}, span, when, "text"))
        elif span.kind == SpanKind.PROCEDURE_DATE_STATEMENT:
            facts.append(_text_fact("PROCEDURE_DATE_STATEMENT", {"label": label, "date": parse_date(span.quote)}, span, when, "text"))
        else:  # DOC_ABSENCE, VITALS_DEFERRED, PROCEDURE_RISK_STATEMENT, DOC_ENCOUNTER_KIND, PLANNED_PROCEDURE
            facts.append(_text_fact(span.kind.value, label, span, when, "text"))
    return facts, flags

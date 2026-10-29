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
    SpanKind.ANTICOAG_CONTINUE: "R3",
    SpanKind.ANTICOAG_DEFERRED: "R3",
    SpanKind.PROCEDURE_DATE_STATEMENT: "R0",
    SpanKind.PROCEDURE_RISK_STATEMENT: "R0",
}

_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _observed_at(doc: Doc, span: Span) -> datetime | None:
    """Document date plus the nearest HH:MM on the reading's line: preceding first, else following."""
    base = parse_iso(doc.date)
    if base is None:
        return None
    text = doc.text or ""
    t = None
    if span.offset is not None:
        line_start = text.rfind("\n", 0, span.offset) + 1
        line_end = text.find("\n", span.offset)
        line_end = len(text) if line_end < 0 else line_end
        rel_start = span.offset - line_start
        rel_end = rel_start + len(span.quote)
        line = text[line_start:line_end]
        before = [m for m in _TIME_RE.finditer(line) if m.end() <= rel_end]
        after = [m for m in _TIME_RE.finditer(line) if m.start() >= rel_end]
        if before:
            t = parse_time(before[-1].group(0))
        elif after:
            t = parse_time(after[0].group(0))
    if t is None:
        t = parse_time(span.quote)
    return base.replace(hour=t.hour, minute=t.minute) if t else base


# Flags that only mean "the model pointed at text the tables do not recognize".
# The rules extractor already finds every recognized phrasing, so for a model-origin
# span these carry no verified information: they are shown to the reviewer but do not
# change the decision. The same flags on a rules-origin span would indicate a code bug.
_ADVISORY_REASONS = {"NO_PATTERN_MATCH", "UNPARSEABLE_BP", "UNPARSEABLE_TEMP"}


def _flag(span: Span, reason: str, code_label: str | None = None) -> ReviewFlag:
    blocking = not (reason in _ADVISORY_REASONS and span.origin != "rules")
    return ReviewFlag(reason, span.doc_id, span.quote, span.model_label, code_label, RULE_FOR_KIND.get(span.kind), blocking)


def _text_fact(kind: str, value: object, span: Span, when: datetime | None, parser: str, meta: dict | None = None) -> Fact:
    full_meta = {"offset": span.offset, "origin": span.origin}
    full_meta.update(meta or {})
    return Fact(kind, value, when, f"documents:{span.doc_id}", span.doc_id, span.quote, parser, full_meta)


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
            meta: dict = {}
            if unit.endswith("?"):
                meta["unit_inferred"] = unit[0]
            if unit.startswith("C"):
                m = re.search(r"(\d{2,3}(?:\.\d+)?)\s*(?:°\s*|degrees?\s+)?(?:[cC]\b|celsius|centigrade)", span.quote, re.IGNORECASE)
                meta["converted_from"] = (f"{m.group(1)} C" if m else span.quote) + (" (unit inferred)" if "unit_inferred" in meta else "")
            facts.append(_text_fact("TEMP", temp_f, span, when, "text_temp", meta))
            continue

        label, flag = classify(span, policy)
        if flag is not None:
            flags.append(_flag(span, flag.reason, flag.code_label))
            continue
        if span.kind == SpanKind.CONSENT_SIGNATURE:
            meta = {"signer": "proxy"} if label == "SIGNED" and policy.is_proxy_signature(span.quote) else {}
            facts.append(_text_fact("CONSENT_SIG", label, span, when, "text", meta))
        elif span.kind == SpanKind.CONSENT_PROCEDURE:
            line = re.sub(r"^Procedure:\s*", "", span.quote, flags=re.IGNORECASE).strip()
            facts.append(_text_fact("CONSENT_PROC", line, span, when, "text"))
        elif span.kind == SpanKind.MED_MENTION:
            canonical, cls = policy.drugs.classify(span.quote)
            if canonical is None and re.search(r"anticoagul", span.quote, re.IGNORECASE):
                cls = "anticoagulant"
            facts.append(_text_fact("MED_MENTION", {"canonical": canonical, "class": cls, "label": label}, span, when, "text"))
        elif span.kind in (SpanKind.ANTICOAG_HOLD, SpanKind.ANTICOAG_RESUME, SpanKind.ANTICOAG_CONTINUE, SpanKind.ANTICOAG_DEFERRED):
            canonical, _ = policy.drugs.classify(span.quote)
            # An instruction that names no drug ("Anticoagulation to be resumed 48 hours post-op")
            # inherits the anticoagulant the document itself discusses, if it discusses exactly one.
            doc_canonical = None
            if canonical is None:
                named = {c for c in (policy.drugs.classify(line)[0] for line in (doc.text or "").split("\n")) if c and policy.drugs.classify(c)[1] == "anticoagulant"}
                doc_canonical = next(iter(named)) if len(named) == 1 else None
            facts.append(_text_fact(span.kind.value, {"canonical": canonical, "label": label, "doc_canonical": doc_canonical}, span, when, "text"))
        elif span.kind == SpanKind.PROCEDURE_DATE_STATEMENT:
            facts.append(_text_fact("PROCEDURE_DATE_STATEMENT", {"label": label, "date": parse_date(span.quote)}, span, when, "text"))
        else:  # DOC_ABSENCE, VITALS_DEFERRED, PROCEDURE_RISK_STATEMENT, DOC_ENCOUNTER_KIND, PLANNED_PROCEDURE
            facts.append(_text_fact(span.kind.value, label, span, when, "text"))
    return facts, flags

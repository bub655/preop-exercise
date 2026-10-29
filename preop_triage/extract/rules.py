"""Rule-based span extractor: regex over the document using the policy's pattern tables.

Deterministic and offline. It is the recall floor; a model extractor can add
spans the patterns miss, but every span still has to pass the same gates.
"""

from __future__ import annotations

import re

from ..classify import FIRST_MATCH_WINS, GROUP_FOR_KIND
from ..model import Span, SpanKind
from ..parsers import UnitConflict, parse_bp, parse_temp_f
from ..policy import Policy

_BP_SPAN_RE = re.compile(
    r"(?:(?:NIBP|BP:?|Blood pressure|Re-measured)\s*)?(?<![\d/.])\d{2,3}\s*/\s*\d{2,3}(?![\d/%])(?:\s*mmHg)?",
    re.IGNORECASE,
)
_TEMP_SPAN_RE = re.compile(
    r"(?:\b(?:temperature|temp|t)\b\.?\s*:?\s*)?\d{2,3}(?:\.\d+)?\s*°?\s*[cf]\b(?:\s*\(\s*\d{2,3}(?:\.\d+)?\s*°?\s*[cf]\s*\))?",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z])")

# Kinds whose quote is the containing sentence rather than the bare match.
SENTENCE_KINDS = {SpanKind.ANTICOAG_HOLD, SpanKind.ANTICOAG_RESUME, SpanKind.ANTICOAG_DEFERRED, SpanKind.MED_MENTION}


def _line_bounds(text: str, pos: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return start, (len(text) if end < 0 else end)


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    ls, le = _line_bounds(text, start)
    line = text[ls:le]
    cursor = 0
    for part in _SENTENCE_SPLIT.split(line):
        pstart = line.find(part, cursor)
        pend = pstart + len(part)
        cursor = pend
        if ls + pstart <= start and end <= ls + pend:
            return ls + pstart, ls + pend
    return ls, le


class RuleSpanExtractor:
    name = "rules"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def extract(self, doc_id: str, title: str | None, text: str | None) -> list[Span]:
        if not text:
            return []
        found: dict[tuple[SpanKind, int], Span] = {}

        def add(kind: SpanKind, start: int, end: int) -> None:
            if kind in SENTENCE_KINDS:
                start, end = _sentence_bounds(text, start, end)
            key = (kind, start)
            if key not in found:
                found[key] = Span(kind=kind, doc_id=doc_id, quote=text[start:end].strip(), offset=start, origin=self.name)

        for m in _BP_SPAN_RE.finditer(text):
            if parse_bp(m.group(0)) is not None:
                add(SpanKind.VITAL_BP, m.start(), m.end())
        for m in _TEMP_SPAN_RE.finditer(text):
            try:
                ok = parse_temp_f(m.group(0)) is not None
            except UnitConflict:
                ok = True  # emit so the conflict becomes a review flag downstream
            if ok:
                add(SpanKind.VITAL_TEMP, m.start(), m.end())

        for kind, group in GROUP_FOR_KIND.items():
            taken: set[int] = set()
            for label in self.policy.labels(group):
                for pattern in self.policy.compiled(group, label):
                    for m in pattern.finditer(text):
                        if group in FIRST_MATCH_WINS:
                            if m.start() in taken:
                                continue
                            taken.add(m.start())
                        add(kind, m.start(), m.end())

        return sorted(found.values(), key=lambda s: (s.offset or 0, s.kind.value))

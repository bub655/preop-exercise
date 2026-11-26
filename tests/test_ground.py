from __future__ import annotations

from preop_triage.ground import ground_spans
from preop_triage.model import Span, SpanKind

DOCS = {"d1": "VITAL SIGNS:\n  08:06  NIBP 189/112, T 37.1 C, HR 74.\nADDENDUM 08:30: Re-measured 137/78 mmHg."}


def _span(quote: str, doc_id: str = "d1", offset: int | None = None) -> Span:
    return Span(kind=SpanKind.VITAL_BP, doc_id=doc_id, quote=quote, offset=offset, origin="claude")


def test_exact_match_sets_offset() -> None:
    kept, rejected = ground_spans([_span("NIBP 189/112")], DOCS)
    assert rejected == []
    assert kept[0].offset == DOCS["d1"].index("NIBP 189/112")


def test_whitespace_normalized_match() -> None:
    kept, rejected = ground_spans([_span("08:06 NIBP 189/112, T 37.1 C")], DOCS)
    assert rejected == []
    assert kept[0].quote == "08:06  NIBP 189/112, T 37.1 C"
    assert kept[0].offset == DOCS["d1"].index("08:06")


def test_missing_quote_rejected() -> None:
    kept, rejected = ground_spans([_span("NIBP 200/120")], DOCS)
    assert kept == []
    assert rejected[0].reason == "NOT_FOUND"
    assert rejected[0].origin == "claude"


def test_too_long_rejected() -> None:
    kept, rejected = ground_spans([_span("x" * 401)], DOCS)
    assert kept == []
    assert rejected[0].reason == "TOO_LONG"


def test_unknown_doc_rejected() -> None:
    kept, rejected = ground_spans([_span("NIBP 189/112", doc_id="nope")], DOCS)
    assert kept == []
    assert rejected[0].reason == "UNKNOWN_DOC"


def test_empty_quote_rejected() -> None:
    kept, rejected = ground_spans([_span("  ")], DOCS)
    assert kept == []
    assert rejected[0].reason == "EMPTY"

"""Grounding gate: a span survives only if its quote exists verbatim in the source.

Whitespace-normalized matching is the one tolerance allowed. A grounded span's
quote is replaced by the exact source text and its offset is set, so every
downstream consumer works from the document, never from what the model typed.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .model import Rejection, Span

MAX_QUOTE_CHARS = 400
_WS = re.compile(r"\s+")


def _normalized_find(text: str, quote: str) -> tuple[int, str] | None:
    """Find quote in text ignoring whitespace runs; return (offset, exact_text)."""
    pattern = r"\s+".join(re.escape(tok) for tok in _WS.split(quote.strip()) if tok)
    m = re.search(pattern, text)
    if not m:
        return None
    return m.start(), m.group(0)


def ground_spans(spans: list[Span], docs: dict[str, str]) -> tuple[list[Span], list[Rejection]]:
    kept: list[Span] = []
    rejected: list[Rejection] = []
    for span in spans:
        quote = span.quote or ""
        if not quote.strip():
            rejected.append(Rejection(span.doc_id, quote, "EMPTY", span.origin))
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            rejected.append(Rejection(span.doc_id, quote, "TOO_LONG", span.origin))
            continue
        text = docs.get(span.doc_id)
        if text is None:
            rejected.append(Rejection(span.doc_id, quote, "UNKNOWN_DOC", span.origin))
            continue
        exact = text.find(quote)
        if exact >= 0:
            kept.append(replace(span, offset=exact))
            continue
        found = _normalized_find(text, quote)
        if found is None:
            rejected.append(Rejection(span.doc_id, quote, "NOT_FOUND", span.origin))
            continue
        offset, exact_text = found
        kept.append(replace(span, quote=exact_text, offset=offset))
    return kept, rejected

"""Run several extractors and keep every span, tagged with its origin.

Provenance later shows which spans the model added beyond the rules.
"""

from __future__ import annotations

from ..model import Span
from .base import SpanExtractor


class UnionSpanExtractor:
    name = "union"

    def __init__(self, *extractors: SpanExtractor) -> None:
        self.extractors = extractors

    def _first(self, attr: str, default=None):
        for e in self.extractors:
            if hasattr(e, attr):
                return getattr(e, attr)
        return default

    @property
    def model(self):
        return self._first("model")

    @property
    def prompt_version(self):
        return self._first("prompt_version")

    @property
    def cache_hits(self) -> int:
        return sum(getattr(e, "cache_hits", 0) for e in self.extractors)

    @property
    def last_error(self):
        return self._first("last_error")

    def extract(self, doc_id: str, title: str | None, text: str | None) -> list[Span]:
        spans: list[Span] = []
        for e in self.extractors:
            spans.extend(e.extract(doc_id, title, text))
        return spans

from __future__ import annotations

from typing import Protocol

from ..model import Span


class ExtractorError(RuntimeError):
    """An extractor could not read a document. The pipeline fails closed on it."""


class SpanExtractor(Protocol):
    """Locate candidate quotes in one document. Output is untrusted until grounded."""

    name: str

    def extract(self, doc_id: str, title: str | None, text: str | None) -> list[Span]: ...

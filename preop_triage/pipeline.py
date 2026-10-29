"""Pipeline orchestrator: ingest -> extract -> ground -> facts -> engine -> invariants.

Nothing past extraction may raise. Extractor failures become review flags.
"""

from __future__ import annotations

from typing import Any

from . import __version__
from .engine import evaluate
from .extract.base import SpanExtractor
from .facts import spans_to_facts
from .ground import ground_spans
from .ingest import ingest
from .invariants import check_invariants
from .model import Doc, Provenance, RejectionOut, ReviewFlag, ReviewFlagOut, Span, TriageOutput
from .policy import Policy

EXTRACTOR_NAMES = ("rules", "claude", "union")


def make_extractor(name: str, *, policy: Policy, model: str | None = None, use_cache: bool = True) -> SpanExtractor:
    from .extract.rules import RuleSpanExtractor

    if name == "rules":
        return RuleSpanExtractor(policy)
    if name in ("claude", "union"):
        from .extract.cache import ExtractionCache, default_cache_root
        from .extract.claude import ClaudeSpanExtractor
        from .extract.union import UnionSpanExtractor

        cache = ExtractionCache(default_cache_root()) if use_cache else None
        claude = ClaudeSpanExtractor(model=model or "claude-opus-5-5", policy=policy, cache=cache)
        return claude if name == "claude" else UnionSpanExtractor(RuleSpanExtractor(policy), claude)
    raise ValueError(f"unknown extractor {name!r}; expected one of {EXTRACTOR_NAMES}")


def _docs(submission: dict[str, Any]) -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    for i, d in enumerate(submission.get("documents") or []):
        doc_id = d.get("doc_id") or f"documents[{i}]"
        text = d.get("text")
        docs[doc_id] = Doc(doc_id, d.get("type"), d.get("date"), d.get("author"), text if isinstance(text, str) else None, i)
    return docs


def _explanation(decision: str, checks, issues) -> str:
    failed = [f"{c.rule_id} {c.status.lower()} ({c.summary})" for c in checks if c.status in ("FAIL", "UNVERIFIED")]
    passed = [c.rule_id for c in checks if c.status == "PASS"]
    skipped = [c.rule_id for c in checks if c.status == "SKIPPED"]
    parts = [f"{decision}."]
    if failed:
        parts.append("Not satisfied: " + "; ".join(failed) + ".")
    if passed:
        parts.append("Satisfied: " + ", ".join(passed) + ".")
    if skipped:
        parts.append("Not applicable: " + ", ".join(skipped) + ".")
    return " ".join(parts)


def _system_error(exc: Exception, policy: Policy, extractor: SpanExtractor) -> TriageOutput:
    return TriageOutput(
        decision="NEEDS_FOLLOW_UP",
        explanation="NEEDS_FOLLOW_UP. [SYSTEM_ERROR: the triage pipeline failed; routed to human review]",
        review_flags=[ReviewFlagOut(reason="SYSTEM_ERROR", quote=f"{type(exc).__name__}: {str(exc)[:300]}")],
        provenance=Provenance(policy_version=policy.version, extractor=getattr(extractor, "name", type(extractor).__name__), pipeline_version=__version__),
    )


def run(submission: dict[str, Any], *, policy: Policy, extractor: SpanExtractor) -> TriageOutput:
    """Triage one package. Never raises: any internal failure becomes a SYSTEM_ERROR result."""
    try:
        return _run(submission, policy=policy, extractor=extractor)
    except Exception as exc:  # last line of defense; the gates below should make this unreachable
        return _system_error(exc, policy, extractor)


def _run(submission: dict[str, Any], *, policy: Policy, extractor: SpanExtractor) -> TriageOutput:
    facts, flags = ingest(submission, policy)
    docs = _docs(submission)

    spans: list[Span] = []
    for doc in sorted(docs.values(), key=lambda d: d.index):
        try:
            spans.extend(extractor.extract(doc.doc_id, doc.title, doc.text))
        except Exception as exc:  # fail closed: the document is treated as unread
            flags.append(ReviewFlag("EXTRACTOR_ERROR", doc.doc_id, str(exc)[:200], None, None, "R0"))

    texts = {d.doc_id: d.text or "" for d in docs.values()}
    grounded, rejections = ground_spans(spans, texts)
    text_facts, text_flags = spans_to_facts(grounded, docs, policy)

    result = evaluate(facts + text_facts, flags + text_flags, policy)
    out = TriageOutput(
        decision=result.decision,
        issues=result.issues,
        explanation=_explanation(result.decision, result.checks, result.issues),
        checks=result.checks,
        review_flags=[ReviewFlagOut(**vars(f)) for f in result.review_flags],
        assumptions=result.assumptions,
        rejections=[RejectionOut(**vars(r)) for r in rejections],
        provenance=Provenance(
            policy_version=policy.version,
            extractor=getattr(extractor, "name", type(extractor).__name__),
            model=getattr(extractor, "model", None),
            prompt_version=getattr(extractor, "prompt_version", None),
            cache_hits=getattr(extractor, "cache_hits", 0),
            pipeline_version=__version__,
        ),
    )
    return check_invariants(out, texts, submission, policy)

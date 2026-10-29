"""Output invariant gate: the last check before a result leaves the pipeline.

Assertions are about the output's shape against the policy, independent of
how it was produced. A failure never raises; it demotes the result to
NEEDS_FOLLOW_UP with a SYSTEM_ERROR flag so a human looks at it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .engine import CATEGORY_FOR_RULE
from .model import ReviewFlagOut, TriageOutput

_WS = re.compile(r"\s+")


def _regrounds(quote: str, text: str) -> bool:
    if quote in text:
        return True
    pattern = r"\s+".join(re.escape(tok) for tok in _WS.split(quote.strip()) if tok)
    return re.search(pattern, text) is not None


def _structured_vital_exists(source: str, submission: dict[str, Any]) -> bool:
    m = re.fullmatch(r"vitals\[(\d+)\]", source)
    if not m:
        return False
    vitals = submission.get("vitals") or []
    idx = int(m.group(1))
    return 0 <= idx < len(vitals)


def _violations(out: TriageOutput, docs: dict[str, str], submission: dict[str, Any]) -> list[str]:
    v: list[str] = []
    if out.decision == "READY" and (out.issues or out.review_flags):
        v.append("READY with issues or review flags")
    if out.decision == "NOT_CLEARED":
        r4 = [i for i in out.issues if i.category == "ACUTE_SAFETY_EXCLUSION"]
        if not r4:
            v.append("NOT_CLEARED without an ACUTE_SAFETY_EXCLUSION issue")
        for i in r4:
            backed = any(
                (e.quote and e.doc_id in docs and _regrounds(e.quote, docs[e.doc_id])) or _structured_vital_exists(e.source, submission)
                for e in i.evidence
            )
            if not backed:
                v.append("ACUTE_SAFETY_EXCLUSION issue not backed by a grounded vital")
    elif any(i.category == "ACUTE_SAFETY_EXCLUSION" for i in out.issues):
        v.append("ACUTE_SAFETY_EXCLUSION issue without NOT_CLEARED decision")
    for i in out.issues:
        if not i.evidence:
            v.append(f"issue {i.rule_id}/{i.status} has no evidence")
        expected = CATEGORY_FOR_RULE.get(i.rule_id)
        if i.category == "ACUTE_SAFETY_EXCLUSION":
            if i.rule_id != "R4":
                v.append(f"issue category {i.category} does not match rule {i.rule_id}")
        elif expected is not None and i.category != expected and not (i.rule_id == "R0" and i.category == "MISSING_REQUIRED_DATA"):
            # review-flag issues may carry the rule's category; anything else is a mismatch
            if i.status != "UNVERIFIED":
                v.append(f"issue category {i.category} does not match rule {i.rule_id}")
        for e in i.evidence:
            if e.quote and e.doc_id:
                text = docs.get(e.doc_id)
                if text is None or not _regrounds(e.quote, text):
                    v.append(f"evidence quote does not reground in {e.doc_id}")
    return v


def check_invariants(out: TriageOutput, docs: dict[str, str], submission: dict[str, Any]) -> TriageOutput:
    violations = _violations(out, docs, submission)
    canonical = json.dumps(out.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    again = json.dumps(TriageOutput.model_validate_json(out.model_dump_json()).model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    if canonical != again:
        violations.append("serialization is not stable")
    if not violations:
        return out
    flags = list(out.review_flags) + [ReviewFlagOut(reason="SYSTEM_ERROR", quote="; ".join(violations))]
    return out.model_copy(update={"decision": "NEEDS_FOLLOW_UP", "review_flags": flags, "explanation": out.explanation + " [SYSTEM_ERROR: output failed invariant checks; routed to human review]"})

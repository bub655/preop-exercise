"""Output invariant gate: the last check before a result leaves the pipeline.

Assertions are about the output's shape against the policy, independent of
how it was produced. A failure never raises; it demotes the result to
NEEDS_FOLLOW_UP with a SYSTEM_ERROR flag so a human looks at it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .engine import CATEGORY_FOR_RULE
from .model import Evidence, ReviewFlagOut, TriageOutput
from .parsers import UnitConflict, parse_bp, parse_temp_f, q1
from .policy import Policy, load_policy

_WS = re.compile(r"\s+")
_PATH_RE = re.compile(r"^([a-z_]+)(?:\[(\d+)\])?(?:\.([a-z_]+))?$")
EXCLUSION_STATUSES = {"BP_EXCLUSION", "TEMP_EXCLUSION"}
# R3 may be skipped on READY (no anticoagulant). Any other skipped rule means something was not evaluated.
SKIPPABLE_ON_READY = {"R3"}


def _regrounds(quote: str, text: str) -> bool:
    if quote in text:
        return True
    pattern = r"\s+".join(re.escape(tok) for tok in _WS.split(quote.strip()) if tok)
    return re.search(pattern, text) is not None


def _resolve(source: str, submission: dict[str, Any]) -> tuple[bool, Any]:
    """Resolve a JSON path like vitals[2] or procedure.procedure_type. (found, value)."""
    m = _PATH_RE.match(source)
    if not m:
        return False, None
    top, idx, field = m.group(1), m.group(2), m.group(3)
    value: Any = submission.get(top) if isinstance(submission, dict) else None
    if idx is not None:
        if not isinstance(value, list) or not 0 <= int(idx) < len(value):
            return False, None
        value = value[int(idx)]
    if field is not None:
        if not isinstance(value, dict):
            return False, None
        value = value.get(field)
    return True, value


def _exceeds(bp: tuple[int, int] | None, temp_f: Decimal | None, policy: Policy) -> bool:
    if bp is not None and (bp[0] >= policy.thresholds.sbp or bp[1] >= policy.thresholds.dbp):
        return True
    return temp_f is not None and temp_f > policy.thresholds.temp_f


def _exclusion_backed(e: Evidence, docs: dict[str, str], submission: dict[str, Any], policy: Policy) -> bool:
    """The evidence must itself contain a reading over the threshold, re-derived here."""
    if e.quote and e.doc_id:
        text = docs.get(e.doc_id)
        if text is None or not _regrounds(e.quote, text):
            return False
        try:
            parsed = parse_temp_f(e.quote)
        except UnitConflict:
            parsed = None
        return _exceeds(parse_bp(e.quote), parsed[0] if parsed else None, policy)
    found, vital = _resolve(e.source, submission)
    if not found or not isinstance(vital, dict):
        return False
    bp = None
    temp = None
    try:
        if vital.get("systolic") is not None and vital.get("diastolic") is not None:
            bp = (int(vital["systolic"]), int(vital["diastolic"]))
        if vital.get("value_f") is not None:
            temp = q1(Decimal(str(vital["value_f"])))
    except (TypeError, ValueError, InvalidOperation):
        return False
    return _exceeds(bp, temp, policy)


def _violations(out: TriageOutput, docs: dict[str, str], submission: dict[str, Any], policy: Policy) -> list[str]:
    v: list[str] = []
    blocking = [f for f in out.review_flags if f.blocking]
    if out.decision == "READY":
        if out.issues or blocking:
            v.append("READY with issues or blocking review flags")
        for c in out.checks:
            if c.status == "UNVERIFIED":
                v.append(f"READY with unverified check {c.rule_id}")
            if c.status == "SKIPPED" and c.rule_id not in SKIPPABLE_ON_READY:
                v.append(f"READY with skipped check {c.rule_id}")
    r4 = [i for i in out.issues if i.category == "ACUTE_SAFETY_EXCLUSION"]
    if out.decision == "NOT_CLEARED":
        if not r4:
            v.append("NOT_CLEARED without an ACUTE_SAFETY_EXCLUSION issue")
        for i in r4:
            if not any(_exclusion_backed(e, docs, submission, policy) for e in i.evidence):
                v.append("ACUTE_SAFETY_EXCLUSION issue not backed by a grounded exclusion-level vital")
    elif r4:
        v.append("ACUTE_SAFETY_EXCLUSION issue without NOT_CLEARED decision")
    for i in out.issues:
        if not i.evidence:
            v.append(f"issue {i.rule_id}/{i.status} has no evidence")
        if i.category == "ACUTE_SAFETY_EXCLUSION":
            if i.rule_id != "R4" or i.status not in EXCLUSION_STATUSES:
                v.append(f"exclusion issue has rule {i.rule_id} status {i.status}")
        else:
            expected = CATEGORY_FOR_RULE.get(i.rule_id) or CATEGORY_FOR_RULE.get(i.rule_id.split(".")[0])
            if expected is None or i.category != expected:
                v.append(f"issue category {i.category} does not match rule {i.rule_id}")
        for e in i.evidence:
            if not e.quote:
                continue
            if e.doc_id:
                text = docs.get(e.doc_id)
                if text is None or not _regrounds(e.quote, text):
                    v.append(f"evidence quote does not reground in {e.doc_id}")
            else:
                found, value = _resolve(e.source, submission)
                if found and e.quote not in json.dumps(value, ensure_ascii=False, default=str):
                    v.append(f"evidence quote does not match {e.source}")
    return v


def check_invariants(out: TriageOutput, docs: dict[str, str], submission: dict[str, Any], policy: Policy | None = None) -> TriageOutput:
    pol = policy or load_policy()
    violations = _violations(out, docs, submission, pol)
    canonical = json.dumps(out.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    again = json.dumps(TriageOutput.model_validate_json(out.model_dump_json()).model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    if canonical != again:
        violations.append("serialization is not stable")
    if not violations:
        return out
    flags = list(out.review_flags) + [ReviewFlagOut(reason="SYSTEM_ERROR", quote="; ".join(violations))]
    return out.model_copy(update={"decision": "NEEDS_FOLLOW_UP", "review_flags": flags, "explanation": out.explanation + " [SYSTEM_ERROR: output failed invariant checks; routed to human review]"})

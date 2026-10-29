"""Hand-written held-out cases (data/heldout_cases.jsonl). Fail-open must be zero; case 03 must be READY."""

from __future__ import annotations

import json
from pathlib import Path

from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.pipeline import run
from preop_triage.policy import load_policy
from preop_triage.synth import classify_error

ROOT = Path(__file__).resolve().parent.parent
CASES = [json.loads(l) for l in (ROOT / "data" / "heldout_cases.jsonl").read_text().splitlines() if l.strip()]
POLICY = load_policy()
RULES = RuleSpanExtractor(POLICY)


def _results():
    out = {}
    for c in CASES:
        res = run(c["submission"], policy=POLICY, extractor=RULES)
        out[c["case_id"]] = (res, classify_error(c["label"]["decision"], c["label"]["categories"], res.decision, sorted({i.category for i in res.issues})))
    return out


def test_heldout_never_fails_open() -> None:
    results = _results()
    for cid, (res, kind) in results.items():
        print(kind, cid, res.decision, sorted({i.category for i in res.issues}))
    assert [cid for cid, (_, kind) in results.items() if kind == "fail_open"] == []


def test_guardian_signed_consent_is_ready_with_assumption() -> None:
    res, kind = _results()["heldout_03_guardian_signed_consent"]
    assert res.decision == "READY" and kind == "ok"
    assert any("on behalf of the patient" in a.text or "guardian" in a.text.lower() for a in res.assumptions)
    assert any("Maria Lopez" in (e.quote or "") for a in res.assumptions for e in a.evidence)


def test_continue_through_surgery_plan_is_ready_with_assumption() -> None:
    res, kind = _results()["heldout_01_continue_through_surgery"]
    assert res.decision == "READY" and kind == "ok"
    assert any("without interruption" in a.text for a in res.assumptions)

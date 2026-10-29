from __future__ import annotations

import json
from pathlib import Path

from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.pipeline import run
from preop_triage.policy import load_policy

ROOT = Path(__file__).resolve().parent.parent
CASES = [json.loads(l) for l in (ROOT / "data" / "patients_sample_50.jsonl").read_text().splitlines() if l.strip()]
POLICY = load_policy()
EXTRACTOR = RuleSpanExtractor(POLICY)

DECISION_FLOOR = 0.98
CATEGORY_FLOOR = 0.98


def _run_all():
    rows = []
    for c in CASES:
        out = run(c["submission"], policy=POLICY, extractor=EXTRACTOR)
        label = c["label"]
        rows.append(
            {
                "case_id": c["case_id"],
                "decision_ok": out.decision == label["decision"],
                "cats_ok": sorted({i.category for i in out.issues}) == sorted(set(label["categories"])),
                "expected": (label["decision"], sorted(set(label["categories"]))),
                "actual": (out.decision, sorted({i.category for i in out.issues})),
                "flags": [f.reason for f in out.review_flags],
            }
        )
    return rows


def test_rules_extractor_meets_floors() -> None:
    rows = _run_all()
    misses = [r for r in rows if not (r["decision_ok"] and r["cats_ok"])]
    for r in misses:
        print(f"MISS {r['case_id']}: expected={r['expected']} actual={r['actual']} flags={r['flags']}")
    d = sum(r["decision_ok"] for r in rows) / len(rows)
    c = sum(r["cats_ok"] for r in rows) / len(rows)
    print(f"decision={d:.2f} categories={c:.2f}")
    assert d >= DECISION_FLOOR
    assert c >= CATEGORY_FLOOR


def test_pipeline_is_byte_deterministic() -> None:
    sub = CASES[16]["submission"]
    a = run(sub, policy=POLICY, extractor=EXTRACTOR).model_dump_json()
    b = run(sub, policy=POLICY, extractor=EXTRACTOR).model_dump_json()
    assert a == b


def test_document_with_null_text_does_not_crash() -> None:
    sub = json.loads(json.dumps(CASES[0]["submission"]))
    sub["documents"][0]["text"] = None
    sub["documents"].append({"doc_id": "empty", "type": "Note", "date": "2026-02-01", "author": None, "text": ""})
    out = run(sub, policy=POLICY, extractor=EXTRACTOR)
    assert out.decision in ("READY", "NEEDS_FOLLOW_UP", "NOT_CLEARED")


def test_no_system_errors_on_sample() -> None:
    for c in CASES:
        out = run(c["submission"], policy=POLICY, extractor=EXTRACTOR)
        assert not any(f.reason == "SYSTEM_ERROR" for f in out.review_flags), c["case_id"]

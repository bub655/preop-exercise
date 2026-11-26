"""Held-out synthetic cases: phrasings and structures not in the 50 sample cases.

Fail-open must be zero. Fail-closed is the recall cost and is capped so it stays visible.
"""

from __future__ import annotations

from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.pipeline import run
from preop_triage.policy import load_policy
from preop_triage.synth import classify_error, generate

POLICY = load_policy()
RULES = RuleSpanExtractor(POLICY)
N = 300
FAIL_CLOSED_CEILING = 0.05


def test_synthetic_held_out_never_fails_open() -> None:
    kinds = []
    misses = []
    for i in range(N):
        sub, sc = generate(7_000_000 + i)
        out = run(sub, policy=POLICY, extractor=RULES)
        actual = sorted({iss.category for iss in out.issues})
        kind = classify_error(sc.expected_decision, sc.expected_categories, out.decision, actual)
        kinds.append(kind)
        if kind != "ok":
            misses.append((kind, sc.seed, sc.expected_decision, sc.expected_categories, out.decision, actual, [f.reason for f in out.review_flags]))
    for m in misses:
        print(m)
    assert kinds.count("fail_open") == 0, [m for m in misses if m[0] == "fail_open"]
    assert kinds.count("fail_closed") / N <= FAIL_CLOSED_CEILING


def test_synthetic_outputs_pass_invariants() -> None:
    for i in range(100):
        sub, _ = generate(8_000_000 + i)
        out = run(sub, policy=POLICY, extractor=RULES)
        assert not any(f.reason == "SYSTEM_ERROR" for f in out.review_flags), i

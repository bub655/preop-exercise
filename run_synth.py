"""Run the pipeline over synthetic held-out cases and report fail-open / fail-closed rates."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.pipeline import run
from preop_triage.policy import load_policy
from preop_triage.synth import classify_error, generate

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--report", default=str(ROOT / "data" / "synth_report.json"))
    ap.add_argument("--dump-failures", type=int, default=15)
    args = ap.parse_args()

    policy = load_policy()
    ext = RuleSpanExtractor(policy)
    rows = []
    for i in range(args.n):
        seed = args.seed * 100000 + i
        sub, sc = generate(seed)
        out = run(sub, policy=policy, extractor=ext)
        actual_cats = sorted({iss.category for iss in out.issues})
        kind = classify_error(sc.expected_decision, sc.expected_categories, out.decision, actual_cats)
        rows.append({"seed": seed, "scenario": {k: getattr(sc, k) for k in ("hp", "consent", "labs", "anticoag", "vitals", "date", "risk", "structured_c", "laterality")}, "expected": [sc.expected_decision, sc.expected_categories], "actual": [out.decision, actual_cats], "kind": kind, "flags": [f.reason for f in out.review_flags if f.blocking], "statuses": [i.status for i in out.issues]})

    kinds = Counter(r["kind"] for r in rows)
    by_axis: dict[str, Counter] = {}
    for r in rows:
        if r["kind"] == "ok":
            continue
        for axis, value in r["scenario"].items():
            by_axis.setdefault(r["kind"], Counter())[f"{axis}={value}"] += 1
    summary = {"n": args.n, "seed": args.seed, "ok": kinds["ok"], "fail_open": kinds["fail_open"], "fail_closed": kinds["fail_closed"], "fail_open_rate_pct": round(100 * kinds["fail_open"] / args.n, 2), "fail_closed_rate_pct": round(100 * kinds["fail_closed"] / args.n, 2), "worst_axes": {k: v.most_common(8) for k, v in by_axis.items()}}
    Path(args.report).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    shown = 0
    for r in rows:
        if r["kind"] != "ok" and shown < args.dump_failures:
            shown += 1
            print(f"{r['kind']:11s} seed={r['seed']} {r['scenario']} expected={r['expected']} actual={r['actual']} flags={r['flags']} statuses={r['statuses']}")


if __name__ == "__main__":
    main()

"""Score triage outputs against the human labels, locally and offline.

Scoring is unchanged from the starter: schema validity, exact decision match,
and exact issue-category set match, equally weighted. This version drops the
OpenAI Evals round trip (the solution is provider-agnostic and runs without
any key), adds per-category precision/recall and a decision confusion matrix,
and can diff against the previous report so regressions are visible per case.

Determinism mode replays one record N times through `triage_submission`.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core import Decision, IssueCategory, triage_submission

ROOT = Path(__file__).resolve().parent

DEFAULT_MODEL = "claude-opus-5-5"
PRIMARY_SCORE_NAME = "aggregate_local_score_pct"
DECISIONS = ["READY", "NEEDS_FOLLOW_UP", "NOT_CLEARED"]
CATEGORIES = ["REQUIRED_DOCUMENTATION", "REQUIRED_TESTING", "ANTICOAGULATION_MANAGEMENT", "ACUTE_SAFETY_EXCLUSION", "MISSING_REQUIRED_DATA"]

METRIC_WEIGHTS: dict[str, float] = {
    "json_schema_valid": 1.0,
    "decision_match_oracle": 1.0,
    "issue_categories_match_oracle": 1.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "data" / "patients_sample_50.jsonl"))
    parser.add_argument("--outputs", default=str(ROOT / "data" / "baseline_outputs.jsonl"))
    parser.add_argument("--report", default=str(ROOT / "data" / "eval_report.json"))
    parser.add_argument("--previous", default=None, help="Previous eval report to diff against (defaults to the existing --report if present)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id used for determinism mode")
    parser.add_argument("--extractor", default="rules", choices=["rules", "claude", "union"], help="Extractor used for determinism mode")
    parser.add_argument("--no-cache", action="store_true", help="Determinism mode: bypass the extraction cache so the model is exercised each run")
    parser.add_argument("--determinism", action="store_true", help="Run N-x replay check instead of eval scoring")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--record-index", type=int, default=0)
    return parser.parse_args()


# --- Inputs ------------------------------------------------------------------


class CaseLabel(BaseModel):
    decision: Decision
    categories: list[IssueCategory]
    rationale: str | None = None


class EvalCase(BaseModel):
    case_id: str
    submission: dict[str, Any]
    label: CaseLabel


class _ScoredIssue(BaseModel):
    model_config = ConfigDict(extra="allow")
    category: IssueCategory


class ScoredOutput(BaseModel):
    """Only decision and issue categories are scored; the rest is implementation-defined."""

    model_config = ConfigDict(extra="allow")
    decision: Decision
    issues: list[_ScoredIssue]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_cases(path: Path) -> list[EvalCase]:
    return [EvalCase.model_validate(row) for row in load_jsonl(path)]


def load_baseline_outputs(path: Path) -> dict[int, dict[str, Any]]:
    outputs: dict[int, dict[str, Any]] = {}
    for row in load_jsonl(path):
        outputs[int(row.get("record_index", len(outputs)))] = row
    return outputs


def _extract_output_payload(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    payload = row.get("output")
    error = row.get("error")
    if isinstance(payload, dict):
        return payload, None if error is None else str(error)
    if "decision" in row and "issues" in row:
        return row, None
    return None, None if error is None else str(error)


# --- Scoring -----------------------------------------------------------------


def _metrics_for_row(label: CaseLabel, payload: dict[str, Any] | None) -> dict[str, Any]:
    parsed: ScoredOutput | None = None
    parse_error: str | None = None
    if payload is not None:
        try:
            parsed = ScoredOutput.model_validate(payload)
        except Exception as exc:  # malformed output path
            parse_error = str(exc)
    actual_decision = parsed.decision if parsed else "INVALID"
    expected = sorted(set(label.categories))
    actual = sorted({i.category for i in parsed.issues}) if parsed else []
    metrics = {
        "json_schema_valid": parsed is not None,
        "decision_match_oracle": parsed is not None and actual_decision == label.decision,
        "issue_categories_match_oracle": expected == actual,
    }
    aggregate = 100.0 * sum(METRIC_WEIGHTS[k] * float(v) for k, v in metrics.items()) / sum(METRIC_WEIGHTS.values())
    return {
        "parsed_output": payload if parsed else None,
        "parse_error": parse_error,
        "metrics": metrics,
        "aggregate_local_score": aggregate,
        "expected_categories": expected,
        "actual_categories": actual,
        "actual_decision": actual_decision,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"records": 0, PRIMARY_SCORE_NAME: 0.0, "primary_score_pct": 0.0}
    summary: dict[str, Any] = {"records": len(rows)}
    for name in METRIC_WEIGHTS:
        rate = 100.0 * sum(1 for r in rows if r["metrics"][name]) / len(rows)
        summary[f"{name}_rate_pct"] = round(rate, 2)
    summary[PRIMARY_SCORE_NAME] = round(sum(float(r["aggregate_local_score"]) for r in rows) / len(rows), 2)
    summary["primary_score_pct"] = summary[PRIMARY_SCORE_NAME]
    return summary


def _per_category(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cat in CATEGORIES:
        tp = sum(1 for r in rows if cat in r["expected_categories"] and cat in r["actual_categories"])
        fp = sum(1 for r in rows if cat not in r["expected_categories"] and cat in r["actual_categories"])
        fn = sum(1 for r in rows if cat in r["expected_categories"] and cat not in r["actual_categories"])
        out[cat] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        }
    return out


def _confusion(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix = {d: {a: 0 for a in DECISIONS + ["INVALID"]} for d in DECISIONS}
    for r in rows:
        matrix[r["label"]["decision"]][r["actual_decision"]] += 1
    return matrix


def score(cases: list[EvalCase], outputs_by_index: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Pure scoring: cases + outputs -> report dict (the shape the TUI reads)."""
    rows: list[dict[str, Any]] = []
    for idx, case in enumerate(cases):
        payload, error = _extract_output_payload(outputs_by_index.get(idx, {}))
        local = _metrics_for_row(case.label, payload)
        rows.append(
            {
                "record_index": idx,
                "case_id": case.case_id,
                "submission": case.submission,
                "baseline_error": error,
                "label": case.label.model_dump(),
                "parsed_output": local["parsed_output"],
                "parse_error": local["parse_error"],
                "metrics": local["metrics"],
                "aggregate_local_score": local["aggregate_local_score"],
                "expected_categories": local["expected_categories"],
                "actual_categories": local["actual_categories"],
                "actual_decision": local["actual_decision"],
            }
        )
    summary = _summary(rows)
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "mode": "eval",
        "primary_score": {"name": PRIMARY_SCORE_NAME, "value_pct": summary[PRIMARY_SCORE_NAME], "goal": "maximize"},
        "local_metrics_summary": summary,
        "per_category": _per_category(rows),
        "confusion": _confusion(rows),
        "records": rows,
    }


def diff_reports(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Cases that went from fully correct to not, and vice versa."""

    def ok(r: dict[str, Any]) -> bool:
        return all(bool(v) for v in r["metrics"].values())

    prev = {r["case_id"]: ok(r) for r in previous.get("records", [])}
    cur = {r["case_id"]: ok(r) for r in current.get("records", [])}
    return {
        "previous_score_pct": previous.get("primary_score", {}).get("value_pct"),
        "current_score_pct": current.get("primary_score", {}).get("value_pct"),
        "regressions": sorted(c for c in cur if prev.get(c) is True and not cur[c]),
        "fixes": sorted(c for c in cur if prev.get(c) is False and cur[c]),
    }


def run_eval_mode(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(Path(args.input))
    outputs = load_baseline_outputs(Path(args.outputs))
    report = score(cases, outputs)
    prev_path = Path(args.previous) if args.previous else Path(args.report)
    if prev_path.exists():
        try:
            report["diff_vs_previous"] = diff_reports(json.loads(prev_path.read_text(encoding="utf-8")), report)
        except (OSError, ValueError):
            pass
    return report


def run_determinism_mode(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(Path(args.input))
    if not cases:
        raise ValueError(f"No patient records found in {args.input}")
    if not 0 <= args.record_index < len(cases):
        raise IndexError(f"record-index {args.record_index} out of range for {len(cases)} records")
    case = cases[args.record_index]

    run_rows: list[dict[str, Any]] = []
    decisions: list[str] = []
    canonical_outputs: list[str] = []
    for run_index in range(args.runs):
        started = time.perf_counter()
        try:
            output = triage_submission(case.submission, model=args.model, extractor=args.extractor, use_cache=not args.no_cache)
            decision = output.decision
            canonical = json.dumps(output.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            valid, error = True, None
        except Exception as exc:  # network/runtime failure path
            decision, canonical, valid, error = "INVALID", "INVALID", False, str(exc)
        run_rows.append({"run_index": run_index, "decision": decision, "json_schema_valid": valid, "error": error, "seconds": round(time.perf_counter() - started, 3)})
        decisions.append(decision)
        canonical_outputs.append(canonical)

    decision_counts = Counter(decisions)
    canonical_counts = Counter(canonical_outputs)
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "mode": "determinism",
        "record_index": args.record_index,
        "case_id": case.case_id,
        "model": args.model,
        "extractor": args.extractor,
        "cache": not args.no_cache,
        "runs": args.runs,
        "decision_stability_pct": round(100.0 * max(decision_counts.values()) / args.runs, 2),
        "json_format_stability_pct": round(100.0 * sum(1 for r in run_rows if r["json_schema_valid"]) / args.runs, 2),
        "exact_output_match_pct": round(100.0 * max(canonical_counts.values()) / args.runs, 2),
        "decision_counts": dict(decision_counts),
        "run_rows": run_rows,
    }


def main() -> None:
    args = parse_args()
    if args.runs <= 0:
        raise ValueError("--runs must be > 0")
    report = run_determinism_mode(args) if args.determinism else run_eval_mode(args)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote report -> {report_path}")
    if report.get("mode") == "eval":
        print(f"Primary score ({PRIMARY_SCORE_NAME}): {report['primary_score']['value_pct']}%")
        for cat, m in report["per_category"].items():
            print(f"  {cat:28s} tp={m['tp']:2d} fp={m['fp']:2d} fn={m['fn']:2d} precision={m['precision']} recall={m['recall']}")
        diff = report.get("diff_vs_previous")
        if diff:
            print(f"  vs previous {diff['previous_score_pct']}% -> {diff['current_score_pct']}%  regressions={diff['regressions']} fixes={diff['fixes']}")
    else:
        print(f"decision_stability={report['decision_stability_pct']}% exact_output_match={report['exact_output_match_pct']}%")


if __name__ == "__main__":
    main()

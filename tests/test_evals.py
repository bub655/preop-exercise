from __future__ import annotations

from run_evals import CaseLabel, EvalCase, score

TUI_FIELDS = {"record_index", "case_id", "submission", "baseline_error", "label", "parsed_output", "parse_error", "metrics", "aggregate_local_score"}


def _case(i: int, decision: str, cats: list[str]) -> EvalCase:
    return EvalCase(case_id=f"c{i}", submission={"procedure": {}}, label=CaseLabel(decision=decision, categories=cats))


def _out(decision: str, cats: list[str]) -> dict:
    return {"output": {"decision": decision, "issues": [{"category": c} for c in cats], "explanation": ""}, "error": None}


def test_per_category_precision_recall() -> None:
    cases = [_case(0, "NEEDS_FOLLOW_UP", ["REQUIRED_TESTING"]), _case(1, "READY", []), _case(2, "NEEDS_FOLLOW_UP", ["REQUIRED_DOCUMENTATION"])]
    outputs = {0: _out("NEEDS_FOLLOW_UP", ["REQUIRED_TESTING"]), 1: _out("NEEDS_FOLLOW_UP", ["REQUIRED_TESTING"]), 2: _out("NEEDS_FOLLOW_UP", [])}
    report = score(cases, outputs)
    pc = report["per_category"]
    assert pc["REQUIRED_TESTING"] == {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0}
    assert pc["REQUIRED_DOCUMENTATION"] == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}


def test_confusion_matrix_counts() -> None:
    cases = [_case(0, "READY", []), _case(1, "READY", []), _case(2, "NOT_CLEARED", ["ACUTE_SAFETY_EXCLUSION"])]
    outputs = {0: _out("READY", []), 1: _out("NEEDS_FOLLOW_UP", ["REQUIRED_TESTING"]), 2: _out("NOT_CLEARED", ["ACUTE_SAFETY_EXCLUSION"])}
    report = score(cases, outputs)
    assert report["confusion"]["READY"]["READY"] == 1
    assert report["confusion"]["READY"]["NEEDS_FOLLOW_UP"] == 1
    assert report["confusion"]["NOT_CLEARED"]["NOT_CLEARED"] == 1
    assert report["local_metrics_summary"]["decision_match_oracle_rate_pct"] == round(100 * 2 / 3, 2)


def test_report_shape_has_tui_fields_and_primary_score() -> None:
    cases = [_case(0, "READY", [])]
    report = score(cases, {0: _out("READY", [])})
    assert TUI_FIELDS <= set(report["records"][0].keys())
    assert report["primary_score"]["name"] == "aggregate_local_score_pct" and report["primary_score"]["value_pct"] == 100.0
    assert report["records"][0]["metrics"] == {"json_schema_valid": True, "decision_match_oracle": True, "issue_categories_match_oracle": True}


def test_missing_output_is_invalid_not_crash() -> None:
    report = score([_case(0, "READY", [])], {})
    assert report["records"][0]["metrics"]["json_schema_valid"] is False
    assert report["records"][0]["actual_decision"] == "INVALID"


def test_diff_vs_previous_lists_regressions_and_fixes() -> None:
    from run_evals import diff_reports

    prev = score([_case(0, "READY", []), _case(1, "READY", [])], {0: _out("READY", []), 1: _out("NEEDS_FOLLOW_UP", [])})
    cur = score([_case(0, "READY", []), _case(1, "READY", [])], {0: _out("NEEDS_FOLLOW_UP", []), 1: _out("READY", [])})
    d = diff_reports(prev, cur)
    assert d["regressions"] == ["c0"] and d["fixes"] == ["c1"]

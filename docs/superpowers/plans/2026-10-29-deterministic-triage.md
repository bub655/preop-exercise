# Deterministic Pre-Op Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-call LLM baseline with a grounded, deterministic triage pipeline where the model only locates text and code makes every decision.

**Architecture:** Six-stage pipeline (ingest, extract, ground, parse/classify, engine, invariants) with a `SpanExtractor` interface implemented by a rule-based extractor (offline) and a Claude extractor (cached). Policy numbers, drug tables, and phrase patterns live in `config/policy.yaml`. Everything after extraction is pure Python and fails closed.

**Tech Stack:** Python 3.11+, uv, pydantic 2, pyyaml, anthropic SDK 2.x, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-deterministic-triage-design.md`

## Global Constraints

- `triage_submission(submission, *, model) -> TriageOutput` in `core.py` keeps its signature; `TriageOutput` keeps `decision`, `issues[].category`, `explanation`.
- No code path from extraction onward raises out of `triage_submission`; failures become review flags and `NEEDS_FOLLOW_UP`.
- Windows inclusive: `0 <= days_before <= window`. Thresholds: SBP `>= 180`, DBP `>= 110`, temp `> 100.4 F` after one-decimal rounding.
- Celsius conversion uses `fractions.Fraction`, then `round(x, 1)` via `Decimal` quantize `ROUND_HALF_UP`.
- Default model id `claude-opus-5-5`, effort `medium`, `output_config.format` JSON schema, `max_tokens` 8192, no `temperature`.
- All timestamps are naive UTC `datetime`; date-only sources get `00:00`.
- Scoring stays exact set match on categories and exact decision match.

## Review Focus

1. A document whose `text` is `None` or empty must produce no spans and no crash (Task 6, Task 9 test).
2. A structured vital with `systolic` present but `diastolic` null must be ignored as a BP fact and reported as a review flag, not treated as 0 (Task 5 test).
3. A consent that is signed but whose `Procedure:` line is absent must be `UNVERIFIED`, not `SIGNED` (Task 7 test).
4. Two vitals with the identical timestamp from structured and text sources: structured wins, and the tie is recorded as an assumption (Task 7 test).
5. `submission_received_at` missing: review time falls back to the latest timestamp in the package, recorded as an assumption (Task 7 test).

---

### Task 1: Project scaffold, policy config, and core types

**Files:**
- Create: `pyproject.toml`, `preop_triage/__init__.py`, `preop_triage/model.py`, `preop_triage/policy.py`, `config/policy.yaml`
- Modify: `run_baseline.py:1-8`, `run_evals.py:1-8`, `view_report.py:1-8` (remove inline script metadata so `uv run` uses the project)
- Test: `tests/test_policy.py`

**Interfaces:**
- Produces: `model.py` dataclasses `Span(kind: SpanKind, doc_id: str, quote: str, offset: int | None, model_label: str | None, origin: str)`, `Rejection(doc_id, quote, reason, origin)`, `Fact(kind: str, value: Any, observed_at: datetime | None, source: str, doc_id: str | None, quote: str | None, parser: str)`, `ReviewFlag(reason, doc_id, quote, model_label, code_label, rule_id)`, `Evidence(source, doc_id, date, author, quote)`, `Check(rule_id, status, evidence, computed)`, `Issue(category, rule_id, status, summary, evidence, computed, action_needed)`, pydantic `TriageOutput` with the spec's fields. `SpanKind` is a `StrEnum` with the 15 kinds in spec 2.2.
- Produces: `policy.load_policy(path: Path | None = None) -> Policy` (pydantic) with `windows`, `thresholds`, `drugs`, `lab_codes`, `required_tests_by_risk`, `patterns`, `procedure_nouns`, `effective_date`, `version`.

- [ ] Write `tests/test_policy.py`: `test_policy_loads_defaults` asserts `p.windows.hp_days == 30`, `p.windows.lab_days["HIGH"] == 14`, `p.thresholds.sbp == 180`, `p.drugs.classify("Xarelto") == ("rivaroxaban", "anticoagulant")`, `p.drugs.classify("ASA 81 mg PO daily") == ("aspirin", "antiplatelet")`, `p.lab_codes.normalize("LAB-CBC") == "CBC"`, `p.lab_codes.normalize("BMP") == "BMP"`.
- [ ] Run `uv run pytest tests/test_policy.py -v`; expect ImportError.
- [ ] Write `pyproject.toml` (name `preop-triage`, deps `pydantic>=2.8`, `pyyaml>=6`, `anthropic>=2.3`, `textual>=1.0`, `rich>=13`; dev `pytest>=8`), `config/policy.yaml` with values from spec 2.5 and drug/lab/pattern tables, `model.py`, `policy.py`.
- [ ] Run tests; expect PASS. Commit `feat: add policy config and core types`.

### Task 2: Parsers

**Files:**
- Create: `preop_triage/parsers.py`
- Test: `tests/test_parsers.py`

**Interfaces:**
- Produces: `parse_bp(quote: str) -> tuple[int, int] | None`; `parse_temp_f(quote: str) -> tuple[Decimal, str] | None` returning `(temp_f, unit_source)` where `unit_source in {"F", "C"}`, `None` on no match, raises `UnitConflict` when a dual reading disagrees by more than 0.2 F; `parse_time(quote: str) -> time | None`; `parse_date(quote: str) -> date | None`; `c_to_f(c: Decimal) -> Decimal`.

- [ ] Write tests: `test_bp_formats` over `"NIBP 189/112"`, `"BP: 157/117"`, `"Blood pressure 131/72 mmHg"`, `"132/76 mmHg"` -> tuples; `test_temp_fahrenheit` `"T 98.4 F"`, `"Temperature 99.1 °F (oral)"`, `"Temp: 97.9F"`; `test_temp_celsius_boundary` `"T 38.0 C"` -> `Decimal("100.4")`, `"37.9 °C"` -> `Decimal("100.2")`, `"38.6 C"` -> `Decimal("101.5")`; `test_temp_dual_reading_agrees` `"38.6 C (101.5 F)"` -> `101.5`; `test_temp_dual_reading_conflict` `"38.6 C (99.0 F)"` raises `UnitConflict`; `test_time` `"08:30"`; `test_date` `"03/17/2026"` and `"2026-03-17"`, `"3/17/26"` -> None.
- [ ] Run; expect fail. Implement. Run; expect pass. Commit `feat: add deterministic value parsers`.

### Task 3: Grounding gate

**Files:**
- Create: `preop_triage/ground.py`
- Test: `tests/test_ground.py`

**Interfaces:**
- Produces: `ground_spans(spans: list[Span], docs: dict[str, str]) -> tuple[list[Span], list[Rejection]]`. Max quote length 400.

- [ ] Tests: `test_exact_match_sets_offset`, `test_whitespace_normalized_match`, `test_missing_quote_rejected` (reason `NOT_FOUND`), `test_too_long_rejected` (reason `TOO_LONG`), `test_unknown_doc_rejected` (reason `UNKNOWN_DOC`).
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add grounding gate`.

### Task 4: Classification gate

**Files:**
- Create: `preop_triage/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `Policy.patterns` (dict of label -> list of regex strings, grouped by `SpanKind`).
- Produces: `classify(span: Span, policy: Policy) -> tuple[str | None, ReviewFlag | None]`; label from pattern table; review flag when no match or when `span.model_label` disagrees. `classify_doc_kind(title: str, text: str, policy: Policy) -> str` returning `PREOP_HP | NON_QUALIFYING_HP | CONSENT | OTHER`. `procedure_matches(scheduled: str, consent_line: str, policy: Policy) -> bool` by overlap of procedure nouns.

- [ ] Tests: `test_hp_typo_title_is_preop_hp` (`"History and Pyhsical"` + body `"PREOPERATIVE HISTORY AND PHYSICAL"`); `test_wellness_hp_is_non_qualifying` (`"Admission H&P"` + `"annual wellness / establish care"`); `test_consent_titles` for `"Surgery Consent (scanned)"`, `"Pre-op Consent Discussion"`; `test_signature_signed` for `"Patient signature on file - signed in clinic 03/13/2026"`, `"Electronic consent obtained and signed by patient"`, `"Patient signature: /s/ Mia Patel (electronically signed"`; `test_signature_unsigned` for `"Patient signature: pending"`, `"status: NOT COMPLETED"`, `"[Form printed for patient review; not yet signed]"`, `"Patient signature: ______"`; `test_hold_specific` `"Hold Xarelto for the 2 days before surgery (last dose 02/26/2026"`; `test_resume_specific` `"Resume Xarelto 24-48 hours after surgery"`, `"Restart dabigatran on postoperative day 1"`; `test_deferred` `"to be determined by the surgical team"`, `"we will follow up with specific recommendations"`; `test_model_disagreement_flags` (model says SIGNED, code says UNSIGNED -> flag); `test_procedure_match` (`"Elective coronary artery bypass grafting"` vs `"coronary artery bypass grafting"` True; vs `"thyroidectomy"` False; `"Elective total knee arthroplasty"` vs `"total knee arthroplasty"` True).
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add pattern-table classifier`.

### Task 5: Structured ingestion

**Files:**
- Create: `preop_triage/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces: `ingest(submission: dict, policy: Policy) -> tuple[list[Fact], list[ReviewFlag]]`. Fact kinds: `PROCEDURE_DATE`, `PROCEDURE_RISK`, `PROCEDURE_TYPE`, `REVIEW_TIME`, `BP`, `TEMP`, `LAB` (value `{"code": str, "status": str}`), `MED` (value `{"name", "canonical", "class", "active": bool | None}`), `DOC` (value `{"type", "date", "author"}`). `parse_iso(ts: str) -> datetime` naive UTC.

- [ ] Tests: `test_vitals_become_facts_with_timestamps`; `test_bp_missing_diastolic_flagged`; `test_lab_code_normalized`; `test_med_classified` (Xarelto -> rivaroxaban/anticoagulant, active True); `test_null_procedure_date_fact_value_none`; `test_entered_in_error_status_preserved`.
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add structured ingestion`.

### Task 6: Rule-based span extractor

**Files:**
- Create: `preop_triage/extract/__init__.py`, `preop_triage/extract/base.py`, `preop_triage/extract/rules.py`
- Test: `tests/test_extract_rules.py`

**Interfaces:**
- Produces: `base.SpanExtractor` Protocol with `name: str` and `extract(doc_id: str, title: str, text: str) -> list[Span]`; `rules.RuleSpanExtractor(policy: Policy)`.

- [ ] Tests on a nursing note fixture containing `"08:06  NIBP 189/112, T 37.1 C, HR 74"` and `"ADDENDUM 08:30: Re-measured 137/78 mmHg"`: `test_finds_both_bp_spans`, `test_finds_temp_span`, `test_finds_vitals_deferred` (`"VITAL SIGNS: See flowsheet."`), `test_finds_med_mention` (`"Patient also reports taking Eliquis 5 mg (BID)"`), `test_finds_hold_and_resume`, `test_finds_doc_absence` (`"H&P: not yet on file"`), `test_empty_text_no_spans` (`text=""` and `text=None`), `test_all_spans_ground` (every span re-grounds via Task 3).
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add rule-based span extractor`.

### Task 7: Policy engine

**Files:**
- Create: `preop_triage/engine.py`, `preop_triage/facts.py` (span -> fact conversion using Tasks 2 and 4)
- Test: `tests/test_facts.py`, `tests/test_engine.py`

**Interfaces:**
- Produces: `facts.spans_to_facts(spans: list[Span], docs: dict[str, Document], policy: Policy) -> tuple[list[Fact], list[ReviewFlag]]` where text facts use kinds `BP`, `TEMP`, `DOC_KIND`, `CONSENT_SIG`, `CONSENT_PROC`, `MED_MENTION`, `ANTICOAG_HOLD`, `ANTICOAG_RESUME`, `ANTICOAG_DEFERRED`, `DOC_ABSENCE`, `VITALS_DEFERRED`, each with `observed_at` from document date + parsed time. `engine.evaluate(facts, review_flags, policy) -> EngineResult(decision, issues, checks, assumptions, review_flags)`.

- [ ] `test_facts.py`: `test_bp_span_timestamp_uses_doc_date_and_time`; `test_temp_celsius_converted_with_converted_from`; `test_unparseable_grounded_span_becomes_flag`.
- [ ] `test_engine.py` with a `make_facts(**overrides)` helper building a READY baseline: `test_ready_baseline`; `test_hp_30_days_ready` / `test_hp_31_days_outdated`; `test_hp_missing`; `test_hp_only_wellness_non_qualifying`; `test_consent_missing` / `test_consent_unsigned_then_signed_ready` / `test_consent_wrong_procedure` / `test_consent_signed_without_procedure_line_unverified`; `test_cbc_15_days_high_fails` / `test_cbc_14_days_high_passes`; `test_bmp_not_cmp`; `test_entered_in_error_newest_lab_skipped`; `test_cbc_missing`; `test_anticoag_no_plan` / `test_anticoag_hold_only_incomplete` / `test_anticoag_deferred_incomplete` / `test_anticoag_stale_plan_other_drug` / `test_antiplatelet_needs_no_plan` / `test_inactive_anticoag_needs_no_plan` / `test_patient_reported_anticoag_counts_with_assumption` / `test_unknown_active_is_missing_data_not_anticoag`; `test_dbp_110_not_cleared` / `test_dbp_109_ready`; `test_temp_100_4_ready` / `test_temp_100_5_not_cleared`; `test_latest_vital_wins_recheck_clears`; `test_text_vital_newer_than_structured`; `test_tie_structured_wins_with_assumption`; `test_missing_temp_is_missing_data`; `test_no_vitals_is_single_missing_data_issue`; `test_null_date_skips_windows_but_checks_presence`; `test_null_risk_skips_r2`; `test_review_time_fallback_assumption`; `test_not_cleared_plus_documentation_lists_both`; `test_review_flag_forces_needs_follow_up`.
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add policy engine`.

### Task 8: Output invariants

**Files:**
- Create: `preop_triage/invariants.py`
- Test: `tests/test_invariants.py`

**Interfaces:**
- Produces: `check_invariants(output: TriageOutput, docs: dict[str, str], submission: dict) -> TriageOutput` returning the same output or a `NEEDS_FOLLOW_UP` output with a `SYSTEM_ERROR` review flag.

- [ ] Tests: `test_ready_with_issue_becomes_system_error`; `test_not_cleared_without_r4_evidence_becomes_system_error`; `test_evidence_quote_must_reground`; `test_serialization_is_stable`; `test_valid_output_passes_unchanged`.
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add output invariant gate`.

### Task 9: Pipeline and core integration

**Files:**
- Create: `preop_triage/pipeline.py`
- Modify: `core.py` (keep schemas; `triage_submission` delegates; add `extractor` kwarg), `tests/test_triage_submission.py` (replace OpenAI mock tests with pipeline tests)
- Test: `tests/test_pipeline_cases.py`

**Interfaces:**
- Produces: `pipeline.run(submission: dict, *, policy: Policy, extractor: SpanExtractor) -> TriageOutput`; `core.triage_submission(submission, *, model: str, extractor: str = "rules", use_cache: bool = True) -> TriageOutput`.

- [ ] `test_pipeline_cases.py`: load the 50 cases, run with `RuleSpanExtractor`, compute decision and category matches, assert `decision_match >= 0.90` and `category_match >= 0.85`, and print the per-case misses. Also `test_pipeline_is_byte_deterministic` (two runs of case 16 serialize identically) and `test_document_with_null_text`.
- [ ] Run; fail. Implement, then iterate on pattern tables in `config/policy.yaml` until the floors hold. Run; pass. Commit `feat: wire pipeline into triage_submission`.

### Task 10: Claude extractor with cache and union

**Files:**
- Create: `preop_triage/extract/claude.py`, `preop_triage/extract/cache.py`, `preop_triage/extract/union.py`
- Test: `tests/test_extract_claude.py`

**Interfaces:**
- Produces: `cache.ExtractionCache(root: Path)` with `key(text, prompt_version, model) -> str`, `get(key) -> list[dict] | None`, `put(key, spans)`; `claude.ClaudeSpanExtractor(model: str, policy: Policy, cache: ExtractionCache | None, client=None)` using `client.messages.create` with `output_config={"format": {"type": "json_schema", "schema": SPAN_SCHEMA}}`, `output_config.effort="medium"`, `max_tokens=8192`; `PROMPT_VERSION = "1"`. API errors return `[]` and set `self.last_error`. `union.UnionSpanExtractor(*extractors)` concatenates with origin preserved.

- [ ] Tests with a fake client object: `test_parses_spans_from_json_text`; `test_cache_hit_skips_client`; `test_api_error_returns_empty_and_records`; `test_refusal_stop_reason_returns_empty`; `test_union_preserves_origin`.
- [ ] Run; fail. Implement. Run; pass. Commit `feat: add cached Claude span extractor`.

### Task 11: Harness: runner, local evals, Makefile

**Files:**
- Modify: `run_baseline.py` (`--extractor`, `--no-cache`, `--policy`), `run_evals.py` (drop OpenAI Evals; add `per_category` precision/recall, `confusion` matrix, `diff_vs_previous`), `Makefile` (`MODEL ?= claude-opus-5-5`, `EXTRACTOR ?= rules`, `make test` via project), `.gitignore` (`data/cache/`)
- Test: `tests/test_evals.py`

**Interfaces:**
- Produces: `run_evals.score(cases, outputs) -> dict` with keys `local_metrics_summary`, `per_category`, `confusion`, `records` (same record shape the TUI reads).

- [ ] Tests: `test_per_category_precision_recall`; `test_confusion_matrix_counts`; `test_report_shape_has_tui_fields`.
- [ ] Run; fail. Implement. Run `make baseline evals score` with `EXTRACTOR=rules`; expect a score printed. Commit `feat: local evals with per-category metrics`.

### Task 12: Write-up and README

**Files:**
- Create: `WRITEUP.md`
- Modify: `README.md` (build/run instructions, extractor modes, key setup)

- [ ] Write `WRITEUP.md`: approach, the grounding principle, the five gates, the trade-off on recall, results table (rules extractor; Claude extractor if a key was available), assumptions from spec section 7, what I would do next.
- [ ] Commit `docs: write-up and README`.

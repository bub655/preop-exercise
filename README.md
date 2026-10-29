# Pre-Op Triage Take-Home

Deterministic, grounded triage for pre-op scheduling. The model (optionally)
locates text; code makes every decision. See `WRITEUP.md` for the approach and
`docs/superpowers/specs/` for the design spec.

## Quick start

```bash
uv --version          # uv is the only prerequisite
make test             # 330+ unit and integration tests, no API key needed
make baseline evals score   # runs the 50 sample cases offline and scores them
make report           # interactive TUI over the eval report
make synth            # 500 synthetic held-out cases; reports fail-open and fail-closed rates
make determinism      # replays one case 10x; prints decision, decision-bearing, and full-bytes stability
```

The ten hand-written held-out cases score the same way:

```bash
make baseline evals INPUT=data/heldout_cases.jsonl OUTPUT=data/heldout_outputs.jsonl REPORT=data/heldout_report.json
```

No API key is needed for the default `rules` extractor. To exercise the model
extractor:

```bash
export ANTHROPIC_API_KEY="<your key>"
make baseline evals EXTRACTOR=claude   # or EXTRACTOR=union
make determinism EXTRACTOR=claude CACHE_FLAG=--no-cache   # 10 cold replays; expect decision-bearing 100%, full bytes lower (advisory flags vary)
```

Model responses are cached under `data/cache/extractions/` keyed by document
hash, prompt version, and model, so repeated runs are free and byte-identical.

## Layout

```
core.py                 triage_submission(submission, *, model, extractor="rules") entry point
preop_triage/
  ingest.py             structured JSON -> Facts
  extract/rules.py      regex span extractor (offline)
  extract/claude.py     Claude span extractor (cached)
  extract/union.py      both, with origin tags
  ground.py             verbatim-quote gate
  parsers.py            BP, temperature (exact C->F), time, date parsers
  classify.py           pattern-table classifier, doc-kind and procedure matching
  facts.py              grounded spans -> Facts | ReviewFlags
  engine.py             policy rules R0-R4, recency, precedence
  invariants.py         output assertions; demotes to NEEDS_FOLLOW_UP on violation
  pipeline.py           orchestrator
config/policy.yaml      every number, drug table, and phrase pattern
run_baseline.py         runs the pipeline over a JSONL (flags: --extractor, --no-cache, --policy)
run_evals.py            local scoring: schema, decision, category set, per-category P/R, confusion, diff
run_synth.py            synthetic held-out generator (preop_triage/synth.py) with fail-open / fail-closed report
scripts/make_heldout_cases.py  builds data/heldout_cases.jsonl, ten hand-written held-out cases
view_report.py          TUI (unchanged from the starter)
tests/                  parsers, grounding, classification, ingestion, engine, invariants, pipeline, evals
```

## Output

```json
{
  "decision": "NOT_CLEARED",
  "issues": [{
    "category": "ACUTE_SAFETY_EXCLUSION", "rule_id": "R4", "status": "BP_EXCLUSION",
    "summary": "Most recent blood pressure 157/117 mmHg on 2026-03-03 meets the exclusion threshold (diastolic >= 110).",
    "evidence": [{"source": "documents:7aef...", "doc_id": "7aef...", "date": "2026-03-03", "author": "Jasmine O. Nguyen, BSN, RN", "quote": "BP 157/117"}],
    "computed": {"observed_date": "2026-03-03T09:05:00", "observed_value": "157/117 mmHg", "threshold": "diastolic >= 110"},
    "action_needed": "Hold scheduling; recheck blood pressure and document a within-range reading."
  }],
  "explanation": "NOT_CLEARED. Not satisfied: R4 fail (Acute safety exclusion present). Satisfied: R0, R1.1, R1.2, R2. Not applicable: R3.",
  "checks": ["... one entry per rule with PASS/FAIL/SKIPPED/UNVERIFIED and evidence ..."],
  "review_flags": [], "assumptions": [], "rejections": [],
  "provenance": {"policy_version": "2026-01-01", "extractor": "rules", "pipeline_version": "0.1.0"}
}
```

## Scoring

Unchanged from the starter: JSON validity, exact decision match, exact
issue-category set match, equally weighted. `run_evals.py` now scores locally
(no hosted evals round trip) and adds per-category precision/recall, a decision
confusion matrix, and a diff against the previous report.

## Data

- `data/patients_sample_50.jsonl`: `case_id`, `submission`, `label`
- `data/heldout_cases.jsonl`: ten hand-written held-out cases, same shape
- `data/baseline_outputs.jsonl`, `data/eval_report.json`,
  `data/determinism_report.json`: generated

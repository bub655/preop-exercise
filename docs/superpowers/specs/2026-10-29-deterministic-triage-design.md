# Deterministic Pre-Op Triage: Design Spec

Date: 2026-10-29
Status: approved in conversation; implementation in progress
Upstream: https://github.com/cadencerpm/preop-exercise

## 1. Goal and non-goals

Replace the single-call LLM baseline with a triage system where **no value that a
decision depends on is produced by a model**. The model only locates text. Code
confirms the text exists, parses it, classifies it, applies the policy, and checks
its own output before emitting it. Anything the code cannot verify fails closed to
`NEEDS_FOLLOW_UP` with an explicit review flag.

Success criteria:

- Decision and issue categories are a pure function of the input package and the
  policy config. Same input, same bytes out.
- Every issue carries evidence that re-grounds verbatim in the source package.
- The pipeline runs end to end without an API key (rule-based extractor), and the
  model's marginal value can be measured when a key is present.
- The harness, Makefile targets, and `triage_submission` entry point keep working.
- Policy numbers, drug tables, and phrase patterns live in config, not code.

Non-goals: a nurse-facing UI beyond the existing TUI, multi-policy versioning
logic beyond an effective date, and any clinical criteria outside the appendix.

## 2. Pipeline

```
submission JSON
   |
   v
[1] ingest.py        structured fields -> Facts            (pure Python)
   |
   v
[2] extract/*        documents -> Spans                    (Claude or rules; cached)
   |
   v
[3] ground.py        Spans -> grounded Spans + Rejections  (verbatim match)
   |
   v
[4] parsers.py       grounded Spans -> Facts | ReviewFlags (regex parsers, closed tables)
    classify.py      semantic Spans -> labels | ReviewFlags (pattern tables, model label cross-check)
   |
   v
[5] engine.py        Facts -> Checks, Issues, decision     (policy config, recency, precedence)
   |
   v
[6] invariants.py    Result -> Result | SYSTEM_ERROR       (assertions on the output)
   |
   v
TriageOutput JSON (decision, issues, explanation, checks, review_flags, assumptions, rejections, provenance)
```

### 2.1 Structured ingestion

`procedure`, `vitals[]`, `labs[]`, `medications[]`, `conditions[]`, `metadata` are
parsed by Python into `Fact` objects with `source` set to the JSON path
(`labs[2]`). Dates are parsed with explicit formats. Unknown or null values become
`None` and are reported by the engine as missing data; they are never defaulted.

### 2.2 Span extraction

`SpanExtractor` protocol: `extract(doc: Document) -> list[Span]`.

- `RuleSpanExtractor`: regex over the document text using the same pattern tables
  the classifier uses. Deterministic, offline, the floor for recall.
- `ClaudeSpanExtractor`: one `messages.create` per document with
  `output_config.format` JSON schema, `claude-opus-5-5`, effort `medium`.
  Prompt lists the closed `SpanKind` enum and asks for verbatim quotes only.
  Responses are cached on disk keyed by sha256 of document text + prompt
  version + model id. Any API error yields zero spans for that document and a
  `ReviewFlag(reason="EXTRACTOR_ERROR")`; it never raises out of the pipeline.
- `UnionSpanExtractor`: both, with each span tagged by its origin so provenance
  shows what the model added beyond the rules.

A `Span` is untrusted: `kind`, `doc_id`, `quote`, `offset`, `model_label`
(optional, for semantic kinds), `origin`.

`SpanKind` (closed enum): `VITAL_BP`, `VITAL_TEMP`, `VITALS_DEFERRED`,
`DOC_TITLE`, `DOC_ENCOUNTER_KIND`, `PLANNED_PROCEDURE`, `CONSENT_PROCEDURE`,
`CONSENT_SIGNATURE`, `MED_MENTION`, `ANTICOAG_HOLD`, `ANTICOAG_RESUME`,
`ANTICOAG_DEFERRED`, `DOC_ABSENCE`, `PROCEDURE_DATE_STATEMENT`,
`PROCEDURE_RISK_STATEMENT`.

### 2.3 Grounding gate

For each span: exact substring match in the document text, else a
whitespace-normalized match. On success the span's offset is replaced by the
found offset. On failure the span becomes a `Rejection(reason="NOT_FOUND")`.
A span whose quote is longer than 400 characters is rejected (`TOO_LONG`) so a
model cannot quote the whole document to satisfy grounding.

### 2.4 Parse and classify gates

Value-bearing kinds go through parsers that read only the grounded quote:

- Blood pressure: `(\d{2,3})\s*/\s*(\d{2,3})` with optional `mmHg`, `NIBP`, `BP`.
- Temperature: number + unit (`C`, `°C`, `F`, `°F`); Celsius converted with
  `Fraction` arithmetic and rounded to one decimal place; a parenthesized dual
  reading uses the Fahrenheit value only if it agrees with the conversion within
  0.2 F, otherwise `ReviewFlag(reason="UNIT_CONFLICT")`.
- Time of day: `HH:MM` immediately preceding or following the reading within the
  same line; combined with the document date. No time means 00:00 of the
  document date.
- Drug normalizer: closed table in config mapping brand and generic names to a
  canonical generic and class (`anticoagulant`, `antiplatelet`, `other`).
- Dates inside quotes: `MM/DD/YYYY` or `YYYY-MM-DD`; two-digit years rejected.

Semantic kinds go through `classify.py`, which labels the grounded quote with
pattern tables from config and compares with `model_label` when present:

- `DOC_TITLE` + `DOC_ENCOUNTER_KIND` -> `PREOP_HP`, `NON_QUALIFYING_HP`,
  `CONSENT`, `OTHER`.
- `CONSENT_SIGNATURE` -> `SIGNED`, `UNSIGNED`.
- `MED_MENTION` -> `TAKING`, `NOT_TAKING`, `UNKNOWN`.
- `ANTICOAG_HOLD` / `ANTICOAG_RESUME` -> `SPECIFIC` (verb + timing) or
  `VAGUE`; `ANTICOAG_DEFERRED` -> `DEFERRED`.
- `DOC_ABSENCE` -> `HP_ABSENT`, `CONSENT_ABSENT`.

Agreement passes. Disagreement, or no pattern match, yields a `ReviewFlag` with
both labels. A review flag on a rule's inputs forces that rule to `UNVERIFIED`,
which the engine treats as not satisfied.

### 2.5 Policy engine

Inputs: facts, review flags, policy config. Rules, in order:

- **R0 missing data.** `procedure_date` null, `procedure_risk` null, no BP fact,
  no temperature fact, or an anticoagulant with unknown active status ->
  `MISSING_REQUIRED_DATA`. Date null disables window checks; risk null disables
  R2. Presence checks still run.
- **R1.1 H&P.** Among documents classified `PREOP_HP`, take the most recent by
  document date. Required: `0 <= (procedure_date - doc_date).days <= 30`.
  None present -> `MISSING`; only `NON_QUALIFYING_HP` present -> `NON_QUALIFYING`
  plus review flag; outside window -> `OUTDATED`. All -> `REQUIRED_DOCUMENTATION`.
- **R1.2 consent.** Documents classified `CONSENT`. Satisfied when at least one
  has signature `SIGNED` and a `CONSENT_PROCEDURE` that matches the scheduled
  procedure by token overlap on the procedure-noun table in config. Otherwise
  `MISSING`, `UNSIGNED`, or `WRONG_PROCEDURE` -> `REQUIRED_DOCUMENTATION`.
- **R2 testing.** Required codes by risk from config. Lab codes normalized by a
  config table (`LAB-CBC` -> `CBC`; `BMP` is not `CMP`). Only `status == final`
  results count. Most recent qualifying result must satisfy
  `0 <= days <= window`. Missing or outdated -> `REQUIRED_TESTING`.
- **R3 anticoagulation.** Patient is anticoagulated when any medication fact of
  class `anticoagulant` is active, from the med list or a `MED_MENTION` labeled
  `TAKING` (recorded as an assumption when the drug is absent from the list).
  Inactive list entries confirmed by a `NOT_TAKING` mention do not count.
  Satisfied when, for each active anticoagulant, a `SPECIFIC` hold span and a
  `SPECIFIC` resume span exist in the same document that names that drug or its
  canonical equivalent. Otherwise `ANTICOAGULATION_MANAGEMENT` with status
  `NO_PLAN`, `INCOMPLETE_PLAN`, or `STALE_PLAN` (plan names a different drug).
- **R4 acute safety.** Merge structured and text vitals per type, sort by
  timestamp, take the latest on or before `submission_received_at`. BP excluded
  when `systolic >= 180 or diastolic >= 110`; temperature excluded when
  `temp_f > 100.4` after one-decimal rounding. -> `ACUTE_SAFETY_EXCLUSION`.

Decision: any R4 issue -> `NOT_CLEARED`; else any issue or review flag or
unverified rule -> `NEEDS_FOLLOW_UP`; else `READY`. All issues are always listed.
Issues are de-duplicated by category for scoring but each keeps its own rule and
evidence entries.

### 2.6 Output invariants

Before emit, assert: no `READY` with issues, review flags, or unverified rules;
`NOT_CLEARED` only with an R4 issue backed by a grounded vital fact; every issue
has at least one evidence entry whose quote re-grounds in the package; every
category maps to a rule that fired; serializing twice yields identical bytes.
A failed assertion emits `NEEDS_FOLLOW_UP` with a `SYSTEM_ERROR` review flag and
the assertion message. It never raises out of `triage_submission`.

## 3. Output schema

`TriageOutput` keeps `decision`, `issues`, `explanation` and adds:

```
issues[]:        category, rule_id, status, summary, evidence[], computed, action_needed
evidence[]:      source, doc_id, date, author, quote
computed:        procedure_date, observed_date, days_before, window_days,
                 threshold, observed_value, converted_from (all optional)
checks[]:        rule_id, status (PASS|FAIL|SKIPPED|UNVERIFIED), evidence[], computed
review_flags[]:  reason, doc_id, quote, model_label, code_label
assumptions[]:   text, evidence[]
rejections[]:    doc_id, quote, reason, origin
provenance:      policy_version, extractor, model, prompt_version, cache_hits,
                 pipeline_version
```

`explanation` is a short deterministic string built from the checks.

## 4. Repository layout

```
preop_triage/
  __init__.py
  model.py          dataclasses and pydantic output models
  policy.py         loads and validates config/policy.yaml
  ingest.py
  extract/{base,rules,claude,cache,union}.py
  ground.py
  parsers.py
  classify.py
  engine.py
  invariants.py
  pipeline.py       triage_submission orchestrator
config/policy.yaml  windows, thresholds, drug tables, lab code map, pattern tables,
                    procedure-noun table, effective date
core.py             keeps schemas; triage_submission delegates to pipeline
run_baseline.py     adds --extractor rules|claude|union, --no-cache
run_evals.py        local scoring only; per-category precision/recall, confusion
                    matrix, diff against previous report; determinism mode kept
tests/              parsers, ground, classify, engine (synthetic boundaries),
                    invariants, pipeline on the 50 cases with the rules extractor
docs/superpowers/   this spec and the implementation plan
WRITEUP.md          approach, design decisions, assumptions, results
```

## 5. Error handling

- Model call failure: zero spans for that document, `EXTRACTOR_ERROR` review flag.
- Malformed submission field: the field is `None`, reported as missing data.
- Parser or classifier failure on grounded text: review flag, rule unverified.
- Invariant failure: `SYSTEM_ERROR`, decision `NEEDS_FOLLOW_UP`.
- Nothing in the pipeline raises out of `triage_submission` except a config
  load error at startup, which is a deployment bug and should be loud.

## 6. Testing and evaluation

- Unit tests per gate with synthetic inputs, including boundaries: 30 vs 31
  days, 14 vs 15, diastolic 109 vs 110, 100.4 vs 100.5 F, 38.0 C, entered-in-
  error newest lab, BMP vs CMP, pending then signed consent, wrong-procedure
  consent, inactive anticoagulant, patient-reported anticoagulant, stale plan.
- Pipeline test on the 50 labeled cases with the rules extractor, asserting the
  aggregate score does not regress below a committed floor.
- `make evals` scores locally with per-category metrics and writes the same
  report shape the TUI reads.
- `make determinism` replays one case N times; `--no-cache` exercises the model.

## 7. Stated assumptions

- Windows are inclusive at the boundary.
- "At the time of review" is the latest reading on or before
  `submission_received_at` across structured and free-text sources.
- "See flowsheet" and "vitals deferred" are not readings.
- A patient-reported anticoagulant is treated as active. Unknown status is
  missing data, not an anticoagulation issue.
- Consent must name the scheduled procedure. Any signed, matching consent
  satisfies the rule regardless of earlier unsigned forms.
- A plan needs both a hold and a restart with timing. Deferral to another team
  is not a plan.
- Wellness or establish-care notes titled H&P do not satisfy R1.1.
- Missing temperature or blood pressure is missing data, never an exclusion.

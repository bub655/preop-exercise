# Pre-Op Scheduling Triage: Approach and Design Decisions

## The one-sentence version

The model never produces a value that a decision depends on. It only points at
text. Code confirms the text exists, parses it, classifies it, applies the
policy, and checks its own output before anything leaves the pipeline. Whatever
code cannot verify fails closed to `NEEDS_FOLLOW_UP` with a review flag.

## Why not improve the single-call baseline?

The starter sends the whole package to a model and asks for a decision. For a
scheduling decision in a surgical center that has three problems the labeled
data makes obvious:

1. **The deciding facts are arithmetic and recency, not language.** Thirty days
   passes and thirty-one fails. A diastolic of exactly 110 excludes. 37.9 C is
   100.2 F and fine; 38.6 C is a fever. The newest reading is often a nursing
   note two days after the structured vitals, and an 08:30 addendum overrides
   an 08:06 reading. Models are unreliable at exactly these things, and a
   prompt cannot make them reliable.
2. **A paraphrased "evidence" string is not evidence.** A nurse reviewing every
   non-READY case needs to open the document and find the sentence. A quote
   that was lightly reworded, or that never existed, costs more time than no
   evidence at all and erodes trust in every other packet.
3. **Determinism.** The same package must produce the same bytes. A model with
   temperature zero is usually stable; "usually" is not a property you can put
   in a policy document.

## Architecture

```
submission JSON
   |
   v
[1] ingest        structured fields -> Facts                (pure Python)
   v
[2] extract       documents -> Spans                        (rules, or Claude, cached)
   v
[3] ground        Spans -> grounded Spans + Rejections      (verbatim match in the document)
   v
[4] parse/classify grounded Spans -> Facts | ReviewFlags    (regex parsers, closed pattern tables)
   v
[5] engine        Facts -> decision, issues, checks         (policy config, recency, precedence)
   v
[6] invariants    output -> output | SYSTEM_ERROR           (assertions on the result itself)
```

A `Span` is untrusted: a kind from a closed enum, a document id, and a quote.
A `Fact` is trusted: a value code derived from either a structured field or a
grounded quote, with its source path, timestamp, and parser name attached.
Only Facts reach the engine.

### The five gates

1. **Grounding.** The quote must appear verbatim in the document, with
   whitespace normalization as the single tolerance. Quotes over 400 characters
   are rejected so a model cannot quote the whole note to pass. A rejected span
   is recorded, never repaired.
2. **Parsing.** Values come only from the grounded quote through deterministic
   parsers: blood pressure, temperature with unit detection, time of day, dates,
   and a closed drug table. Celsius conversion uses exact rational arithmetic so
   38.0 C is exactly 100.4 F. A parser failure on a grounded quote is a review
   flag, not a guess.
3. **Classification.** For semantic kinds (is this a patient signature or a
   physician attestation; is this a hold instruction with timing or a vague
   one) code labels the grounded quote against pattern tables in
   `config/policy.yaml`. If the model suggested a label, it is compared with
   the code's label. Disagreement or no match is a review flag.
4. **Consistency.** Structured and free-text vitals merge into one timeline and
   the latest reading on or before the review time wins. A drug mentioned in a
   note but absent from the medication list is treated as active and recorded
   as an assumption. A consent whose procedure line does not match the
   scheduled procedure is `WRONG_PROCEDURE`. Medication mentions are resolved
   by recency so an old "maintained on warfarin" cannot contradict a later
   "switched to apixaban".
5. **Output invariants.** Before emitting: no `READY` with issues or flags;
   `NOT_CLEARED` only when backed by a grounded vital; every issue has evidence
   whose quote re-grounds; every category matches the rule that fired; the
   output serializes identically twice. A violation demotes the result to
   `NEEDS_FOLLOW_UP` with a `SYSTEM_ERROR` flag. Nothing past extraction
   raises.

### Where the model is left

One job: for each document, emit candidate spans with a kind and a verbatim
quote, through `output_config.format` with a strict JSON schema. If it misses
a span, the engine sees a missing fact and fails closed. If it invents a span,
gate 1 removes it. If it mislabels a span, gate 3 catches it. The only way a
hallucination reaches a decision is if the model fabricates text that happens
to exist verbatim in the document and code then parses and classifies it the
same wrong way, which is no longer a hallucination.

The `SpanExtractor` interface has three implementations, chosen with
`--extractor`:

- `rules`: regex over the document using the same pattern tables as the
  classifier. Deterministic, offline, no key. This is the recall floor.
- `claude`: `claude-opus-5-5`, effort `medium`, cached on disk by
  sha256(document text, prompt version, model). API errors, refusals, and
  truncated responses yield zero spans for that document and a review flag.
- `union`: both, with each span tagged by origin, so provenance shows exactly
  what the model found beyond the rules.

Everything after extraction is identical across the three, which is what makes
the model's marginal value measurable instead of assumed.

### Policy as data

Windows, thresholds, required tests by risk, the lab-code map (`BMP` is not
`CMP`), the anticoagulant and antiplatelet tables, the procedure-noun table for
consent matching, and every phrase pattern live in `config/policy.yaml` with
an effective date. The engine contains no clinical numbers.

## Results on the 50 labeled cases

| Extractor | Decision match | Category set match | Aggregate | Determinism (10 runs) |
|-----------|---------------:|-------------------:|----------:|----------------------:|
| rules     | 50/50          | 50/50              | 100%      | 100% byte-identical   |
| claude    | not run        | not run            |           |                       |

The `claude` and `union` rows were not run because no Anthropic key was
available in the environment where this was built. The code path is covered by
tests with a fake client (parsing, caching, error, refusal, and truncation
handling). To run it: set `ANTHROPIC_API_KEY`, then
`make baseline evals EXTRACTOR=claude`. Cached extractions in `data/cache/`
make a second run free and byte-identical.

Per-category precision and recall are all 1.0 on the sample. The sample is
small and the pattern tables were built against it, so the honest reading is
"the architecture handles every trap the labelers set" rather than "the
patterns generalize". The harness reports per-category precision and recall
and a diff against the previous run precisely so that regressions on a larger
set show up per case.

## The evidence packet

Each issue carries `category`, `rule_id`, a `status` enum
(`MISSING`, `OUTDATED`, `UNSIGNED`, `WRONG_PROCEDURE`, `NO_PLAN`,
`INCOMPLETE_PLAN`, `STALE_PLAN`, `BP_EXCLUSION`, ...), a one-sentence summary,
`evidence[]` entries pointing at a JSON path or a document id with the exact
quote, a `computed` block with the dates, day counts, window, threshold, and
unit conversion that drove the call, and an `action_needed` line.

Each result also carries `checks[]` (every rule with PASS, FAIL, SKIPPED, or
UNVERIFIED and its evidence, so sampled READY cases are auditable too),
`assumptions[]` (every judgment the engine made and the text it made it on),
`review_flags[]`, `rejections[]` (spans the model proposed that did not ground),
and `provenance` (policy version, extractor, model, prompt version, cache hits).

## Stated assumptions

- Windows are inclusive at the boundary. Thirty days passes.
- "At the time of review" is the latest reading on or before
  `submission_received_at`, across structured and free-text sources. Later
  readings are ignored and recorded.
- "See flowsheet" and "vitals deferred" are not readings.
- A patient-reported anticoagulant counts as active. An unknown status is
  `MISSING_REQUIRED_DATA`, not an anticoagulation issue, and R3 is skipped.
- Consent must name the scheduled procedure. Any signed, matching consent
  satisfies the rule regardless of earlier unsigned forms.
- A plan needs both a hold and a restart with timing, in the same document,
  naming the drug the patient actually takes. Deferral to another team is not a
  plan.
- Wellness and establish-care notes titled "H&P" do not satisfy the H&P rule.
- Missing temperature or blood pressure is missing data, never an exclusion.
- An unverifiable vital (bad timestamp, unit conflict) routes to
  `MISSING_REQUIRED_DATA` so a human looks, rather than to an exclusion the
  text does not support.
- A structured and a free-text reading with the identical timestamp resolve in
  favor of the structured value, recorded as an assumption.

## What the starter did that this fixes

New client per call, no retries, sorted-key JSON that scrambles document order,
`strict: False`, no temperature or seed, no caching, evidence as free text, and
the eval script round-tripping through a hosted evals API. All replaced.

## Trade-offs and what I would do next

- **Recall now depends on the pattern tables.** A phrasing the tables do not
  cover becomes a review flag instead of a fact. That is the right trade in
  this setting, and the write-up says so plainly. The Claude extractor exists
  to find spans the rules miss; a span it finds still has to pass the same
  gates, so the pattern tables remain the ceiling on what counts.
- **Next:** run the `union` extractor on a larger labeled set and mine the
  `rejections` and `review_flags` to grow the pattern tables from real
  phrasings; add golden extraction fixtures so the model path is tested
  offline; version the policy config per effective date; surface
  `checks[]` and `assumptions[]` in the TUI.

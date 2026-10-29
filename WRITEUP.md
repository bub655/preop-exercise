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
  truncated responses raise a typed extractor error; the pipeline turns that
  into a blocking `EXTRACTOR_ERROR` flag and a `MISSING_REQUIRED_DATA` issue,
  so an unread document can never produce `READY`.
- `union`: both, with each span tagged by origin, so provenance shows exactly
  what the model found beyond the rules.

Everything after extraction is identical across the three, which is what makes
the model's marginal value measurable instead of assumed.

One consequence is worth stating plainly. For semantic kinds (signatures,
plans, medication mentions) the rules extractor already finds every phrasing
the pattern tables recognize. A model span those tables do not recognize
therefore carries no verified information: it may be a real phrasing the
tables lack, or noise such as a physician attestation quoted as a patient
signature. Such spans become **advisory** review flags: they are listed in the
output for the reviewer, but they do not change the decision or add a scored
issue. A model label that *contradicts* the code's label on a recognized
phrasing is a blocking flag, because that is a genuine conflict. Where the
model can add real value is in locating values (vitals in unusual layouts),
which the parsers verify independently.

### Policy as data

Windows, thresholds, required tests by risk, the lab-code map (`BMP` is not
`CMP`), the anticoagulant and antiplatelet tables, the procedure-noun table for
consent matching, and every phrase pattern live in `config/policy.yaml` with
an effective date. The engine contains no clinical numbers.

## Generalization: a held-out synthetic set

The pattern tables were first built against the 50 sample cases, and a score
on those cases cannot show whether the system generalizes. So the repo includes
a synthetic case generator (`preop_triage/synth.py`, `make synth`) whose
phrasing banks were written from general clinical documentation rather than
copied from the sample: nine H&P title variants, twelve unsigned and ten signed
consent phrasings, eight hold and nine restart phrasings, ten blood-pressure
layouts including `SBP 182, DBP 96`, Celsius and unit-less temperatures,
structured `value_c` vitals and `bp`/`temp` type aliases, CPT and display-name
lab codes, seven anticoagulants, unlisted procedures with laterality, and
unknown risk strings. Ground truth comes from the scenario parameters, never
from the engine.

Errors are split into **fail-open** (an exclusion or follow-up the system
cleared; must be zero) and **fail-closed** (a follow-up it did not need; the
recall cost).

| Run | Cases | Fully correct | Fail-open | Fail-closed |
|-----|------:|--------------:|----------:|------------:|
| Before broadening (tables tuned on the sample only) | 400 | 24% | 30% | 46% |
| After broadening, 5 seeds | 2000 | 100% | 0% | 0% |

The broadening pass changed parsers (temperature unit words and inference,
`SBP/DBP` pairs, month-name dates), structured ingestion (vital type aliases,
`value_c`, CPT and display-name lab codes, an `UNKNOWN_VITAL_TYPE` flag so an
unrecognized vital can never be dropped silently), the pattern tables (cue plus
negation rather than enumerated sentences, with negation winning for
signatures and medication mentions), consent matching (shared content words
when neither side names a listed procedure, laterality), and plan attribution
(a drugless "anticoagulation to be resumed 48 hours post-op" inherits the one
anticoagulant its document discusses, recorded as an assumption).

Two caveats, stated plainly. First, the generator and the tables were written
by the same author, so 100% on it is a lower bound on generalization, not
proof. A phrasing neither anticipated still fails closed, which is the
property that matters, and the generator exists so the next unanticipated
phrasing becomes a test rather than an incident. Second, the broadening did
surface one real tension with the sample: "per surgical team" appended to a
complete restart instruction is attribution, not deferral, and the tables now
distinguish the two.

## Results on the 50 labeled cases

| Extractor | Set | Decision match | Category set match | Aggregate | Determinism |
|-----------|-----|---------------:|-------------------:|----------:|-------------|
| rules     | 50 sample cases | 50/50 | 50/50 | 100% | 10 runs byte-identical |
| rules     | synthetic held-out, 5 seeds | 2000/2000 | 2000/2000 | 100% | n/a |
| rules     | 10 hand-written held-out | 10/10 | 10/10 | 100% | n/a |
| claude    | 50 sample cases | 50/50 | 50/50 | 100% | 10 no-cache runs: decision 100%, exact bytes 50% (see below) |
| claude    | 10 hand-written held-out | 10/10 | 10/10 | 100% | n/a |
| union     | 50 sample cases | 50/50 | 50/50 | 100% | from cache |
| union     | 10 hand-written held-out | 10/10 | 10/10 | 100% | from cache |

The `claude` runs used `claude-opus-5-5` at effort `medium`, one call per
document, about 290 documents across both sets, roughly 3 seconds and 1,500
input tokens each. Three observations matter more than the scores:

- **Zero rejected spans.** Across every document the model never returned a
  quote that failed to ground. Nothing it proposed had to be thrown away for
  not existing in the source.
- **115 advisory flags, zero blocking ones.** The model also quoted things the
  pattern tables do not recognize, such as a note's title line or "Proposed
  procedure: ...". Those are shown to the reviewer and change nothing, which is
  the designed behavior for model-only spans on a set the rules already cover.
- **What "exact bytes 50%" means.** On ten fresh no-cache replays of one case,
  the decision, issues, checks' statuses, and assumptions were identical every
  time. The only varying bytes were the model's quote boundaries in the R4
  evidence: "ADDENDUM 08:30: Re-measured 137/78 mmHg" one run,
  "08:30: Re-measured 137/78 mmHg" the next, "Re-measured 137/78 mmHg" the
  third. Same reading, same parsed value, same timestamp. With the cache on,
  repeat runs are byte-identical. If evidence quotes must also be stable across
  cold runs, the fix is to normalize a grounded span to its containing line
  before it becomes evidence, which is a one-line change in the fact layer.

On these two sets the model adds no correct decisions the rules did not
already reach, which is expected: the tables were grown against them. Its
value shows up on inputs neither author anticipated, where the rules fail
closed and the model can still locate the span for code to verify.

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
- A consent signed by a legal guardian or healthcare proxy on behalf of the
  patient counts as signed. Whether the signer had authority is the surgical
  team's call, so the output records who signed as an assumption.
- A medication mention counts as "taking" only when anchored to a reporting
  verb or an explicit status phrase. A bare "takes X" is not a mention, because
  it would also match "no longer takes X".
- A plan needs both a hold and a restart with timing, in the same document,
  naming the drug the patient actually takes. Deferral to another team is not a
  plan.
- Wellness and establish-care notes titled "H&P" do not satisfy the H&P rule.
- Missing temperature or blood pressure is missing data, never an exclusion.
- An unverifiable vital (bad timestamp, unit conflict) routes to
  `MISSING_REQUIRED_DATA` so a human looks, rather than to an exclusion the
  text does not support.
- A structured and a free-text reading with the identical timestamp resolve in
  favor of the structured value, recorded as an assumption. Two free-text
  readings with the identical timestamp resolve to the one later in the
  document, recorded as an assumption when their values differ.
- A plan that continues the anticoagulant satisfies the rule when the sentence
  says so perioperatively ("through surgery", "without interruption",
  "before or after"), because an uninterrupted drug is managed before and after.
  A bare "continue apixaban" is vague and stays an incomplete plan. The output
  records the continue instruction as an assumption so the nurse sees it.
- A hold or restart instruction counts as specific only when its timing sits in
  the same sentence. A period ends a sentence except between two digits, so a
  decimal dose ("2.5 mg") stays inside the sentence.
- Consent laterality is compared when both the scheduled procedure and the
  consent state a side; a mismatch is `WRONG_PROCEDURE`.
- A bare "92 F" with no label, degree sign, or decimal is not a temperature.
- Malformed input (wrong types, unknown risk strings, non-finite numbers) is
  triaged rather than rejected: each bad field is flagged and the result fails
  closed.

## Review findings that changed the code

A fresh-context review of the branch reproduced four fail-open paths that
ended in `READY`: a silent model-extractor failure masking a 192/114 blood
pressure, blank or negated consent signatures matching the "signed" pattern,
a vague restart instruction rescued by an unrelated "48 hours" later on the
same line, and "92 F" in an age/sex token masking a 101.8 F fever. Each has a
regression test in `tests/test_review_fixes.py`, alongside fixes for recency
inside a document, rounding consistency between structured and text
temperatures, consent laterality, and an invariant gate strong enough to catch
those regressions on its own.

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
- **Known limit: the lab-code map.** It covers common aliases, CPT codes, and
  display-name patterns, and nothing more; an unmapped code fails closed as a
  missing test rather than being guessed, because enumerating every lab
  system's codes in regex is the wrong direction and resolving an unfamiliar
  code from its display text is exactly the kind of reading the model extractor
  is for.
- **Known limit: the procedure-noun table.** Consent matching knows a fixed
  noun list plus a shared-content-word fallback, so a consent whose wording
  differs from the schedule in a way neither covers is `UNVERIFIED` rather than
  matched, because patching word orders one regex at a time does not generalize
  and judging that two phrasings name the same operation belongs to the model
  extractor, with code still verifying the quote it points at.
- **Next:** run the `union` extractor on a larger labeled set and mine the
  `rejections` and `review_flags` to grow the pattern tables from real
  phrasings; add golden extraction fixtures so the model path is tested
  offline; version the policy config per effective date; surface
  `checks[]` and `assumptions[]` in the TUI.

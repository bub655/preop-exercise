# Pre-Op Scheduling Triage: Approach and Design Decisions

## The one-sentence version

The model never produces a value that a decision depends on. It only points at
text. Code confirms the text exists, parses it, classifies it, applies the
policy, and checks its own output before anything leaves the pipeline. Whatever
code cannot verify fails closed to `NEEDS_FOLLOW_UP` with a review flag.

## Why not improve the single-call baseline?

The starter sends the whole package to a model and asks for a decision. Three
reasons that is the wrong shape here:

1. **The deciding facts are arithmetic and recency.** Thirty days passes and
   thirty-one fails. A diastolic of 110 excludes. 37.9 C is fine and 38.6 C is a
   fever. An 08:30 addendum overrides 08:06. Models are unreliable at exactly
   these things.
2. **A paraphrased quote is not evidence.** The nurse has to find the sentence.
   A reworded or invented quote erodes trust in every other packet.
3. **Determinism.** "Usually stable" is not a property a policy can rely on.

## Architecture

```
submission JSON
   v
[1] ingest        structured fields -> Facts                (pure Python)
[2] extract       documents -> Spans                        (rules, or Claude, cached)
[3] ground        Spans -> grounded Spans + Rejections      (verbatim match in the document)
[4] parse/classify grounded Spans -> Facts | ReviewFlags    (regex parsers, pattern tables)
[5] engine        Facts -> decision, issues, checks         (policy config, recency, precedence)
[6] invariants    output -> output | SYSTEM_ERROR           (assertions on the result itself)
```

A `Span` is untrusted: a kind, a document id, a quote. A `Fact` is trusted: a
value code derived from a structured field or a grounded quote, with source,
timestamp, and parser attached. Only Facts reach the engine.

### The five gates

1. **Grounding.** The quote must appear verbatim in the document; whitespace is
   the only tolerance. Quotes over 400 characters are rejected. A rejected span
   is recorded, never repaired.
2. **Parsing.** Values come only from the grounded quote through deterministic
   parsers and a closed drug table. Celsius conversion is exact rational
   arithmetic, so 38.0 C is exactly 100.4 F. A parse failure is a flag, not a
   guess.
3. **Classification.** For semantic kinds, code labels the quote against pattern
   tables in `config/policy.yaml` and compares with any model-suggested label.
   Disagreement or no match is a flag.
4. **Consistency.** Structured and free-text vitals merge into one timeline and
   the latest reading on or before review time wins. A drug reported in a note
   but absent from the list is active, recorded as an assumption. Mentions
   resolve by recency.
5. **Output invariants.** No `READY` with issues, blocking flags, or unverified
   or skipped checks; `NOT_CLEARED` only when backed by a grounded reading over
   the threshold; every evidence quote re-grounds; every category matches its
   rule; the output serializes identically twice. A violation demotes to
   `NEEDS_FOLLOW_UP` with `SYSTEM_ERROR`. Nothing past extraction raises.

### Where the model is left

One job: for each document, emit candidate spans with a kind and a verbatim
quote, through a strict JSON schema. If it misses a span the engine fails
closed. If it invents one, grounding removes it. If it mislabels one,
classification catches it.

Three extractors share everything downstream, chosen with `--extractor`:
`rules` (regex over the same pattern tables, offline, the recall floor),
`claude` (`claude-opus-5-5`, effort medium, cached by document hash, prompt
version, and model; any API failure becomes a blocking `EXTRACTOR_ERROR` so an
unread document can never produce `READY`), and `union` (both, each span tagged
by origin).

For semantic kinds the rules already find every phrasing the tables recognize,
so a model span the tables do not recognize carries no verified information.
Those become **advisory** review flags: shown to the reviewer, never a scored
issue. A model label that contradicts the code's label is blocking, because
that is a real conflict.

### Policy as data

Windows, thresholds, required tests by risk, the lab-code map, the drug tables,
the procedure-noun table, and every phrase pattern live in `config/policy.yaml`.
The engine contains no clinical numbers.

## Generalization: a held-out synthetic set

The pattern tables were first built against the 50 sample cases, so a score on
those cases says nothing about generalization. The repo includes a synthetic
generator (`preop_triage/synth.py`, `make synth`) whose phrasing banks were
written from general clinical documentation rather than copied from the sample:
H&P title variants, signed and unsigned consent phrasings, hold, restart, and
continue phrasings, ten blood-pressure layouts, Celsius and unit-less
temperatures, structured `value_c` vitals and type aliases, CPT and display-name
lab codes, unlisted procedures with laterality, and unknown risk strings.
Ground truth comes from the scenario parameters, never from the engine.

Errors are split into **fail-open** (something cleared that should not have
been; must be zero) and **fail-closed** (a follow-up that was not needed; the
recall cost).

| Run | Cases | Fully correct | Fail-open | Fail-closed |
|-----|------:|--------------:|----------:|------------:|
| Tables tuned on the sample only | 400 | 24% | 30% | 46% |
| After broadening, 5 seeds | 2000 | 100% | 0% | 0% |

The generator and the tables have the same author, so 100% is a lower bound.
What matters is that an unanticipated phrasing fails closed and becomes a test.

## Results

| Extractor | Set | Decision match | Category set match | Determinism |
|-----------|-----|---------------:|-------------------:|-------------|
| rules  | 50 sample cases | 50/50 | 50/50 | 10 replays byte-identical |
| rules  | synthetic held-out, 5 seeds | 2000/2000 | 2000/2000 | n/a |
| rules  | 10 hand-written held-out | 10/10 | 10/10 | n/a |
| claude | 50 sample cases | 50/50 | 50/50 | 10 cold replays: decision-bearing 10/10, full bytes 4/10 |
| claude | 10 hand-written held-out | 10/10 | 10/10 | n/a |
| union  | 50 sample cases | 50/50 | 50/50 | byte-identical from cache |
| union  | 10 hand-written held-out | 10/10 | 10/10 | byte-identical from cache |

**Two determinism numbers.** The harness reports both. Decision-bearing match
covers decision, issues, checks with evidence, assumptions, rejections, and
blocking flags. Full-bytes match covers everything. They differ only because
advisory flags vary between cold runs: sometimes the model also quotes a
heading such as "PRE-OP NURSING ASSESSMENT" and that non-blocking flag appears
or does not. The advisory flags stay in the output so the nurse sees what the
model pointed at. Blood pressure and temperature evidence is normalized to its
containing line, so the model's quote boundaries no longer affect the
decision-bearing output.

**What the model run showed.** About 290 documents, one call each, roughly 3
seconds and 1,500 input tokens per call. Zero rejected spans: the model never
returned a quote that failed to ground. 115 advisory flags and no blocking
ones. Union changed no decision versus rules on either set, which is expected
because the tables were grown against them. The model's value is on inputs
neither author anticipated, where rules fail closed and the model can still
locate the span for code to verify.

## The evidence packet

Each issue carries `category`, `rule_id`, a `status` such as `OUTDATED` or
`STALE_PLAN`, a one-sentence summary, `evidence[]` with a JSON path or document
id and the exact quote, a `computed` block with the dates, day counts, window,
threshold, and unit conversion, and `action_needed`. Each result also carries
`checks[]` for every rule so READY cases are auditable, `assumptions[]` for
every judgment and the text it rests on, `review_flags[]`, `rejections[]`, and
`provenance`.

## Stated assumptions

Four of these were decisions made on review of the held-out cases.

- **Inclusive windows.** Thirty days passes; thirty-one fails. Fourteen passes
  for high risk; fifteen fails.
- **Continue plans.** A plan that continues the anticoagulant satisfies Rule 3
  when the sentence says so perioperatively ("through surgery", "without
  interruption", "before or after"), because an uninterrupted drug is managed
  before and after. A bare "continue apixaban" is vague and stays an incomplete
  plan. The instruction is recorded as an assumption.
- **Proxy signatures.** A consent signed by a legal guardian or healthcare proxy
  on behalf of the patient counts as signed. Whether the signer had authority is
  the surgical team's call, so who signed is recorded as an assumption.
- **Unit-less temperatures.** A labeled reading with no unit ("Temp 38.7") is
  read from its value range, 30 to 45 as Celsius and 85 to 115 as Fahrenheit,
  and the inference is recorded. A bare "92 F" with no label, degree sign, or
  decimal is not a temperature.

The rest:

- Review time is `submission_received_at`; later readings are ignored and
  recorded. "See flowsheet" and "vitals deferred" are not readings.
- A patient-reported anticoagulant counts as active. An unknown status is
  `MISSING_REQUIRED_DATA`, and the anticoagulation rule is skipped.
- A medication mention counts as "taking" only when anchored to a reporting
  verb or a status phrase; a bare "takes X" would also match "no longer takes X".
- A hold or restart is specific only when its timing is in the same sentence. A
  period ends a sentence except between two digits, so "2.5 mg" stays inside.
- Any signed, matching consent satisfies the rule regardless of earlier unsigned
  forms. Laterality is compared when both sides state a side.
- Wellness and establish-care notes titled "H&P" do not satisfy the H&P rule.
- Missing or unverifiable vitals are `MISSING_REQUIRED_DATA`, never an exclusion.
- Same-timestamp ties go to the structured reading, else to the reading later
  in the document, recorded when values differ.
- Malformed input is triaged, not rejected: each bad field is flagged and the
  result fails closed.

## What I changed after reviewing the generated code

Three sources found problems after the first complete build.

**A fresh-context code review** reproduced four fail-open paths that ended in
`READY`: a silent model-extractor failure masking a 192/114 blood pressure,
blank or negated consent signatures matching the "signed" pattern, a vague
restart rescued by an unrelated "48 hours" later on the line, and "92 F" in an
age/sex token masking a 101.8 F fever. All four are fixed with regression tests
in `tests/test_review_fixes.py`.

**The synthetic set** found two more fail-open bugs the sample could not:
structured vitals typed `bp` or `temp`, or carrying `value_c`, were dropped
silently, so an exclusion-level reading could vanish; and the invariant gate
treated flag evidence as a JSON path and did not recognize structured Celsius
exclusions, so it demoted correct `NOT_CLEARED` results. Both are fixed, and an
unrecognized vital type is now a flag.

**The hand-written held-out set** found no fail-open. It found three
fail-closed misses, each a judgment the policy text left open, and each became
one of the decisions above: a continue-through-surgery plan, a guardian-signed
consent, and a decimal dose read as a sentence boundary. Fixing the first two
meant adding what the policy allows; fixing the third was one helper in the
pattern compiler.

## What the starter did that this fixes

New client per call, no retries, sorted-key JSON that scrambles document order,
a non-strict schema, no caching, evidence as free text, and an eval script that
round-trips through a hosted evals API. All replaced.

## Trade-offs and what I would do next

- **Recall depends on the pattern tables.** A phrasing the tables do not cover
  becomes a review flag instead of a fact. That is the right trade here, and
  the Claude extractor exists to find spans the rules miss.
- **Known limit, not patched: the lab-code map.** It covers common aliases, CPT
  codes, and display-name patterns; an unmapped code fails closed as a missing
  test, because enumerating every lab system's codes in regex is the wrong
  direction and reading an unfamiliar code from its display text is what the
  model extractor is for.
- **Known limit, not patched: the procedure-noun table.** A consent whose
  wording differs from the schedule in a way the noun list and the content-word
  fallback do not cover is `UNVERIFIED`, because patching word orders one regex
  at a time does not generalize and judging that two phrasings name one
  operation belongs to the model, with code verifying the quote.
- **Known limit, not patched: two spellings on the sample.** "Proposed
  procedure:" and "HISTORY & PHYSICAL - PRE-OP" account for 50 of the 115
  advisory flags; the model located them correctly and classification rejected
  them because the tables list neither spelling.
- **Next:** run `union` on a larger labeled set and mine `review_flags` to
  grow the tables from real phrasings; add golden extraction fixtures; surface
  `checks[]` and `assumptions[]` in the TUI.

# Pre-op triage: write-up

## How to run

Everything runs offline by default. `uv` is the only prerequisite.

```
make test                     # 332 tests
make baseline evals score     # 50 sample cases
make baseline evals INPUT=data/heldout_cases.jsonl OUTPUT=data/heldout_outputs.jsonl REPORT=data/heldout_report.json
make synth                    # 500 generated cases, reports fail-open / fail-closed
make determinism              # replays one case 10x
```

To use the model path, set `ANTHROPIC_API_KEY` and add `EXTRACTOR=claude` (or
`union`) to the baseline/determinism targets. Model calls are cached under
`data/cache/`, so a second run is free. The `triage_submission` entry point in
`core.py` keeps the starter's signature.

## What I built

The baseline sends the whole package to a model and asks for a decision. I
didn't want to improve that, I wanted to get rid of it. Almost everything that
decides a case here is arithmetic or recency: is the H&P 30 or 31 days old, is
the diastolic 109 or 110, is the 08:30 recheck later than the 08:06 reading,
does 37.9 C convert to something over 100.4 F. A language model is the wrong
tool for that, and a nurse who has to verify every non-READY case can't do
anything with a paraphrased "evidence" string.

So the model has exactly one job: point at text. For each document it returns
spans, each a kind from a fixed list and a verbatim quote. Everything after
that is code:

- Grounding checks the quote actually exists in the document. If not, it's
  dropped and logged. The model can't invent evidence.
- Parsers turn the quote into a value (BP, temperature with unit handling,
  dates, times). Temperature conversion uses exact rational arithmetic so 38.0 C
  is 100.4 F and not 100.3999.
- A classifier labels semantic quotes (signed vs unsigned, specific hold vs
  vague) against pattern tables in `config/policy.yaml`. If the model also
  suggested a label and it disagrees with the code's label, that's a flag.
- The engine applies the four rules from the policy appendix, with every
  number coming from the config file.
- An invariant gate checks the output itself before it leaves: no READY with
  issues, NOT_CLEARED only when backed by a reading that actually crosses the
  threshold, every evidence quote re-grounds. If anything fails, the case is
  demoted to NEEDS_FOLLOW_UP with a SYSTEM_ERROR flag.

Anything the code can't verify fails closed. That's the whole design.

There's also a regex extractor that uses the same pattern tables, so the full
pipeline runs with no API key. It turned out to be enough for the sample data.
The model extractor exists for phrasings the regexes don't know; a span it
finds still has to pass every gate above.

## Results

| extractor | set | decision | categories |
|---|---|---|---|
| rules | 50 sample cases | 50/50 | 50/50 |
| rules | 2000 generated cases (5 seeds) | 2000/2000 | 2000/2000 |
| rules | 10 hand-written held-out cases | 10/10 | 10/10 |
| claude | 50 sample cases | 50/50 | 50/50 |
| claude | 10 held-out cases | 10/10 | 10/10 |

Determinism: the rules path is byte-identical across 10 replays. The model
path, run cold with no cache 10 times on one case, gave identical decisions,
issues, checks and assumptions every time, and identical full output 4 times
out of 10. The difference is advisory flags, where the model sometimes also
quotes a note heading the tables don't recognize. Those flags never affect the
decision and I left them in so the reviewer can see what the model pointed at.
The harness reports both numbers.

The model never produced a quote that failed to ground, across about 290
documents. It also didn't change a single decision versus the regex extractor
on these sets, which is what I'd expect since the pattern tables were grown on
them.

## Overfitting

The 100% on the sample is not a useful number by itself. The pattern tables
were built against those 50 cases. So I wrote a generator (`preop_triage/synth.py`)
with phrasing banks that deliberately don't copy the sample: different H&P
titles, different consent wording, BP as `SBP 182, DBP 96`, Celsius and unit-less
temps, structured vitals typed `bp`/`temp` with `value_c`, CPT lab codes,
procedures not in my noun list, and so on. Ground truth comes from how each case
was generated, not from the engine.

The first run was bad: 24% fully correct, 30% fail-open. Two of the fail-open
causes were real bugs the sample could never have shown: structured vitals with
a type other than `blood_pressure`/`temperature` were silently dropped, and the
invariant gate mis-resolved one evidence path. After fixing those and widening
the tables (cue plus negation instead of enumerated sentences), five seeds come
back clean. I wrote both the generator and the tables, so that's a lower bound,
not proof. The point is that an unanticipated phrasing fails closed and becomes
a test.

## Decisions I made

The policy leaves some things open. These came up in the held-out cases and I
decided them rather than leaving them to the pattern tables:

- Windows are inclusive. 30 days passes, 31 fails.
- A plan that says to continue the anticoagulant through surgery without
  interruption satisfies Rule 3. The policy asks how the drug is managed before
  and after, and "not interrupted" answers both. A bare "continue apixaban" is
  vague and stays an incomplete plan. The instruction is recorded as an
  assumption in the output.
- A consent signed by a legal guardian or healthcare proxy on behalf of the
  patient counts as signed. Whether they had authority is the surgical team's
  call, not the triage system's. Who signed is recorded as an assumption.
- A labeled temperature with no unit ("Temp 38.7") is read from its range and
  the inference is recorded. A bare "92 F" with no label is not a temperature;
  it's more likely an age.

Every assumption the engine makes shows up in the output under `assumptions`,
with the quote it rests on.

## Things I left alone

The lab-code map and the procedure-noun table are both finite lists. An
unknown lab code fails closed as a missing test; a consent whose wording the
noun list and word-overlap fallback don't cover comes back UNVERIFIED. I could
keep adding regexes for every code and every word order but that's the wrong
direction. Reading an unfamiliar lab display name or deciding that two phrasings
name the same operation is what the model extractor is for, with code still
verifying the quote. Two spellings in the sample itself fall in this bucket:
"Proposed procedure:" and "HISTORY & PHYSICAL - PRE-OP". The model located both
and classification rejected them because the tables list neither.

## What I'd do next

Run the union extractor on a bigger labeled set and mine the advisory flags for
real phrasings to add. Add golden extraction fixtures so the model path is
tested offline. Show the per-rule checks and assumptions in the TUI, since
that's what a nurse would actually read.

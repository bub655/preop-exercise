INPUT ?= data/patients_sample_50.jsonl
OUTPUT ?= data/baseline_outputs.jsonl
REPORT ?= data/eval_report.json
DETERMINISM_REPORT ?= data/determinism_report.json
MODEL ?= claude-opus-5-5
EXTRACTOR ?= rules
CACHE_FLAG ?=

.PHONY: baseline evals determinism score report test synth all clean

baseline:
	uv run run_baseline.py \
		--input $(INPUT) \
		--output $(OUTPUT) \
		--model $(MODEL) \
		--extractor $(EXTRACTOR) $(CACHE_FLAG)

evals:
	uv run run_evals.py \
		--input $(INPUT) \
		--outputs $(OUTPUT) \
		--report $(REPORT)

determinism:
	uv run run_evals.py \
		--determinism \
		--input $(INPUT) \
		--model $(MODEL) \
		--extractor $(EXTRACTOR) $(CACHE_FLAG) \
		--report $(DETERMINISM_REPORT)

score:
	@python3 -c 'import json; r=json.load(open("$(REPORT)")); s=(r.get("primary_score",{}) or {}).get("value_pct"); print(s if s is not None else r.get("local_metrics_summary",{}).get("aggregate_local_score_pct", 0.0))'

report:
	uv run view_report.py --report $(REPORT)

test:
	uv run pytest -q

SYNTH_N ?= 500
SYNTH_SEED ?= 1
synth:
	uv run run_synth.py --n $(SYNTH_N) --seed $(SYNTH_SEED)

all: baseline evals determinism score

clean:
	rm -f data/baseline_outputs.jsonl \
		data/eval_report.json \
		data/determinism_report.json

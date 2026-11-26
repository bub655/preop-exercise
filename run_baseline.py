"""Run the triage pipeline over a JSONL of submission packages and write outputs."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import PatientSubmission, triage_submission
from preop_triage.policy import load_policy

ROOT = Path(__file__).resolve().parent

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass
class BaselineInputCase:
    case_id: str
    submission: PatientSubmission


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "data" / "patients_sample_50.jsonl"), help="Input patient JSONL file")
    parser.add_argument("--output", default=str(ROOT / "data" / "baseline_outputs.jsonl"), help="Output JSONL file with triage results")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model id for the claude/union extractors")
    parser.add_argument("--extractor", default="rules", choices=["rules", "claude", "union"], help="rules: offline regex; claude: model locates spans; union: both")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the on-disk extraction cache")
    parser.add_argument("--policy", default=None, help="Path to an alternative policy.yaml")
    parser.add_argument("--max-records", type=int, default=0, help="Maximum number of records to process (0 means all)")
    return parser.parse_args()


def load_cases(path: Path) -> list[BaselineInputCase]:
    cases: list[BaselineInputCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict) and "submission" in payload and "case_id" in payload:
                cases.append(BaselineInputCase(str(payload["case_id"]), PatientSubmission.model_validate(payload["submission"])))
            else:
                cases.append(BaselineInputCase(f"case_{idx:05d}", PatientSubmission.model_validate(payload)))
    return cases


def main() -> None:
    args = parse_args()
    input_path, output_path = Path(args.input), Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    policy = load_policy(Path(args.policy)) if args.policy else None

    cases = load_cases(input_path)
    if args.max_records > 0:
        cases = cases[: args.max_records]

    with output_path.open("w", encoding="utf-8") as handle:
        for idx, case in enumerate(cases):
            print(f"[{idx + 1}/{len(cases)}] {case.case_id} extractor={args.extractor}")
            submission = case.submission.model_dump()
            row: dict[str, Any] = {"record_index": idx, "case_id": case.case_id, "submission": submission, "model": args.model, "extractor": args.extractor, "output": None, "error": None, "seconds": None}
            started = time.perf_counter()
            try:
                output = triage_submission(submission, model=args.model, extractor=args.extractor, use_cache=not args.no_cache, policy=policy)
                row["output"] = output.model_dump(mode="json")
            except Exception as exc:  # should not happen: the pipeline fails closed internally
                row["error"] = str(exc)
            row["seconds"] = round(time.perf_counter() - started, 3)
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")

    print(f"Wrote outputs -> {output_path}")


if __name__ == "__main__":
    main()

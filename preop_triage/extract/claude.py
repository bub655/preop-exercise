"""Claude span extractor: the model's one job is to locate verbatim quotes.

It returns candidate spans with a kind from a closed enum and an optional
label suggestion. It never returns parsed values. Every span it emits still
has to survive grounding, parsing, classification, and the policy engine.
Any failure yields zero spans for the document and sets `last_error`; the
pipeline then fails closed for that document.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from ..classify import GROUP_FOR_KIND
from ..model import Span, SpanKind
from ..policy import Policy
from .cache import ExtractionCache

PROMPT_VERSION = "1"
MAX_TOKENS = 8192
EFFORT = "medium"

SPAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [k.value for k in SpanKind]},
                    "quote": {"type": "string"},
                    "label": {"type": ["string", "null"]},
                },
                "required": ["kind", "quote", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["spans"],
    "additionalProperties": False,
}

_KIND_HELP = {
    SpanKind.VITAL_BP: "a blood pressure reading, e.g. 'NIBP 189/112' or 'BP 157/117'. Include any HH:MM time that precedes it on the same line.",
    SpanKind.VITAL_TEMP: "a temperature reading with its unit, e.g. 'T 37.1 C' or 'Temperature 99.1 °F'.",
    SpanKind.VITALS_DEFERRED: "a statement that vitals were not recorded here, e.g. 'VITAL SIGNS: See flowsheet'.",
    SpanKind.DOC_ENCOUNTER_KIND: "a phrase showing what kind of visit this note documents, e.g. 'PREOPERATIVE HISTORY AND PHYSICAL' or 'annual wellness / establish care'.",
    SpanKind.PLANNED_PROCEDURE: "the planned-procedure line of an H&P, e.g. 'Planned procedure: inguinal hernia repair'.",
    SpanKind.CONSENT_PROCEDURE: "the 'Procedure:' line of a consent form.",
    SpanKind.CONSENT_SIGNATURE: "the statement about the PATIENT's signature on a consent (not the physician attestation).",
    SpanKind.MED_MENTION: "a short phrase stating the patient is taking, not taking, or unsure about an anticoagulant or antiplatelet, including the drug name.",
    SpanKind.ANTICOAG_HOLD: "an instruction to hold/stop an anticoagulant before surgery, including the drug name and timing.",
    SpanKind.ANTICOAG_RESUME: "an instruction to resume/restart an anticoagulant after surgery, including the drug name and timing.",
    SpanKind.ANTICOAG_DEFERRED: "a statement deferring anticoagulation management to another team or to later.",
    SpanKind.DOC_ABSENCE: "a statement that an H&P or consent is not on file / not yet obtained.",
    SpanKind.PROCEDURE_DATE_STATEMENT: "a statement of the scheduled procedure date or that it is pending.",
    SpanKind.PROCEDURE_RISK_STATEMENT: "a statement that the procedure risk category is not yet assigned.",
}


def build_system_prompt(policy: Policy) -> str:
    lines = [
        "You locate text in a clinical document for a pre-operative scheduling review.",
        "Return spans only. Every quote must be copied VERBATIM from the document: same characters, same spacing, no paraphrase, no corrections, at most 300 characters.",
        "Do not compute, convert, or interpret values. Do not add anything that is not in the document.",
        "If the document contains nothing relevant, return an empty list.",
        "",
        "Span kinds and, where applicable, the allowed label suggestions:",
    ]
    for kind, help_text in _KIND_HELP.items():
        group = GROUP_FOR_KIND.get(kind)
        labels = policy.labels(group) if group else []
        suffix = f" Labels: {', '.join(labels)}." if labels else " Label: null."
        lines.append(f"- {kind.value}: {help_text}{suffix}")
    return "\n".join(lines)


class ClaudeSpanExtractor:
    name = "claude"
    prompt_version = PROMPT_VERSION

    def __init__(self, *, model: str, policy: Policy, cache: ExtractionCache | None = None, client: Any | None = None) -> None:
        self.model = model
        self.policy = policy
        self.cache = cache
        self._client = client
        self.system_prompt = build_system_prompt(policy)
        self.last_error: str | None = None
        self.cache_hits = 0
        self.calls = 0

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def _request(self, title: str | None, text: str) -> str:
        user = f"Document title: {title or '(untitled)'}\n\n<document>\n{text}\n</document>"
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": SPAN_SCHEMA}, "effort": EFFORT},
            cache_control={"type": "ephemeral"},
        )
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"stop_reason={response.stop_reason}")
        return next(b.text for b in response.content if b.type == "text")

    def _to_spans(self, doc_id: str, raw: list[dict]) -> list[Span]:
        spans: list[Span] = []
        bad: list[str] = []
        for item in raw:
            kind = item.get("kind")
            try:
                skind = SpanKind(kind)
            except ValueError:
                bad.append(str(kind))
                continue
            quote = item.get("quote")
            if not isinstance(quote, str):
                bad.append("non-string quote")
                continue
            label = item.get("label")
            spans.append(Span(kind=skind, doc_id=doc_id, quote=quote, offset=None, model_label=label if isinstance(label, str) else None, origin=self.name))
        if bad:
            self.last_error = f"dropped spans with unknown kinds: {sorted(set(bad))}"
        return spans

    def extract(self, doc_id: str, title: str | None, text: str | None) -> list[Span]:
        self.last_error = None
        if not text:
            return []
        key = ExtractionCache.key(text, self.prompt_version, self.model)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return self._to_spans(doc_id, cached)
        try:
            self.calls += 1
            payload = json.loads(self._request(title, text))
            raw = payload.get("spans") if isinstance(payload, dict) else None
            if not isinstance(raw, list):
                raise ValueError("response missing 'spans' list")
        except anthropic.RateLimitError as exc:
            self.last_error = f"rate limited: {exc}"
            return []
        except anthropic.APIStatusError as exc:
            self.last_error = f"api status {exc.status_code}: {exc.message}"
            return []
        except anthropic.APIConnectionError as exc:
            self.last_error = f"connection error: {exc}"
            return []
        except (ValueError, RuntimeError, StopIteration) as exc:
            self.last_error = f"unusable response: {exc}"
            return []
        if self.cache is not None:
            self.cache.put(key, raw)
        return self._to_spans(doc_id, raw)

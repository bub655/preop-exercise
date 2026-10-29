from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anthropic

import pytest

from preop_triage.extract.base import ExtractorError
from preop_triage.extract.cache import ExtractionCache
from preop_triage.extract.claude import PROMPT_VERSION, SPAN_SCHEMA, ClaudeSpanExtractor
from preop_triage.extract.rules import RuleSpanExtractor
from preop_triage.extract.union import UnionSpanExtractor
from preop_triage.model import SpanKind
from preop_triage.policy import load_policy

POLICY = load_policy()
TEXT = "VITAL SIGNS:\n  08:06  NIBP 189/112, T 37.1 C.\nPatient signature: pending\n"


class FakeMessages:
    def __init__(self, payload: object, stop_reason: str = "end_turn", error: Exception | None = None) -> None:
        self.payload, self.stop_reason, self.error = payload, stop_reason, error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = json.dumps(self.payload) if not isinstance(self.payload, str) else self.payload
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)], usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


GOOD = {"spans": [{"kind": "VITAL_BP", "quote": "NIBP 189/112", "label": None}, {"kind": "CONSENT_SIGNATURE", "quote": "Patient signature: pending", "label": "UNSIGNED"}]}


def test_parses_spans_from_json_text() -> None:
    fake = FakeMessages(GOOD)
    ext = ClaudeSpanExtractor(model="claude-opus-5-5", policy=POLICY, cache=None, client=_client(fake))
    spans = ext.extract("d1", "Consent", TEXT)
    assert [(s.kind, s.quote, s.model_label, s.origin) for s in spans] == [
        (SpanKind.VITAL_BP, "NIBP 189/112", None, "claude"),
        (SpanKind.CONSENT_SIGNATURE, "Patient signature: pending", "UNSIGNED", "claude"),
    ]
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SPAN_SCHEMA}
    assert call["output_config"]["effort"] == "medium"
    assert "temperature" not in call
    assert TEXT in call["messages"][0]["content"]


def test_unknown_kind_dropped_and_recorded() -> None:
    fake = FakeMessages({"spans": [{"kind": "MAGIC", "quote": "x", "label": None}]})
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    assert ext.extract("d1", None, TEXT) == []
    assert ext.last_error is not None and "MAGIC" in ext.last_error


def test_cache_hit_skips_client(tmp_path: Path) -> None:
    fake = FakeMessages(GOOD)
    cache = ExtractionCache(tmp_path)
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=cache, client=_client(fake))
    first = ext.extract("d1", None, TEXT)
    second = ext.extract("d1", None, TEXT)
    assert first == second and len(fake.calls) == 1 and ext.cache_hits == 1
    assert cache.get(cache.key(TEXT, PROMPT_VERSION, "m")) is not None


def test_cache_key_changes_with_prompt_version_and_model(tmp_path: Path) -> None:
    cache = ExtractionCache(tmp_path)
    assert cache.key(TEXT, "1", "m") != cache.key(TEXT, "2", "m") != cache.key(TEXT, "1", "n")


def test_api_error_raises_extractor_error_and_records() -> None:
    err = anthropic.APIConnectionError(request=SimpleNamespace())
    fake = FakeMessages(GOOD, error=err)
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    with pytest.raises(ExtractorError):
        ext.extract("d1", None, TEXT)
    assert ext.last_error is not None


def test_refusal_stop_reason_raises() -> None:
    fake = FakeMessages(GOOD, stop_reason="refusal")
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    with pytest.raises(ExtractorError):
        ext.extract("d1", None, TEXT)
    assert "refusal" in (ext.last_error or "")


def test_invalid_json_raises() -> None:
    fake = FakeMessages("not json")
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    with pytest.raises(ExtractorError):
        ext.extract("d1", None, TEXT)
    assert ext.last_error


def test_empty_text_makes_no_call() -> None:
    fake = FakeMessages(GOOD)
    ext = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    assert ext.extract("d1", None, "") == [] and fake.calls == []


def test_union_preserves_origin() -> None:
    fake = FakeMessages(GOOD)
    claude = ClaudeSpanExtractor(model="m", policy=POLICY, cache=None, client=_client(fake))
    union = UnionSpanExtractor(RuleSpanExtractor(POLICY), claude)
    spans = union.extract("d1", None, TEXT)
    origins = {s.origin for s in spans}
    assert origins == {"rules", "claude"} and union.name == "union"
    assert union.model == "m" and union.prompt_version == PROMPT_VERSION

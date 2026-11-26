"""Structured ingestion: typed JSON fields become Facts with no model involvement.

Nulls stay None. Nothing is defaulted; the engine reports missing values.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .model import Fact, ReviewFlag
from .parsers import c_to_f, q1
from .policy import Policy


def parse_iso(value: str | None) -> datetime | None:
    """Parse ISO-8601 date or datetime into a naive UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        offset = dt.utcoffset()
        dt = dt.replace(tzinfo=None)
        if offset is not None:
            dt = dt - offset
    return dt


def _dicts(value: Any, source: str, flags: list[ReviewFlag], rule: str | None) -> list[dict[str, Any]]:
    """A list of dicts, or an empty list plus a flag when the field is malformed."""
    if value is None:
        return []
    if not isinstance(value, list):
        flags.append(ReviewFlag("MALFORMED_FIELD", None, source, None, None, rule))
        return []
    good = [v for v in value if isinstance(v, dict)]
    if len(good) != len(value):
        flags.append(ReviewFlag("MALFORMED_FIELD", None, source, None, None, rule))
    return good


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        d = Decimal(str(raw))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def structured_temp_f(v: dict[str, Any]) -> tuple[Decimal, str | None] | None:
    """Fahrenheit value from value_f, value_c, or value+unit. None when absent or non-finite."""
    if v.get("value_f") is not None:
        d = _decimal(v.get("value_f"))
        return (q1(d), None) if d is not None else None
    if v.get("value_c") is not None:
        d = _decimal(v.get("value_c"))
        return (c_to_f(d), f"{d} C") if d is not None else None
    if v.get("value") is not None:
        d = _decimal(v.get("value"))
        if d is None:
            return None
        unit = str(v.get("unit") or "").strip().lower()
        if unit.startswith("c"):
            return c_to_f(d), f"{d} C"
        if unit.startswith("f"):
            return q1(d), None
        if 30 <= d <= 45:
            return c_to_f(d), f"{d} C (unit inferred)"
        if 85 <= d <= 115:
            return q1(d), None
    return None


def _ingest_vitals(vitals: list[dict[str, Any]], policy: Policy, facts: list[Fact], flags: list[ReviewFlag]) -> None:
    for i, v in enumerate(vitals or []):
        source = f"vitals[{i}]"
        kind = policy.vital_kind(v.get("type"))
        if kind == "IGNORE":
            continue
        if kind is None:
            flags.append(ReviewFlag("UNKNOWN_VITAL_TYPE", None, f"{source}: type={v.get('type')!r}", None, None, "R4"))
            continue
        when = parse_iso(v.get("date"))
        if when is None:
            flags.append(ReviewFlag("STRUCTURED_DATE_UNPARSEABLE", None, str(v.get("date")), None, None, "R4"))
            continue
        if kind == "BP":
            s, d = _int_or_none(v.get("systolic")), _int_or_none(v.get("diastolic"))
            if s is None or d is None:
                flags.append(ReviewFlag("STRUCTURED_VITAL_INCOMPLETE", None, source, None, None, "R4"))
                continue
            facts.append(Fact("BP", (s, d), when, source))
        else:
            parsed = structured_temp_f(v)
            if parsed is None:
                flags.append(ReviewFlag("STRUCTURED_VITAL_INCOMPLETE", None, source, None, None, "R4"))
                continue
            temp_f, converted = parsed
            facts.append(Fact("TEMP", temp_f, when, source, meta={"converted_from": converted} if converted else {}))


def _ingest_labs(labs: list[dict[str, Any]], policy: Policy, facts: list[Fact], flags: list[ReviewFlag]) -> None:
    for i, lab in enumerate(labs or []):
        source = f"labs[{i}]"
        when = parse_iso(lab.get("effective_at"))
        code = policy.lab_codes.normalize(lab.get("code"), lab.get("display"))
        if when is None or code is None:
            flags.append(ReviewFlag("STRUCTURED_LAB_INCOMPLETE", None, source, None, None, "R2"))
            continue
        facts.append(Fact("LAB", {"code": code, "status": (lab.get("status") or "").lower(), "display": lab.get("display")}, when, source))


def _ingest_meds(meds: list[dict[str, Any]], policy: Policy, facts: list[Fact]) -> None:
    for i, m in enumerate(meds or []):
        name = m.get("name") or ""
        canonical, cls = policy.drugs.classify(name)
        active = m.get("active")
        active = active if isinstance(active, bool) else None
        facts.append(Fact("MED", {"name": name, "canonical": canonical, "class": cls, "active": active}, None, f"medications[{i}]"))


def _ingest_docs(docs: list[dict[str, Any]], facts: list[Fact], flags: list[ReviewFlag]) -> None:
    for i, d in enumerate(docs or []):
        source = f"documents[{i}]"
        doc_id = d.get("doc_id") or source
        when = parse_iso(d.get("date"))
        if when is None:
            flags.append(ReviewFlag("DOC_DATE_UNPARSEABLE", doc_id, str(d.get("date")), None, None, "R1"))
        facts.append(Fact("DOC", {"type": d.get("type"), "date": d.get("date"), "author": d.get("author"), "index": i}, when, source, doc_id=doc_id))


def ingest(submission: dict[str, Any], policy: Policy) -> tuple[list[Fact], list[ReviewFlag]]:
    facts: list[Fact] = []
    flags: list[ReviewFlag] = []
    proc = submission.get("procedure")
    if not isinstance(proc, dict):
        if proc is not None:
            flags.append(ReviewFlag("MALFORMED_FIELD", None, "procedure", None, None, "R0"))
        proc = {}
    raw_date = proc.get("procedure_date")
    pdate = parse_iso(raw_date) if isinstance(raw_date, str) else None
    if raw_date not in (None, "") and pdate is None:
        flags.append(ReviewFlag("STRUCTURED_DATE_UNPARSEABLE", None, str(raw_date), None, None, "R0"))
    facts.append(Fact("PROCEDURE_DATE", pdate.date() if pdate else None, None, "procedure.procedure_date"))
    risk = proc.get("procedure_risk")
    if isinstance(risk, str) and risk.strip():
        risk_value: str | None = risk.strip().upper()
    else:
        if risk not in (None, ""):
            flags.append(ReviewFlag("MALFORMED_FIELD", None, "procedure.procedure_risk", None, None, "R0"))
        risk_value = None
    facts.append(Fact("PROCEDURE_RISK", risk_value, None, "procedure.procedure_risk", meta={"raw": risk if isinstance(risk, str) else None}))
    ptype = proc.get("procedure_type")
    facts.append(Fact("PROCEDURE_TYPE", ptype if isinstance(ptype, str) and ptype else None, None, "procedure.procedure_type"))
    meta = submission.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    received = meta.get("submission_received_at")
    facts.append(Fact("REVIEW_TIME", parse_iso(received) if isinstance(received, str) else None, None, "metadata.submission_received_at"))
    _ingest_vitals(_dicts(submission.get("vitals"), "vitals", flags, "R4"), policy, facts, flags)
    _ingest_labs(_dicts(submission.get("labs"), "labs", flags, "R2"), policy, facts, flags)
    _ingest_meds(_dicts(submission.get("medications"), "medications", flags, "R3"), policy, facts)
    _ingest_docs(_dicts(submission.get("documents"), "documents", flags, "R1"), facts, flags)
    return facts, flags

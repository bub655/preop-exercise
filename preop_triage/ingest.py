"""Structured ingestion: typed JSON fields become Facts with no model involvement.

Nulls stay None. Nothing is defaulted; the engine reports missing values.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .model import Fact, ReviewFlag
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


def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _ingest_vitals(vitals: list[dict[str, Any]], facts: list[Fact], flags: list[ReviewFlag]) -> None:
    for i, v in enumerate(vitals or []):
        source = f"vitals[{i}]"
        vtype = (v.get("type") or "").lower()
        when = parse_iso(v.get("date"))
        if when is None:
            flags.append(ReviewFlag("STRUCTURED_DATE_UNPARSEABLE", None, str(v.get("date")), None, None, "R4"))
            continue
        if vtype == "blood_pressure":
            s, d = _int_or_none(v.get("systolic")), _int_or_none(v.get("diastolic"))
            if s is None or d is None:
                flags.append(ReviewFlag("STRUCTURED_VITAL_INCOMPLETE", None, source, None, None, "R4"))
                continue
            facts.append(Fact("BP", (s, d), when, source))
        elif vtype == "temperature":
            raw = v.get("value_f")
            if raw is None:
                flags.append(ReviewFlag("STRUCTURED_VITAL_INCOMPLETE", None, source, None, None, "R4"))
                continue
            try:
                temp = Decimal(str(raw)).quantize(Decimal("0.1"))
            except InvalidOperation:
                flags.append(ReviewFlag("STRUCTURED_VITAL_INCOMPLETE", None, source, None, None, "R4"))
                continue
            facts.append(Fact("TEMP", temp, when, source))
        # other vital types are not policy-relevant and are ignored


def _ingest_labs(labs: list[dict[str, Any]], policy: Policy, facts: list[Fact], flags: list[ReviewFlag]) -> None:
    for i, lab in enumerate(labs or []):
        source = f"labs[{i}]"
        when = parse_iso(lab.get("effective_at"))
        code = policy.lab_codes.normalize(lab.get("code"))
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
    proc = submission.get("procedure") or {}
    pdate = parse_iso(proc.get("procedure_date"))
    facts.append(Fact("PROCEDURE_DATE", pdate.date() if pdate else None, None, "procedure.procedure_date"))
    risk = proc.get("procedure_risk")
    facts.append(Fact("PROCEDURE_RISK", risk.upper() if isinstance(risk, str) and risk.strip() else None, None, "procedure.procedure_risk"))
    facts.append(Fact("PROCEDURE_TYPE", proc.get("procedure_type") or None, None, "procedure.procedure_type"))
    meta = submission.get("metadata") or {}
    facts.append(Fact("REVIEW_TIME", parse_iso(meta.get("submission_received_at")), None, "metadata.submission_received_at"))
    _ingest_vitals(submission.get("vitals") or [], facts, flags)
    _ingest_labs(submission.get("labs") or [], policy, facts, flags)
    _ingest_meds(submission.get("medications") or [], policy, facts)
    _ingest_docs(submission.get("documents") or [], facts, flags)
    return facts, flags

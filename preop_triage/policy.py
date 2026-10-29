"""Load and validate the policy config. The engine never hard-codes a number."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"


class Windows(BaseModel):
    hp_days: int
    lab_days: dict[str, int]


class Thresholds(BaseModel):
    sbp: int
    dbp: int
    temp_f: Decimal

    @field_validator("temp_f", mode="before")
    @classmethod
    def _as_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class DrugTable(BaseModel):
    """Closed table of drug names. classify() is the only way names get meaning."""

    anticoagulant: dict[str, list[str]]
    antiplatelet: dict[str, list[str]]

    def _index(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for cls_name in ("anticoagulant", "antiplatelet"):
            for canonical, names in getattr(self, cls_name).items():
                for name in names:
                    rows.append((name.lower(), canonical, cls_name))
        # longest names first so "aspirin" wins over "asa" when both appear
        rows.sort(key=lambda r: -len(r[0]))
        return rows

    def all_names(self) -> list[str]:
        return [name for name, _, _ in self._index()]

    def classify(self, text: str) -> tuple[str | None, str]:
        """Return (canonical_generic, class) for the first known drug in text."""
        lowered = text.lower()
        best: tuple[int, int, str, str] | None = None  # (position, -len(name), canonical, class)
        for name, canonical, cls_name in self._index():
            m = re.search(rf"\b{re.escape(name)}\b", lowered)
            if m is None:
                continue
            key = (m.start(), -len(name), canonical, cls_name)
            if best is None or key < best:
                best = key
        return (best[2], best[3]) if best else (None, "other")

    def drug_regex(self) -> str:
        names = sorted({n for n in self.all_names()}, key=len, reverse=True)
        return "(?:" + "|".join(re.escape(n) for n in names) + ")"


class LabCodes(BaseModel):
    table: dict[str, list[str]]

    def normalize(self, code: str | None) -> str | None:
        if code is None:
            return None
        upper = code.strip().upper()
        for canonical, aliases in self.table.items():
            if upper == canonical or upper in {a.upper() for a in aliases}:
                return canonical
        return upper


class Policy(BaseModel):
    version: str
    effective_date: str
    windows: Windows
    required_tests_by_risk: dict[str, list[str]]
    thresholds: Thresholds
    lab_codes: LabCodes
    lab_final_statuses: list[str]
    drugs: DrugTable
    procedure_nouns: list[str]
    patterns: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    def compiled(self, group: str, label: str) -> list[re.Pattern[str]]:
        """Compile a pattern list with {DRUG} expanded. Cached per instance."""
        cache = self.__dict__.setdefault("_compiled_cache", {})
        key = (group, label)
        if key not in cache:
            drug = self.drugs.drug_regex()
            cache[key] = [
                re.compile(p.replace("{DRUG}", drug), re.IGNORECASE | re.MULTILINE)
                for p in self.patterns.get(group, {}).get(label, [])
            ]
        return cache[key]

    def labels(self, group: str) -> list[str]:
        return list(self.patterns.get(group, {}).keys())


def load_policy(path: Path | None = None) -> Policy:
    raw = yaml.safe_load((path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    raw["lab_codes"] = {"table": raw["lab_codes"]}
    return Policy.model_validate(raw)

from __future__ import annotations

from preop_triage.policy import load_policy


def test_policy_loads_defaults() -> None:
    p = load_policy()
    assert p.windows.hp_days == 30
    assert p.windows.lab_days["HIGH"] == 14
    assert p.windows.lab_days["LOW"] == 30
    assert p.thresholds.sbp == 180
    assert p.thresholds.dbp == 110
    assert str(p.thresholds.temp_f) == "100.4"
    assert p.drugs.classify("Xarelto") == ("rivaroxaban", "anticoagulant")
    assert p.drugs.classify("ASA 81 mg PO daily") == ("aspirin", "antiplatelet")
    assert p.drugs.classify("warfarin (Coumadin) 5 mg PO daily") == ("warfarin", "anticoagulant")
    assert p.drugs.classify("lisinopril 20 mg PO daily") == (None, "other")
    assert p.lab_codes.normalize("LAB-CBC") == "CBC"
    assert p.lab_codes.normalize("BMP") == "BMP"
    assert p.required_tests_by_risk["HIGH"] == ["CBC", "CMP"]
    assert p.required_tests_by_risk["MODERATE"] == ["CBC"]


def test_drug_classify_returns_first_occurrence_not_longest_name() -> None:
    p = load_policy()
    assert p.drugs.classify("Resume warfarin at the usual home dose, with enoxaparin bridging restarted") == ("warfarin", "anticoagulant")
    assert p.drugs.classify("Bridge with enoxaparin; resume warfarin later") == ("enoxaparin", "anticoagulant")

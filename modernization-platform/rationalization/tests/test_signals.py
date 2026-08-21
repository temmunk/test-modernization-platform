"""The signal pack is the deterministic half of every portfolio decision, so the
maths behind it has to be right before an LLM is allowed to reason over it."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rationalization.signals import compute_signals, pair_key, signal_refs  # noqa: E402
from rationalization.tests import fixtures as fx  # noqa: E402


def test_data_variants_are_maximally_redundant():
    signals = compute_signals(fx.data_variant_pair(), fx.IR)
    (pair,) = signals["redundancy"]
    assert pair["pair"] == ["TIM-001", "TIM-002"]
    assert pair["step_intent_jaccard"] == 1.0
    assert pair["binding_jaccard"] == 1.0
    # assertion shapes match once the data row is masked out
    assert pair["assertion_shape_jaccard"] == 1.0
    assert pair["score"] == 1.0
    assert pair["identical_shape_different_data"] is True
    assert any("data rows differ" in d for d in pair["differing"])


def test_unrelated_tests_are_not_redundant():
    s = fx.data_variant_pair()
    s["tests"].append(fx.unrelated_test())
    signals = compute_signals(s, fx.IR)
    scores = {pair_key(*r["pair"]): r["score"] for r in signals["redundancy"]}
    assert scores["TIM-001~TIM-002"] == 1.0
    assert scores["TIM-001~TIM-003"] < 0.5
    assert scores["TIM-002~TIM-003"] < 0.5


def test_app_independent_assertions_are_flagged():
    s = fx.suite(fx.tautological_test(), fx.unrelated_test())
    signals = compute_signals(s, fx.IR)
    assert signals["tests"]["TIM-004"]["app_independent_assertions"] == ["A1"]
    # a test that reads the application has none
    assert signals["tests"]["TIM-003"]["app_independent_assertions"] == []


def test_oracle_and_unresolved_refs():
    broken = fx.test(
        "TIM-005", "broken oracle",
        [fx.step("S1", "x", "method:P.m")],
        [fx.assertion("A1", "y", "method:P.get", "data:drugs.NOPE.expectedName")],
    )
    signals = compute_signals(fx.suite(broken), fx.IR)
    sig = signals["tests"]["TIM-005"]
    assert sig["oracle_backed_assertions"] == 0
    assert sig["unresolved_expected_refs"] == ["A1"]


def test_cost_and_runtime_signals_come_from_execution_evidence():
    run = {"tests": [
        {"test_class": "DemoTests", "test_method": "generic", "status": "passed", "duration_s": 12.5},
        {"test_class": "DemoTests", "test_method": "brand", "status": "failed", "duration_s": 7.5},
    ]}
    signals = compute_signals(fx.data_variant_pair(), fx.IR, selenium_run=run)
    assert signals["tests"]["TIM-001"]["legacy_duration_s"] == 12.5
    assert signals["tests"]["TIM-002"]["legacy_status"] == "failed"
    assert signals["suite"]["total_legacy_runtime_s"] == 20.0


def test_feasibility_reports_unimplemented_bindings():
    gen = {
        "tim_coverage": [{"test_id": "TIM-001", "steps": [1], "assertions": [1, 2]}],
        "methods": [{"ir_ref": "method:SearchPage.addMedicine", "strategy": "todo-stub"}],
    }
    signals = compute_signals(fx.data_variant_pair(), fx.IR, gen_report=gen)
    feas = signals["tests"]["TIM-001"]["feasibility"]
    assert feas["unimplemented_bindings"] == ["method:SearchPage.addMedicine"]
    assert feas["fully_regenerable"] is False
    # a test with no dry-run coverage at all is not regenerable either
    assert signals["tests"]["TIM-002"]["feasibility"]["fully_regenerable"] is False


def test_signal_refs_are_the_citable_surface():
    signals = compute_signals(fx.data_variant_pair(), fx.IR)
    refs = signal_refs(signals)
    assert "tests.TIM-001.assertion_count" in refs
    assert "redundancy.TIM-001~TIM-002.score" in refs
    assert "suite.total_tests" in refs
    assert "tests.TIM-001.feasibility.source" in refs
    assert "redundancy.TIM-001~TIM-002.pair" not in refs

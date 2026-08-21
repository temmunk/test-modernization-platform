"""Every rejection path of the rationalization guardrails.

A disposition that removes, defers, or reshapes coverage has to survive these
checks; that is the difference between a portfolio decision and an opinion.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rationalization.signals import compute_signals  # noqa: E402
from rationalization.tests import fixtures as fx  # noqa: E402
from rationalization.validators import validate_plan  # noqa: E402


def _suite_and_signals():
    suite = fx.data_variant_pair()
    suite["tests"].append(fx.unrelated_test())
    return suite, compute_signals(suite, fx.IR)


def _decision(test_id, disposition, **kw):
    d = {
        "test_id": test_id,
        "disposition": disposition,
        "rationale": "because the signals say so",
        "confidence": 0.9,
        "evidence": [{"signal_ref": f"tests.{test_id}.assertion_count", "detail": "counted"}],
    }
    d.update(kw)
    return d


def _merge_plan():
    return {
        "suite_name": "demo",
        "portfolio_narrative": "narrative",
        "decisions": [
            _decision("TIM-001", "MERGE", group_id="g1", primary=True,
                      evidence=[{"signal_ref": "redundancy.TIM-001~TIM-002.score", "detail": "1.0"}]),
            _decision("TIM-002", "MERGE", group_id="g1", primary=False,
                      evidence=[{"signal_ref": "redundancy.TIM-001~TIM-002.score", "detail": "1.0"}]),
            _decision("TIM-003", "MIGRATE"),
        ],
        "merge_groups": [{"group_id": "g1", "primary_test_id": "TIM-001",
                          "member_test_ids": ["TIM-001", "TIM-002"], "rationale": "same test, other row"}],
    }


def _errors(plan, suite=None, signals=None, **kw):
    suite_, signals_ = _suite_and_signals()
    _, errors, _ = validate_plan(plan, suite or suite_, signals or signals_, **kw)
    return errors


def test_valid_merge_plan_passes():
    suite, signals = _suite_and_signals()
    plan, errors, warnings = validate_plan(_merge_plan(), suite, signals)
    assert errors == []
    assert plan is not None
    assert [d.test_id for d in plan.decisions] == ["TIM-001", "TIM-002", "TIM-003"]


def test_every_intent_needs_a_decision():
    plan = _merge_plan()
    plan["decisions"] = [d for d in plan["decisions"] if d["test_id"] != "TIM-003"]
    assert any("TIM-003: no decision" in e for e in _errors(plan))


def test_unknown_test_id_is_rejected():
    plan = _merge_plan()
    plan["decisions"].append(_decision("TIM-999", "RETIRE"))
    assert any("unknown test_id 'TIM-999'" in e for e in _errors(plan))


def test_duplicate_decision_is_rejected():
    plan = _merge_plan()
    plan["decisions"].append(_decision("TIM-003", "RETIRE"))
    assert any("more than one decision" in e for e in _errors(plan))


def test_decision_without_evidence_is_rejected():
    plan = _merge_plan()
    plan["decisions"][2]["evidence"] = []
    assert any("no evidence" in e for e in _errors(plan))


def test_invented_signal_ref_is_rejected():
    plan = _merge_plan()
    plan["decisions"][2]["evidence"] = [{"signal_ref": "tests.TIM-003.vibes", "detail": "trust me"}]
    assert any("is not a signal in the pack" in e for e in _errors(plan))


def test_merge_below_the_floor_is_rejected():
    """The anti-hallucination gate: TIM-003 shares almost nothing with TIM-001."""
    plan = _merge_plan()
    plan["decisions"][2] = _decision(
        "TIM-003", "MERGE", group_id="g1", primary=False,
        evidence=[{"signal_ref": "redundancy.TIM-001~TIM-003.score", "detail": "low"}],
    )
    plan["merge_groups"][0]["member_test_ids"].append("TIM-003")
    errors = _errors(plan)
    assert any("below the merge floor" in e for e in errors)


def test_merge_floor_is_configurable():
    plan = _merge_plan()
    plan["decisions"][2] = _decision(
        "TIM-003", "MERGE", group_id="g1", primary=False,
        evidence=[{"signal_ref": "redundancy.TIM-001~TIM-003.score", "detail": "low"}],
    )
    plan["merge_groups"][0]["member_test_ids"].append("TIM-003")
    assert not any("below the merge floor" in e for e in _errors(plan, merge_floor=0.0))


def test_merge_needs_exactly_one_primary():
    plan = _merge_plan()
    plan["decisions"][1]["primary"] = True
    assert any("exactly one member must be primary" in e for e in _errors(plan))

    plan = _merge_plan()
    plan["decisions"][0]["primary"] = False
    assert any("exactly one member must be primary" in e for e in _errors(plan))


def test_merge_group_needs_two_members():
    plan = _merge_plan()
    plan["decisions"][1] = _decision("TIM-002", "MIGRATE")
    plan["merge_groups"][0]["member_test_ids"] = ["TIM-001"]
    errors = _errors(plan)
    assert any("needs at least 2 members" in e for e in errors)


def test_merge_without_group_entry_is_rejected():
    plan = _merge_plan()
    plan["merge_groups"] = []
    assert any("has no matching entry in merge_groups" in e for e in _errors(plan))


def test_non_merge_member_in_group_is_rejected():
    plan = _merge_plan()
    plan["decisions"][1]["disposition"] = "MIGRATE"
    assert any("is MIGRATE, not MERGE" in e for e in _errors(plan))


def test_split_needs_two_targets():
    plan = _merge_plan()
    plan["decisions"][2] = _decision("TIM-003", "SPLIT", split_targets=["only one"])
    assert any("SPLIT requires at least 2 split_targets" in e for e in _errors(plan))


def test_defer_needs_a_blocker():
    plan = _merge_plan()
    plan["decisions"][2] = _decision("TIM-003", "DEFER")
    assert any("DEFER requires blocked_by" in e for e in _errors(plan))


def test_redesign_needs_channel_and_note():
    plan = _merge_plan()
    plan["decisions"][2] = _decision("TIM-003", "REDESIGN")
    errors = _errors(plan)
    assert any("REDESIGN requires target_channel" in e for e in errors)
    assert any("REDESIGN requires redesign_note" in e for e in errors)


def test_retiring_application_coverage_warns():
    suite, signals = _suite_and_signals()
    plan = _merge_plan()
    plan["decisions"][2] = _decision("TIM-003", "RETIRE")
    _, errors, warnings = validate_plan(plan, suite, signals)
    assert errors == []
    assert any("RETIRE drops 1 assertion" in w for w in warnings)


def test_retiring_a_tautological_test_does_not_warn():
    suite = fx.suite(fx.tautological_test())
    signals = compute_signals(suite, fx.IR)
    plan = {
        "suite_name": "demo",
        "portfolio_narrative": "n",
        "decisions": [_decision(
            "TIM-004", "RETIRE",
            evidence=[{"signal_ref": "tests.TIM-004.app_independent_assertions",
                       "detail": "asserts only on fixture data"}],
        )],
        "merge_groups": [],
    }
    _, errors, warnings = validate_plan(plan, suite, signals)
    assert errors == []
    assert not any("RETIRE drops" in w for w in warnings)


def test_low_confidence_decision_warns():
    suite, signals = _suite_and_signals()
    plan = _merge_plan()
    plan["decisions"][2]["confidence"] = 0.4
    _, errors, warnings = validate_plan(plan, suite, signals)
    assert errors == []
    assert any("low decision confidence" in w for w in warnings)


def test_schema_violation_is_reported_not_raised():
    suite, signals = _suite_and_signals()
    plan = _merge_plan()
    plan["decisions"][2]["disposition"] = "ARCHIVE"
    result, errors, _ = validate_plan(plan, suite, signals)
    assert result is None
    assert errors and errors[0].startswith("schema:")

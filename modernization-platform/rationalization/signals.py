"""Deterministic signal pack for the rationalization stage.

Nothing here calls an LLM. These are the facts a portfolio decision must rest on:
how redundant a test is, how much it actually proves, what it costs, and whether
the platform can regenerate it at all. The AI's job is judgement on top of these
numbers, and every rationale it writes must cite one of them by dotted path.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from tim.validators import resolve_data_ref

DRUG_KEY_RE = re.compile(r"^data:drugs\.(?P<key>[A-Z0-9_]+)(?:\.(?P<field>\w+))?$")

# Redundancy weights: what a test DOES (step intents) matters most, then how it
# does it (bindings), then what it PROVES with those bindings (assertion shape).
W_STEP_INTENT, W_BINDING, W_ASSERT_SHAPE = 0.4, 0.3, 0.3


def _jaccard(a: Set, b: Set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return round(len(a & b) / len(union), 4) if union else 0.0


def _mask_data_key(ref: Optional[str]) -> Optional[str]:
    """'data:drugs.LIPITOR_BRAND.strength' -> 'data:drugs.*.strength'.

    Masking the data key is what makes two data-variant tests look identical:
    same shape, different row. That is precisely the merge candidate.
    """
    if not ref:
        return ref
    m = DRUG_KEY_RE.match(ref)
    if not m:
        return ref
    return f"data:drugs.*.{m.group('field')}" if m.group("field") else "data:drugs.*"


def _data_keys(test: dict) -> Set[str]:
    keys = set()
    refs = [a.get("expected_ref") for a in test["assertions"]]
    refs += [r for s in test["steps"] for r in s.get("input_refs", [])]
    refs += [d.get("ref") for d in test.get("test_data", [])]
    for ref in refs:
        m = DRUG_KEY_RE.match(ref or "")
        if m:
            keys.add(m.group("key"))
    return keys


def _class_of(binding: Optional[str]) -> Optional[str]:
    if not binding or ":" not in binding:
        return None
    return binding.split(":", 1)[1].split(".", 1)[0]


def _assertion_shape(test: dict) -> Set[str]:
    return {
        "{}|{}|{}".format(
            a["intent"],
            a.get("actual_binding_ref") or "-",
            _mask_data_key(a.get("expected_ref")) or "-",
        )
        for a in test["assertions"]
    }


def _test_signals(test: dict, ir: dict, legacy: Optional[dict], gen_report: Optional[dict]) -> dict:
    asserts = test["assertions"]
    steps = test["steps"]

    oracle_backed, unresolved = 0, []
    for a in asserts:
        ref = a.get("expected_ref")
        if not ref:
            continue
        ok, _ = resolve_data_ref(ref, ir)
        if ok:
            oracle_backed += 1
        else:
            unresolved.append(a["assertion_id"])

    # An assertion with no actual_binding_ref reads nothing from the application:
    # it asserts over the test's own fixture data and therefore proves nothing
    # about the system under test. The strongest RETIRE/REDESIGN signal there is.
    app_independent = [a["assertion_id"] for a in asserts if not a.get("actual_binding_ref")]

    bindings = {s["binding_ref"] for s in steps if s.get("binding_ref")}
    bindings |= {a["actual_binding_ref"] for a in asserts if a.get("actual_binding_ref")}
    pages = sorted({c for c in (_class_of(b) for b in bindings) if c})

    low_conf = [s["step_id"] for s in steps if s["confidence"] < 0.7]
    low_conf += [a["assertion_id"] for a in asserts if a["confidence"] < 0.7]

    sig = {
        "name": test["name"],
        "business_capability": test["business_capability"],
        "risk": test["risk"],
        "step_count": len(steps),
        "step_intents": [s["intent"] for s in steps],
        "step_bindings": sorted(s["binding_ref"] for s in steps if s.get("binding_ref")),
        "assertion_count": len(asserts),
        "assertion_intents": [a["intent"] for a in asserts],
        "oracle_backed_assertions": oracle_backed,
        "literal_backed_assertions": sum(1 for a in asserts if a.get("expected_literal") is not None),
        "app_independent_assertions": app_independent,
        "unresolved_expected_refs": unresolved,
        "distinct_pages": pages,
        "data_keys": sorted(_data_keys(test)),
        "overall_confidence": test["overall_confidence"],
        "min_step_confidence": round(min([s["confidence"] for s in steps], default=1.0), 3),
        "min_assertion_confidence": round(min([a["confidence"] for a in asserts], default=1.0), 3),
        "low_confidence_items": low_conf,
        "legacy_duration_s": legacy.get("duration_s") if legacy else None,
        "legacy_status": legacy.get("status") if legacy else None,
    }

    if gen_report is not None:
        cov = next(
            (c for c in gen_report.get("tim_coverage", []) if c["test_id"] == test["test_id"]), None
        )
        stub_refs = {m["ir_ref"] for m in gen_report.get("methods", []) if m["strategy"] == "todo-stub"}
        partial_refs = {m["ir_ref"] for m in gen_report.get("methods", []) if m["strategy"] == "partial"}
        sig["feasibility"] = {
            "source": "generator dry-run",
            "steps_realized": len(cov["steps"]) if cov else 0,
            "assertions_realized": len(cov["assertions"]) if cov else 0,
            "unimplemented_bindings": sorted(bindings & stub_refs),
            "partial_bindings": sorted(bindings & partial_refs),
            "fully_regenerable": bool(
                cov
                and len(cov["steps"]) >= len(steps)
                and len(cov["assertions"]) >= len(asserts)
                and not (bindings & stub_refs)
            ),
        }
    else:
        sig["feasibility"] = {"source": "unavailable - generator dry-run did not run"}
    return sig


def compute_signals(
    tim_suite: dict,
    ir: dict,
    selenium_run: Optional[dict] = None,
    gen_report: Optional[dict] = None,
) -> dict:
    """Build the full signal pack. Pure function of artifacts already on disk."""
    legacy_by_method = {
        (t["test_class"], t["test_method"]): t for t in (selenium_run or {}).get("tests", [])
    }

    tests: Dict[str, dict] = {}
    for t in tim_suite["tests"]:
        legacy = legacy_by_method.get((t["source"]["test_class"], t["source"]["test_method"]))
        tests[t["test_id"]] = _test_signals(t, ir, legacy, gen_report)

    by_id = {t["test_id"]: t for t in tim_suite["tests"]}
    redundancy: List[dict] = []
    ids = list(by_id)
    for i, a_id in enumerate(ids):
        for b_id in ids[i + 1:]:
            a, b = by_id[a_id], by_id[b_id]
            si = _jaccard(set(tests[a_id]["step_intents"]), set(tests[b_id]["step_intents"]))
            bi = _jaccard(set(tests[a_id]["step_bindings"]), set(tests[b_id]["step_bindings"]))
            shape_a, shape_b = _assertion_shape(a), _assertion_shape(b)
            ai = _jaccard(shape_a, shape_b)
            score = round(W_STEP_INTENT * si + W_BINDING * bi + W_ASSERT_SHAPE * ai, 4)

            ka, kb = set(tests[a_id]["data_keys"]), set(tests[b_id]["data_keys"])
            differing = []
            if ka != kb:
                differing.append(
                    "data rows differ: {} vs {}".format(sorted(ka) or ["-"], sorted(kb) or ["-"])
                )
            only_a, only_b = shape_a - shape_b, shape_b - shape_a
            if only_a or only_b:
                differing.append(
                    "assertion shapes only in {}: {}; only in {}: {}".format(
                        a_id, sorted(only_a), b_id, sorted(only_b)
                    )
                )

            redundancy.append(
                {
                    "pair": [a_id, b_id],
                    "step_intent_jaccard": si,
                    "binding_jaccard": bi,
                    "assertion_shape_jaccard": ai,
                    "score": score,
                    "identical_shape_different_data": bool(
                        si == 1.0 and bi == 1.0 and ai == 1.0 and ka != kb
                    ),
                    "differing": differing,
                }
            )

    runtimes = [s["legacy_duration_s"] for s in tests.values() if s["legacy_duration_s"]]
    suite = {
        "total_tests": len(tests),
        "total_assertions": sum(s["assertion_count"] for s in tests.values()),
        "total_legacy_runtime_s": round(sum(runtimes), 2) if runtimes else None,
        "distinct_business_capabilities": sorted({s["business_capability"] for s in tests.values()}),
        "max_redundancy_score": max([r["score"] for r in redundancy], default=0.0),
    }

    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "suite_name": tim_suite["suite_name"],
        "weights": {
            "step_intent": W_STEP_INTENT,
            "binding": W_BINDING,
            "assertion_shape": W_ASSERT_SHAPE,
        },
        "tests": tests,
        "redundancy": redundancy,
        "suite": suite,
    }


def pair_key(a: str, b: str) -> str:
    return "~".join(sorted((a, b)))


def redundancy_lookup(signals: dict) -> Dict[str, float]:
    return {pair_key(*r["pair"]): r["score"] for r in signals.get("redundancy", [])}


def signal_refs(signals: dict) -> Set[str]:
    """Every citable dotted path. A rationale may only cite refs in this set."""
    refs: Set[str] = set()
    for tid, sig in signals.get("tests", {}).items():
        for field, value in sig.items():
            refs.add(f"tests.{tid}.{field}")
            if field == "feasibility" and isinstance(value, dict):
                for sub in value:
                    refs.add(f"tests.{tid}.feasibility.{sub}")
    for r in signals.get("redundancy", []):
        key = pair_key(*r["pair"])
        for field in r:
            if field != "pair":
                refs.add(f"redundancy.{key}.{field}")
    for field in signals.get("suite", {}):
        refs.add(f"suite.{field}")
    return refs

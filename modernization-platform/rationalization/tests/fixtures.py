"""Synthetic TIM/IR fixtures.

The live estate is three tests in one class, which cannot exercise MERGE refusal,
SPLIT, RETIRE or the validator rejection paths. These fixtures can.
"""
from __future__ import annotations

IR = {
    "page_objects": [],
    "helpers": [],
    "tests": [],
    "config": {"properties": {"healthPlan": "BCBS Alabama"}},
    "test_data": {
        "expected-drugs": {
            "content": {
                "drugs": [
                    {"key": "A_GENERIC", "expectedName": "alpha", "detailTierHeading": "Tier 1",
                     "onDrugList": True},
                    {"key": "B_BRAND", "expectedName": "beta", "detailTierHeading": "Tier 3",
                     "onDrugList": False},
                ]
            }
        }
    },
}


def step(step_id: str, intent: str, binding: str | None, confidence: float = 0.9) -> dict:
    return {
        "step_id": step_id,
        "intent": intent,
        "description": f"do {intent}",
        "channel": "ui",
        "binding_ref": binding,
        "input_refs": [],
        "confidence": confidence,
        "evidence": [],
    }


def assertion(aid: str, intent: str, actual: str | None, expected_ref: str | None,
              confidence: float = 0.9) -> dict:
    return {
        "assertion_id": aid,
        "intent": intent,
        "description": f"check {intent}",
        "actual_binding_ref": actual,
        "expected_ref": expected_ref,
        "expected_literal": None,
        "failure_meaning": "something bad",
        "confidence": confidence,
        "evidence": [],
    }


def test(test_id: str, name: str, steps: list, assertions: list, risk: str = "medium",
         method: str | None = None) -> dict:
    return {
        "tim_version": "1.0",
        "test_id": test_id,
        "name": name,
        "business_capability": "Drug Coverage Lookup",
        "description": name,
        "risk": risk,
        "channel": "ui",
        "preconditions": [],
        "steps": steps,
        "assertions": assertions,
        "test_data": [],
        "reusable_capabilities": [],
        "dependencies": [],
        "source": {"framework": "selenium-java-testng", "test_class": "DemoTests",
                   "test_method": method or test_id.lower().replace("-", "_"), "files": []},
        "overall_confidence": 0.9,
        "provenance": {"model_version": "m", "prompt_version": "p", "extracted_at": "now",
                       "review_status": "pending"},
    }


def suite(*tests: dict) -> dict:
    return {
        "tim_version": "1.0",
        "suite_name": "demo",
        "business_domain": "demo",
        "application_under_test": "demo",
        "tests": list(tests),
    }


def data_variant_pair() -> dict:
    """Two tests with identical shape over different data rows - the merge case."""
    def body(key: str):
        return (
            [step("S1", "search_medicine", "method:SearchPage.addMedicine")],
            [
                assertion("A1", "verify_tier", "method:DetailsPage.getTierHeading",
                          f"data:drugs.{key}.detailTierHeading"),
                assertion("A2", "verify_name", "method:DetailsPage.getDrugName",
                          f"data:drugs.{key}.expectedName"),
            ],
        )

    s1, a1 = body("A_GENERIC")
    s2, a2 = body("B_BRAND")
    return suite(
        test("TIM-001", "generic is covered", s1, a1, method="generic"),
        test("TIM-002", "brand is not covered", s2, a2, method="brand"),
    )


def unrelated_test() -> dict:
    return test(
        "TIM-003",
        "selected plan is displayed",
        [step("S1", "enter_as_guest", "helper:BaseTest.goToFindMedicines")],
        [assertion("A1", "verify_plan", "method:SearchPage.getSelectedHealthPlan", "config:healthPlan")],
        method="plan",
    )


def tautological_test() -> dict:
    """Every assertion reads the fixture data, none reads the application."""
    return test(
        "TIM-004",
        "test data is self-consistent",
        [step("S1", "load_data", "helper:BaseTest.loadData")],
        [assertion("A1", "verify_flag", None, "data:drugs.A_GENERIC.onDrugList")],
        method="sanity",
    )

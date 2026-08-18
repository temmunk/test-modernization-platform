"""TIM validation: structural (schema) + referential (guardrails).

Referential checks are the anti-hallucination gate: every binding_ref the
LLM emits must point at a real IR node, and every data/config ref must
resolve against the deterministic test-data catalog. AI output that cannot
be traced to source evidence is rejected, not silently accepted.
"""
from __future__ import annotations

import re
from typing import List, Tuple

from pydantic import ValidationError

from tim.models import TimSuite

DATA_REF_RE = re.compile(r"^data:(?:drugs\.(?P<key>[A-Z0-9_]+)\.(?P<field>\w+)|(?P<top>\w+))$")
CONFIG_REF_RE = re.compile(r"^config:(?P<prop>\w+)$")


def ir_ids(ir: dict) -> set:
    ids = set()
    for p in ir.get("page_objects", []):
        ids.add(p["id"])
        for l in p.get("locators", []):
            ids.add(l["id"])
        for m in p.get("methods", []):
            ids.add(m["id"])
    for coll in ("helpers", "tests"):
        for n in ir.get(coll, []):
            ids.add(n["id"])
    return ids


def resolve_data_ref(ref: str, ir: dict):
    """Return (ok, resolved_value_or_reason) for a data:/config: reference."""
    m = CONFIG_REF_RE.match(ref)
    if m:
        props = ir["config"]["properties"]
        prop = m.group("prop")
        return (prop in props, props.get(prop, f"unknown config property '{prop}'"))
    m = DATA_REF_RE.match(ref)
    if m:
        catalog = ir.get("test_data", {}).get("expected-drugs", {}).get("content", {})
        if m.group("key"):
            drug = next((d for d in catalog.get("drugs", []) if d.get("key") == m.group("key")), None)
            if drug is None:
                return (False, f"unknown drug key '{m.group('key')}'")
            field = m.group("field")
            if field not in drug:
                return (False, f"unknown field '{field}' on drug '{m.group('key')}'")
            return (True, drug[field])
        top = m.group("top")
        if top in catalog:
            return (True, catalog[top])
        return (False, f"unknown top-level data field '{top}'")
    return (False, f"unrecognized reference format '{ref}'")


def validate_suite(suite_data: dict, ir: dict) -> Tuple[TimSuite | None, List[str], List[str]]:
    """Returns (suite_or_None, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        suite = TimSuite.model_validate(suite_data)
    except ValidationError as e:
        return None, [f"schema: {err['loc']} — {err['msg']}" for err in e.errors()], warnings

    known = ir_ids(ir)
    ir_test_ids = {t["id"] for t in ir.get("tests", [])}

    for t in suite.tests:
        loc = f"{t.test_id} ({t.name})"
        src_id = f"test:{t.source.test_class}.{t.source.test_method}"
        if src_id not in ir_test_ids:
            errors.append(f"{loc}: source trace '{src_id}' does not exist in IR")

        for s in t.steps:
            if s.binding_ref and s.binding_ref not in known:
                errors.append(f"{loc} step {s.step_id}: binding_ref '{s.binding_ref}' not found in IR")
            if not s.binding_ref:
                warnings.append(f"{loc} step {s.step_id}: no binding_ref — cannot be regenerated deterministically")
            for ref in s.input_refs:
                ok, why = resolve_data_ref(ref, ir)
                if not ok:
                    errors.append(f"{loc} step {s.step_id}: input_ref '{ref}': {why}")
            if s.confidence < 0.7:
                warnings.append(f"{loc} step {s.step_id}: low confidence ({s.confidence}) — flag for human review")

        for a in t.assertions:
            if a.actual_binding_ref and a.actual_binding_ref not in known:
                errors.append(f"{loc} assertion {a.assertion_id}: actual_binding_ref '{a.actual_binding_ref}' not found in IR")
            if a.expected_ref:
                ok, why = resolve_data_ref(a.expected_ref, ir)
                if not ok:
                    errors.append(f"{loc} assertion {a.assertion_id}: expected_ref '{a.expected_ref}': {why}")
            if not a.expected_ref and a.expected_literal is None:
                warnings.append(f"{loc} assertion {a.assertion_id}: no expected value reference or literal")
            if a.confidence < 0.7:
                warnings.append(f"{loc} assertion {a.assertion_id}: low confidence ({a.confidence}) — flag for human review")

        for cap in t.reusable_capabilities:
            if cap not in known:
                errors.append(f"{loc}: reusable capability '{cap}' not found in IR")

        for req in t.test_data:
            ok, why = resolve_data_ref(req.ref, ir)
            if not ok:
                # allow whole-record refs like data:drugs.KEY (no field)
                if re.match(r"^data:drugs\.[A-Z0-9_]+$", req.ref):
                    key = req.ref.split(".")[-1]
                    catalog = ir.get("test_data", {}).get("expected-drugs", {}).get("content", {})
                    if not any(d.get("key") == key for d in catalog.get("drugs", [])):
                        errors.append(f"{loc}: test_data ref '{req.ref}': unknown drug key")
                else:
                    errors.append(f"{loc}: test_data ref '{req.ref}': {why}")

    return suite, errors, warnings

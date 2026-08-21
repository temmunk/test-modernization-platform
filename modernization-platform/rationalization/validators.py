"""Rationalization validation: structural (schema) + referential (guardrails).

The gate that matters here is the merge floor. An LLM will happily call two tests
"redundant" because their names rhyme; a merge that is not backed by a computed
structural-overlap score is rejected and fed back as a validation error. Same
principle as the TIM stage: a claim that cannot be traced to a deterministic fact
does not become a decision.
"""
from __future__ import annotations

from typing import List, Tuple

from pydantic import ValidationError

from rationalization.models import Disposition, RationalizationPlan
from rationalization.signals import pair_key, redundancy_lookup, signal_refs

DEFAULT_MERGE_FLOOR = 0.5


def validate_plan(
    plan_data: dict,
    tim_suite: dict,
    signals: dict,
    merge_floor: float = DEFAULT_MERGE_FLOOR,
) -> Tuple[RationalizationPlan | None, List[str], List[str]]:
    """Returns (plan_or_None, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        plan = RationalizationPlan.model_validate(plan_data)
    except ValidationError as e:
        return None, [f"schema: {err['loc']} - {err['msg']}" for err in e.errors()], warnings

    tim_ids = [t["test_id"] for t in tim_suite["tests"]]
    tim_by_id = {t["test_id"]: t for t in tim_suite["tests"]}
    decided = [d.test_id for d in plan.decisions]

    for tid in decided:
        if tid not in tim_by_id:
            errors.append(f"decision for unknown test_id '{tid}' - not in the TIM suite")
    for tid in tim_ids:
        if tid not in decided:
            errors.append(f"{tid}: no decision - every recovered intent needs a disposition")
    for tid in {t for t in decided if decided.count(t) > 1}:
        errors.append(f"{tid}: more than one decision")

    allowed_refs = signal_refs(signals)
    scores = redundancy_lookup(signals)
    groups = {g.group_id: g for g in plan.merge_groups}

    for d in plan.decisions:
        loc = f"{d.test_id}"

        if not d.evidence:
            errors.append(f"{loc}: no evidence - every disposition must cite at least one signal")
        for ev in d.evidence:
            if ev.signal_ref not in allowed_refs:
                errors.append(
                    f"{loc}: evidence signal_ref '{ev.signal_ref}' is not a signal in the pack"
                )

        if d.disposition == Disposition.MERGE:
            if not d.group_id:
                errors.append(f"{loc}: MERGE requires a group_id")
            elif d.group_id not in groups:
                errors.append(f"{loc}: group_id '{d.group_id}' has no matching entry in merge_groups")
        else:
            if d.group_id:
                warnings.append(f"{loc}: group_id set on a non-MERGE disposition - ignored")

        if d.disposition == Disposition.SPLIT and len(d.split_targets) < 2:
            errors.append(f"{loc}: SPLIT requires at least 2 split_targets")
        if d.disposition == Disposition.DEFER and not d.blocked_by:
            errors.append(f"{loc}: DEFER requires blocked_by - say what is blocking it")
        if d.disposition == Disposition.REDESIGN:
            if not d.target_channel:
                errors.append(f"{loc}: REDESIGN requires target_channel")
            if not d.redesign_note:
                errors.append(f"{loc}: REDESIGN requires redesign_note")

        if d.confidence < 0.7:
            warnings.append(f"{loc}: low decision confidence ({d.confidence}) - flag for human review")

        tim = tim_by_id.get(d.test_id)
        sig = signals.get("tests", {}).get(d.test_id, {})
        if d.disposition == Disposition.RETIRE and tim:
            app_dependent = sig.get("assertion_count", 0) - len(sig.get("app_independent_assertions", []))
            if app_dependent > 0:
                warnings.append(
                    f"{loc}: RETIRE drops {app_dependent} assertion(s) that do read from the "
                    f"application - a human must confirm this coverage is genuinely obsolete"
                )
            if tim.get("risk") == "high":
                warnings.append(f"{loc}: RETIRE on a HIGH risk intent - requires explicit sign-off")

    by_id = {d.test_id: d for d in plan.decisions}
    for g in plan.merge_groups:
        loc = f"merge group '{g.group_id}'"
        members = g.member_test_ids
        if len(members) < 2:
            errors.append(f"{loc}: needs at least 2 members")
        if g.primary_test_id not in members:
            errors.append(f"{loc}: primary_test_id '{g.primary_test_id}' is not a member")
        for m in members:
            d = by_id.get(m)
            if d is None:
                errors.append(f"{loc}: member '{m}' has no decision")
            elif d.disposition != Disposition.MERGE:
                errors.append(f"{loc}: member '{m}' is {d.disposition.value}, not MERGE")
            elif d.group_id != g.group_id:
                errors.append(f"{loc}: member '{m}' carries group_id '{d.group_id}'")

        primaries = [m for m in members if by_id.get(m) and by_id[m].primary]
        if len(primaries) != 1:
            errors.append(f"{loc}: exactly one member must be primary, found {primaries or 'none'}")
        elif primaries[0] != g.primary_test_id:
            errors.append(
                f"{loc}: primary flag is on '{primaries[0]}' but primary_test_id is '{g.primary_test_id}'"
            )

        # The anti-hallucination gate for this stage.
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                score = scores.get(pair_key(a, b))
                if score is None:
                    errors.append(f"{loc}: no computed redundancy signal for {a}/{b}")
                elif score < merge_floor:
                    errors.append(
                        f"{loc}: {a} and {b} have redundancy score {score}, below the merge floor "
                        f"{merge_floor} - they are not the same test and must not be merged"
                    )

    return plan, errors, warnings

"""Rationalization service - turns recovered intent + deterministic signals into a
portfolio decision per test.

Same contract as the understanding stage: the LLM supplies judgement, the platform
supplies the facts, validates the output referentially, stamps provenance, and retries
with the validation errors when the model gets it wrong.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

import anthropic

from rationalization.models import (
    PortfolioSummary,
    RationalizationPlan,
    export_json_schema,
)
from rationalization.signals import signal_refs
from rationalization.validators import DEFAULT_MERGE_FLOOR, validate_plan

PROMPT_VERSION = "rationalization_v1"
DEFAULT_MODEL = os.environ.get("TIM_LLM_MODEL", "claude-sonnet-5")
MAX_TOKENS = 16000

PROMPTS_DIR = Path(__file__).parent.parent / "understanding" / "prompts"


def _load_prompt() -> tuple[str, str]:
    text = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    system = text.split("## System", 1)[1].split("## User", 1)[0].strip()
    user_tpl = text.split("## User (template)", 1)[1].strip()
    return system, user_tpl


def _slim_tim(tim_suite: dict) -> dict:
    """The TIM minus evidence blobs - the decision needs meaning, not line numbers."""
    slim = {"suite_name": tim_suite["suite_name"], "business_domain": tim_suite["business_domain"],
            "application_under_test": tim_suite["application_under_test"], "tests": []}
    for t in tim_suite["tests"]:
        slim["tests"].append(
            {
                "test_id": t["test_id"],
                "name": t["name"],
                "business_capability": t["business_capability"],
                "description": t["description"],
                "risk": t["risk"],
                "overall_confidence": t["overall_confidence"],
                "source": t["source"],
                "steps": [
                    {k: s[k] for k in ("step_id", "intent", "description", "binding_ref",
                                       "input_refs", "confidence")}
                    for s in t["steps"]
                ],
                "assertions": [
                    {k: a[k] for k in ("assertion_id", "intent", "description", "actual_binding_ref",
                                       "expected_ref", "expected_literal", "failure_meaning",
                                       "confidence")}
                    for a in t["assertions"]
                ],
            }
        )
    return slim


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    start = text.index("{")
    return json.loads(text[start:])


def summarize(plan: RationalizationPlan, signals: dict) -> PortfolioSummary:
    """Platform-computed portfolio numbers. Never taken from the model."""
    by_disp: dict = {}
    for d in plan.decisions:
        by_disp[d.disposition.value] = by_disp.get(d.disposition.value, 0) + 1

    not_migrated = [d.test_id for d in plan.decisions if d.disposition.value in ("RETIRE", "DEFER")]

    # A merge group of N intents collapses to 1 implementation: N-1 bodies eliminated.
    eliminated = sum(max(len(g.member_test_ids) - 1, 0) for g in plan.merge_groups)
    eliminated += len(not_migrated)

    reclaimed = 0.0
    for tid in not_migrated:
        d = signals.get("tests", {}).get(tid, {}).get("legacy_duration_s")
        if d:
            reclaimed += d

    return PortfolioSummary(
        total_tests=len(plan.decisions),
        by_disposition=by_disp,
        implementations_eliminated=eliminated,
        legacy_runtime_reclaimed_s=round(reclaimed, 2),
        tests_not_migrated=not_migrated,
    )


def rationalize(
    tim_suite: dict,
    signals: dict,
    merge_floor: float = DEFAULT_MERGE_FLOOR,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> Tuple[RationalizationPlan, List[str]]:
    """Run rationalization. Returns (validated plan, warnings). Raises on failure."""
    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    system, user_tpl = _load_prompt()

    user = (
        user_tpl.replace("{TIM_JSON}", json.dumps(_slim_tim(tim_suite), indent=1))
        .replace("{SIGNALS_JSON}", json.dumps(signals, indent=1))
        .replace("{SIGNAL_REFS}", json.dumps(sorted(signal_refs(signals)), indent=1))
        .replace("{MERGE_FLOOR}", str(merge_floor))
        .replace("{PLAN_SCHEMA}", json.dumps(export_json_schema()))
    )
    system = system.replace("{MERGE_FLOOR}", str(merge_floor))

    messages = [{"role": "user", "content": user}]
    last_errors: List[str] = []

    for _attempt in range(3):
        with client.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()
        raw = "".join(b.text for b in resp.content if b.type == "text")

        try:
            data = _extract_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            last_errors = [f"output was not parseable JSON: {e}"]
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Your output failed: {last_errors[0]}. "
                                            "Re-emit the full corrected JSON object only."},
            ]
            continue

        data.setdefault("suite_name", tim_suite["suite_name"])
        stamp = {
            "model_version": getattr(resp, "model", model),
            "prompt_version": PROMPT_VERSION,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "pending",
        }
        data["provenance"] = stamp
        for d in data.get("decisions", []):
            d.setdefault("decided_by", "ai")
            d.setdefault("decided_at", stamp["extracted_at"])

        plan, errors, warnings = validate_plan(data, tim_suite, signals, merge_floor)
        if plan is not None and not errors:
            plan.summary = summarize(plan, signals)
            return plan, warnings

        last_errors = errors
        messages += [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your output failed validation:\n"
                + "\n".join(f"- {e}" for e in errors[:30])
                + "\nRe-emit the full corrected JSON object only.",
            },
        ]

    raise RuntimeError("Rationalization failed after 3 attempts:\n" + "\n".join(last_errors[:30]))

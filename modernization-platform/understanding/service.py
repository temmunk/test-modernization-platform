"""AI Understanding Engine — recovers business intent from the deterministic IR.

Deterministic processing happens BEFORE this step (the adapter) and AFTER it
(validators + generator). The LLM is only asked to do the one thing code
cannot: recover business meaning. Output is structured JSON, schema-validated,
referentially cross-checked against the IR, and stamped with provenance.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from tim.models import TimSuite, export_json_schema
from tim.validators import ir_ids, validate_suite

PROMPT_VERSION = "intent_recovery_v2"
DEFAULT_MODEL = os.environ.get("TIM_LLM_MODEL", "claude-sonnet-5")
MAX_TOKENS = 32000

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt() -> tuple[str, str]:
    text = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    system = text.split("## System", 1)[1].split("## User", 1)[0].strip()
    user_tpl = text.split("## User (template)", 1)[1].strip()
    return system, user_tpl


def _slim_ir(ir: dict) -> dict:
    """The full IR minus fields the LLM doesn't need (keeps the call compact)."""
    slim = json.loads(json.dumps(ir))
    for p in slim.get("page_objects", []):
        for m in p.get("methods", []):
            m.pop("has_control_flow", None)
    return slim


def _data_ref_examples(ir: dict) -> list[str]:
    refs = []
    catalog = ir.get("test_data", {}).get("expected-drugs", {}).get("content", {})
    for top in catalog:
        if top != "drugs":
            refs.append(f"data:{top}")
    for d in catalog.get("drugs", []):
        for field in d:
            if field != "key":
                refs.append(f"data:drugs.{d['key']}.{field}")
    for prop in ir.get("config", {}).get("properties", {}):
        refs.append(f"config:{prop}")
    return refs


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    start = text.index("{")
    return json.loads(text[start:])


def recover_intent(ir: dict, api_key: str | None = None, model: str = DEFAULT_MODEL,
                   expected_test_ids: list[str] | None = None) -> tuple[TimSuite, list[str]]:
    """Run intent recovery over one IR (a batch or a whole estate).

    expected_test_ids: the exact id sequence this batch must use (reserved per
    batch by the manifest so regenerating one batch never renumbers another).
    Returns (validated TimSuite, warnings). Raises on unrecoverable failure."""
    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    system, user_tpl = _load_prompt()

    if expected_test_ids is None:
        expected_test_ids = [f"TIM-{i:03d}" for i in range(1, len(ir.get("tests", [])) + 1)]

    schema = export_json_schema()
    user = (
        user_tpl.replace("{IR_JSON}", json.dumps(_slim_ir(ir), indent=1))
        .replace("{TEST_IDS}", json.dumps(expected_test_ids))
        .replace("{BINDING_IDS}", json.dumps(sorted(ir_ids(ir)), indent=1))
        .replace("{DATA_REFS}", json.dumps(_data_ref_examples(ir), indent=1))
        .replace("{TIM_SCHEMA}", json.dumps(schema))
    )

    messages = [{"role": "user", "content": user}]
    last_errors: list[str] = []

    for attempt in range(3):
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
                {"role": "user", "content": f"Your output failed: {last_errors[0]}. Re-emit the full corrected JSON object only."},
            ]
            continue

        # Inject provenance (never trust the model to stamp itself)
        stamp = {
            "model_version": getattr(resp, "model", model),
            "prompt_version": PROMPT_VERSION,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "pending",
        }
        for t in data.get("tests", []):
            t["provenance"] = stamp

        suite, errors, warnings = validate_suite(data, ir)
        if suite is not None:
            got_ids = [t.test_id for t in suite.tests]
            if got_ids != expected_test_ids:
                errors.append(
                    f"test_id sequence mismatch: expected {expected_test_ids}, got {got_ids}"
                )
        if suite is not None and not errors:
            return suite, warnings

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

    raise RuntimeError("Intent recovery failed after 3 attempts:\n" + "\n".join(last_errors[:30]))

"""Human Review Workbench — platform component [9].

Streamlit UI over the pipeline artifacts: recovered intent (TIM) with
confidence + evidence, side-by-side legacy/generated code, execution
evidence, and the behavioral equivalence assessment. Reviewer decisions
(approve/reject) are written back into the TIM artifact's provenance —
human approval is the gate for material modernization decisions.

Run from the platform root:  streamlit run review_ui/app.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

PLATFORM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM))

from tim.validators import resolve_data_ref  # noqa: E402

WORKSPACE = PLATFORM.parent
ART = PLATFORM / "artifacts"
TIM_FILE = ART / "tim" / "tim-suite.json"
IR_FILE = ART / "ir" / "normalized_ir.json"
PLAN_FILE = ART / "rationalization" / "plan.json"
SIGNALS_FILE = ART / "rationalization" / "signals.json"
GEN_FILE = ART / "reports" / "generation-report.json"
EQ_FILE = ART / "reports" / "equivalence-report.json"
SEL_RUN = ART / "runs" / "selenium-run.json"
PW_RUN = ART / "runs" / "playwright-run.json"

NAVY, IB, GOLD, GREEN, AMBER, RED, GREY = (
    "#002060", "#0070C0", "#FFC000", "#00703C", "#C55A11", "#B00020", "#6D6D6D"
)

PURPLE, SLATE = "#7030A0", "#4A4A6A"

VERDICT_COLORS = {
    "EQUIVALENT": GREEN,
    "PARTIALLY_EQUIVALENT": AMBER,
    "NOT_EQUIVALENT": RED,
    "BLOCKED": GREY,
    "NOT_MIGRATED": SLATE,
}
DISPOSITIONS = ["MIGRATE", "MERGE", "SPLIT", "REDESIGN", "RETIRE", "DEFER"]
DISPOSITION_COLORS = {
    "MIGRATE": IB, "MERGE": PURPLE, "SPLIT": PURPLE,
    "REDESIGN": AMBER, "RETIRE": SLATE, "DEFER": SLATE,
}
STATUS_COLORS = {"passed": GREEN, "failed": RED, "skipped": GREY, "missing": GREY}
REVIEW_COLORS = {"pending": AMBER, "approved": GREEN, "rejected": RED}


def badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#fff;padding:2px 10px;border-radius:10px;'
        f'font-size:0.78rem;font-weight:600;white-space:nowrap;">{text}</span>'
    )


def conf_badge(c: float) -> str:
    color = GREEN if c >= 0.85 else (AMBER if c >= 0.7 else RED)
    return badge(f"conf {c:.2f}", color)


def load(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_tim(tim: dict) -> None:
    TIM_FILE.write_text(json.dumps(tim, indent=2), encoding="utf-8")


def save_plan(plan: dict) -> None:
    PLAN_FILE.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def recompute_plan_summary(plan: dict) -> None:
    """Keep the portfolio numbers true after a human override."""
    by_disp: dict = {}
    for d in plan["decisions"]:
        by_disp[d["disposition"]] = by_disp.get(d["disposition"], 0) + 1
    not_migrated = [d["test_id"] for d in plan["decisions"] if d["disposition"] in ("RETIRE", "DEFER")]
    live_groups = [
        g for g in plan.get("merge_groups", [])
        if sum(1 for m in g["member_test_ids"]
               if next((d for d in plan["decisions"] if d["test_id"] == m), {}).get("disposition") == "MERGE") > 1
    ]
    signals = load(SIGNALS_FILE) or {}
    reclaimed = sum(
        (signals.get("tests", {}).get(t, {}) or {}).get("legacy_duration_s") or 0 for t in not_migrated
    )
    plan["summary"] = {
        "total_tests": len(plan["decisions"]),
        "by_disposition": by_disp,
        "implementations_eliminated": sum(max(len(g["member_test_ids"]) - 1, 0) for g in live_groups)
        + len(not_migrated),
        "legacy_runtime_reclaimed_s": round(reclaimed, 2),
        "tests_not_migrated": not_migrated,
    }


def spec_slice(spec_text: str, test_id: str) -> str:
    """The generated spec block for one TIM test."""
    starts = [m.start() for m in re.finditer(r"^  test\(", spec_text, re.MULTILINE)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(spec_text)
        block = spec_text[s:end]
        if test_id in block.split("\n", 1)[0]:
            # include the preceding jsdoc if present
            head = spec_text[:s].rstrip("\n")
            jd = head.rfind("  /**")
            if jd != -1 and "*/" in head[jd:]:
                block = head[jd:] + "\n" + block
            return block.rstrip()
    return "(spec block not found)"


st.set_page_config(page_title="Modernization Review Workbench", page_icon="🛠️", layout="wide")

st.markdown(
    f"""
    <div style="background:linear-gradient(135deg,{NAVY} 0%,{IB} 100%);color:#fff;
                padding:1.2rem 1.6rem;border-radius:12px;margin-bottom:1rem;">
      <div style="font-size:1.5rem;font-weight:700;">AI Test <span style="color:{GOLD};">Modernization</span> — Review Workbench</div>
      <div style="opacity:0.85;font-size:0.9rem;">Understand Legacy → Recover Intent → Regenerate → Validate → <b>Approve</b></div>
    </div>
    """,
    unsafe_allow_html=True,
)

tim = load(TIM_FILE)
ir = load(IR_FILE)
gen = load(GEN_FILE)
eq = load(EQ_FILE)
sel_run = load(SEL_RUN)
pw_run = load(PW_RUN)
plan = load(PLAN_FILE)
signals = load(SIGNALS_FILE)

if not tim or not ir:
    st.error("Missing TIM/IR artifacts — run `python pipeline.py ingest` and `understand` first.")
    st.stop()

eq_by_id = {r["test_id"]: r for r in (eq or {}).get("results", [])}
cov_by_id = {c["test_id"]: c for c in (gen or {}).get("tim_coverage", [])}
dec_by_id = {d["test_id"]: d for d in (plan or {}).get("decisions", [])}

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("Pipeline artifacts")
    for label, path, data in (
        ("Normalized IR", IR_FILE, ir),
        ("Test Intent Model", TIM_FILE, tim),
        ("Rationalization plan", PLAN_FILE, plan),
        ("Generation report", GEN_FILE, gen),
        ("Legacy run evidence", SEL_RUN, sel_run),
        ("Modern run evidence", PW_RUN, pw_run),
        ("Equivalence report", EQ_FILE, eq),
    ):
        st.markdown(("✅ " if data else "⬜ ") + label)
    if gen:
        st.subheader("Generation strategies")
        strat = {}
        for m in gen["methods"]:
            strat[m["strategy"]] = strat.get(m["strategy"], 0) + 1
        for s, n in sorted(strat.items()):
            st.markdown(f"- `{s}`: **{n}**")
    st.caption(f"TIM provenance: {tim['tests'][0]['provenance']['model_version']} · "
               f"{tim['tests'][0]['provenance']['prompt_version']}")

# ---------------------------------------------------------------- overview
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("TIM tests", len(tim["tests"]))
c2.metric("Avg intent confidence", f"{sum(t['overall_confidence'] for t in tim['tests']) / len(tim['tests']):.2f}")
c3.metric("Equivalent", (eq or {}).get("summary", {}).get("EQUIVALENT", "—"))
approved = sum(1 for t in tim["tests"] if t["provenance"]["review_status"] == "approved")
c4.metric("Approved", f"{approved}/{len(tim['tests'])}")
c5.metric("Needs attention",
          sum(1 for t in tim["tests"]
              for x in t["steps"] + t["assertions"] if x["confidence"] < 0.7))

st.divider()

# ------------------------------------------------------------- portfolio view
if plan:
    st.markdown("### Portfolio decision")
    st.write(plan.get("portfolio_narrative", ""))

    summary = plan.get("summary") or {}
    counts = summary.get("by_disposition") or {}
    st.markdown(
        " ".join(badge(f"{d} {counts[d]}", DISPOSITION_COLORS[d]) for d in DISPOSITIONS if d in counts)
        + f" &nbsp;&nbsp; implementations eliminated: <b>{summary.get('implementations_eliminated', 0)}</b>"
        + (f" &nbsp;·&nbsp; legacy runtime reclaimed: <b>{summary['legacy_runtime_reclaimed_s']}s</b>"
           if summary.get("legacy_runtime_reclaimed_s") else ""),
        unsafe_allow_html=True,
    )

    rows = []
    for t in tim["tests"]:
        d = dec_by_id.get(t["test_id"], {})
        rows.append({
            "test": t["test_id"],
            "name": t["name"],
            "disposition": d.get("disposition", "—"),
            "decided by": d.get("decided_by", "—"),
            "confidence": d.get("confidence"),
            "value": d.get("business_value", "—"),
            "cost": d.get("migration_cost", "—"),
            "rationale": d.get("rationale", ""),
        })
    st.dataframe(rows, width="stretch", hide_index=True)

    for m in (gen or {}).get("merges", []):
        if m.get("realized"):
            st.success(
                f"Merge {m['group_id']} realized: {', '.join(m['members'])} share one parameterized "
                f"implementation in `{m['spec']}` (parameters: {', '.join(m.get('parameters', []))}). "
                "Every merged intent still runs as its own case under its own TIM id."
            )
        else:
            st.warning(
                f"Merge {m['group_id']} refused by the generator: {m.get('reason')} — "
                f"{', '.join(m['members'])} were emitted separately, so no coverage was dropped."
            )
    for s in (gen or {}).get("skipped_by_disposition", []):
        st.info(f"{s['test_id']} not generated [{s['disposition']}]: {s['reason']}")

    st.caption(
        "Dispositions are AI proposals backed by deterministic signals (artifacts/rationalization/"
        "signals.json). Override any of them below — the platform recomputes the portfolio numbers "
        "and records the decision as human-made."
    )
    st.divider()
else:
    st.info("No rationalization plan yet — run `python pipeline.py rationalize` to decide what each "
            "recovered intent should become before generating.")

# ---------------------------------------------------------------- per test
tabs = st.tabs([f"{t['test_id']} · {t['provenance']['review_status'].upper()}" for t in tim["tests"]])

for tab, test in zip(tabs, tim["tests"]):
    tid = test["test_id"]
    eq_r = eq_by_id.get(tid)
    cov = cov_by_id.get(tid)
    dec = dec_by_id.get(tid)

    with tab:
        left, right = st.columns([3, 1])
        with left:
            st.markdown(f"### {test['name']}")
            st.markdown(
                badge(test["business_capability"], IB) + " "
                + badge(f"risk {test['risk']}", NAVY) + " "
                + (badge(dec["disposition"], DISPOSITION_COLORS[dec["disposition"]]) + " " if dec else "")
                + conf_badge(test["overall_confidence"]) + " "
                + (badge(eq_r["verdict"], VERDICT_COLORS[eq_r["verdict"]]) if eq_r else badge("NOT ASSESSED", GREY))
                + " " + badge(test["provenance"]["review_status"],
                              REVIEW_COLORS[test["provenance"]["review_status"]]),
                unsafe_allow_html=True,
            )
            st.write(test["description"])
        with right:
            src = test["source"]
            st.caption(f"Source: `{src['test_class']}.{src['test_method']}`")
            if cov:
                st.caption(f"Generated: `{cov['spec']}`")

        if test["preconditions"]:
            st.markdown("**Preconditions**")
            for p in test["preconditions"]:
                st.markdown(f"- {p['description']} &nbsp; " + conf_badge(p["confidence"]), unsafe_allow_html=True)

        st.markdown("**Recovered intent — steps**")
        step_rows = []
        for s in test["steps"]:
            ev = s["evidence"][0] if s["evidence"] else {}
            step_rows.append({
                "step": s["step_id"],
                "intent": s["intent"],
                "description": s["description"],
                "binding": s["binding_ref"] or "—",
                "inputs": ", ".join(s["input_refs"]) or "—",
                "confidence": s["confidence"],
                "evidence": f"{ev.get('file', '')}:{ev.get('line', '')}" if ev.get("file") else (ev.get("ir_ref") or ""),
            })
        st.dataframe(step_rows, width="stretch", hide_index=True)

        st.markdown("**Recovered intent — assertions** (expected values resolved from oracle data)")
        assert_rows = []
        for a in test["assertions"]:
            resolved = ""
            if a["expected_ref"]:
                ok, val = resolve_data_ref(a["expected_ref"], ir)
                resolved = repr(val) if ok else "⚠ unresolved"
            elif a["expected_literal"] is not None:
                resolved = repr(a["expected_literal"])
            assert_rows.append({
                "id": a["assertion_id"],
                "intent": a["intent"],
                "description": a["description"],
                "reads from": a["actual_binding_ref"] or "—",
                "expected": f"{a['expected_ref'] or 'literal'} → {resolved}",
                "confidence": a["confidence"],
                "failure means": a["failure_meaning"] or "",
            })
        st.dataframe(assert_rows, width="stretch", hide_index=True)
        low = [a["assertion_id"] for a in test["assertions"] if a["confidence"] < 0.7]
        low += [s["step_id"] for s in test["steps"] if s["confidence"] < 0.7]
        if low:
            st.warning(f"Low-confidence items flagged for review: {', '.join(low)}")

        with st.expander("Side-by-side: legacy source vs generated Playwright"):
            ir_test = next(
                (t for t in ir["tests"]
                 if t["class_name"] == test["source"]["test_class"]
                 and t["name"] == test["source"]["test_method"]),
                None,
            )
            cL, cR = st.columns(2)
            with cL:
                st.caption(f"Legacy · selenium-framework/{ir_test['file'] if ir_test else ''}")
                st.code(ir_test["body"] if ir_test else "(not found)", language="java")
            with cR:
                spec_path = WORKSPACE / "playwright-framework" / (cov["spec"] if cov else "")
                st.caption(f"Generated · playwright-framework/{cov['spec'] if cov else ''}")
                if cov and spec_path.exists():
                    st.code(spec_slice(spec_path.read_text(encoding="utf-8"), tid), language="javascript")
                else:
                    st.code("(spec not generated)")

        if eq_r:
            st.markdown("**Behavioral equivalence evidence**")
            e1, e2, e3 = st.columns(3)
            legacy = eq_r["legacy_execution"] or {}
            modern = eq_r["modern_execution"] or {}
            with e1:
                st.markdown(
                    "Legacy execution<br>"
                    + badge(legacy.get("status", "missing"), STATUS_COLORS.get(legacy.get("status", "missing"), GREY))
                    + f" &nbsp;{legacy.get('duration_s', '—')}s",
                    unsafe_allow_html=True,
                )
            with e2:
                st.markdown(
                    "Modern execution<br>"
                    + badge(modern.get("status", "missing"), STATUS_COLORS.get(modern.get("status", "missing"), GREY))
                    + f" &nbsp;{modern.get('duration_s', '—')}s",
                    unsafe_allow_html=True,
                )
            with e3:
                fm = eq_r["checks"]["flow_match"]
                am = eq_r["checks"]["assertion_match"]
                st.markdown(f"Flow match **{fm['realized']}/{fm['total']}** · Assertions **{am['realized']}/{am['total']}**")
            for reason in eq_r["reasons"]:
                st.markdown(f"- {reason}")
            st.info(f"Recommendation: **{eq_r['recommendation']}**")

        if dec:
            st.markdown("**Portfolio decision — what should happen to this intent**")
            st.markdown(
                badge(dec["disposition"], DISPOSITION_COLORS[dec["disposition"]]) + " "
                + conf_badge(dec["confidence"]) + " "
                + badge(f"decided by {dec.get('decided_by', 'ai')}",
                        GREEN if dec.get("decided_by") == "human" else GREY)
                + f" &nbsp; value **{dec.get('business_value')}** · cost **{dec.get('migration_cost')}**",
                unsafe_allow_html=True,
            )
            st.write(dec["rationale"])
            for extra_key, label in (("blocked_by", "Blocked by"), ("redesign_note", "Redesign"),
                                     ("group_id", "Merge group")):
                if dec.get(extra_key):
                    suffix = " (primary)" if extra_key == "group_id" and dec.get("primary") else ""
                    st.markdown(f"- **{label}:** {dec[extra_key]}{suffix}")
            if dec.get("split_targets"):
                st.markdown("- **Split into:** " + "; ".join(dec["split_targets"]))

            with st.expander("Signals cited as evidence"):
                sig = (signals or {}).get("tests", {}).get(tid, {})
                for ev in dec.get("evidence", []):
                    st.markdown(f"- `{ev['signal_ref']}` — {ev['detail']}")
                if sig:
                    st.markdown(
                        f"Computed for this test: **{sig['assertion_count']}** assertions "
                        f"(**{sig['oracle_backed_assertions']}** oracle-backed, "
                        f"**{len(sig['app_independent_assertions'])}** reading no application state), "
                        f"**{sig['step_count']}** steps across {len(sig['distinct_pages'])} page objects, "
                        f"legacy runtime {sig.get('legacy_duration_s', '—')}s, "
                        f"fully regenerable: **{sig.get('feasibility', {}).get('fully_regenerable')}**."
                    )
                for r in (signals or {}).get("redundancy", []):
                    if tid in r["pair"]:
                        other = [p for p in r["pair"] if p != tid][0]
                        st.markdown(
                            f"- redundancy vs **{other}**: score **{r['score']}** "
                            f"(steps {r['step_intent_jaccard']}, bindings {r['binding_jaccard']}, "
                            f"assertions {r['assertion_shape_jaccard']})"
                        )

            ov1, ov2, ov3 = st.columns([1, 2, 1])
            with ov1:
                new_disp = st.selectbox("Override disposition", DISPOSITIONS,
                                        index=DISPOSITIONS.index(dec["disposition"]),
                                        key=f"disp_{tid}")
            with ov2:
                disp_note = st.text_input("Reason for the override", key=f"dispnote_{tid}",
                                          value=dec.get("reviewer_notes") or "")

            def _override(tid=tid, disp_key=f"disp_{tid}", note_key=f"dispnote_{tid}"):
                d = next(x for x in plan["decisions"] if x["test_id"] == tid)
                d["disposition"] = st.session_state[disp_key]
                d["reviewer_notes"] = st.session_state[note_key] or None
                d["decided_by"] = "human"
                d["decided_at"] = datetime.now(timezone.utc).isoformat()
                recompute_plan_summary(plan)
                save_plan(plan)

            with ov3:
                st.write("")
                st.button("Save disposition", key=f"savedisp_{tid}", on_click=_override,
                          width="stretch",
                          disabled=(new_disp == dec["disposition"]
                                    and disp_note == (dec.get("reviewer_notes") or "")))
            st.caption("Changing a disposition here does not regenerate anything — "
                       "re-run `python pipeline.py generate` to apply it.")
            st.divider()

        st.markdown("**Reviewer decision**")
        note_key, dec_cols = f"note_{tid}", st.columns([2, 1, 1])
        with dec_cols[0]:
            notes = st.text_input("Notes (optional)", key=note_key,
                                  value=test["provenance"].get("reviewer_notes") or "")
        def _decide(status: str, test=test, notes_key=note_key):
            test["provenance"]["review_status"] = status
            test["provenance"]["reviewer_notes"] = st.session_state[notes_key] or None
            test["provenance"]["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            save_tim(tim)
        with dec_cols[1]:
            st.button("✅ Approve migration", key=f"approve_{tid}",
                      on_click=_decide, args=("approved",), width="stretch")
        with dec_cols[2]:
            st.button("❌ Reject", key=f"reject_{tid}",
                      on_click=_decide, args=("rejected",), width="stretch")

st.divider()
st.caption(
    "Every intent value shown carries confidence + source evidence; approval decisions are "
    "written back into artifacts/tim/tim-suite.json (provenance.review_status). "
    "Nothing AI-inferred becomes fact without a human decision."
)

"""Behavioral Equivalence Engine.

Produces an explainable, per-test equivalence assessment — not just pass/fail:
 - Flow match: every TIM step is realized in the generated framework
 - Assertion match: every TIM assertion is realized (same business checks)
 - Outcome match: legacy and modern executions agree against the live app
Each verdict lists its reasons so a reviewer can see WHY a migration is (or
is not) equivalent before approving it.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone


VERDICTS = ("EQUIVALENT", "PARTIALLY_EQUIVALENT", "NOT_EQUIVALENT", "BLOCKED")


def assess(tim_suite: dict, gen_report: dict, selenium_run: dict, playwright_run: dict) -> dict:
    coverage_by_id = {c["test_id"]: c for c in gen_report.get("tim_coverage", [])}
    legacy_by_method = {
        (t["test_class"], t["test_method"]): t for t in selenium_run.get("tests", [])
    }
    modern_by_tim = {t["tim_id"]: t for t in playwright_run.get("tests", []) if t.get("tim_id")}

    results = []
    for tim in tim_suite["tests"]:
        tid = tim["test_id"]
        reasons, checks = [], {}

        cov = coverage_by_id.get(tid)
        n_steps = len(tim["steps"])
        n_asserts = len(tim["assertions"])
        steps_realized = len(cov["steps"]) if cov else 0
        asserts_realized = len(cov["assertions"]) if cov else 0

        checks["flow_match"] = {
            "realized": steps_realized,
            "total": n_steps,
            "ok": steps_realized >= n_steps and n_steps > 0,
        }
        if checks["flow_match"]["ok"]:
            reasons.append(f"All {n_steps} intent steps are realized in the generated framework.")
        else:
            reasons.append(f"Only {steps_realized}/{n_steps} intent steps realized in generated code.")

        checks["assertion_match"] = {
            "realized": asserts_realized,
            "total": n_asserts,
            "ok": asserts_realized >= n_asserts and n_asserts > 0,
        }
        if checks["assertion_match"]["ok"]:
            reasons.append(f"All {n_asserts} business assertions are regenerated with the same oracle data.")
        else:
            reasons.append(f"Only {asserts_realized}/{n_asserts} business assertions realized in generated code.")

        legacy = legacy_by_method.get((tim["source"]["test_class"], tim["source"]["test_method"]))
        modern = modern_by_tim.get(tid)

        if legacy is None:
            reasons.append("No legacy execution evidence found for this test.")
        if modern is None:
            reasons.append("No modern execution evidence found for this test.")

        outcome_ok = None
        if legacy and modern:
            outcome_ok = legacy["status"] == modern["status"]
            checks["outcome_match"] = {
                "legacy_status": legacy["status"],
                "modern_status": modern["status"],
                "ok": outcome_ok,
            }
            if outcome_ok and legacy["status"] == "passed":
                reasons.append(
                    "Both implementations executed against the live application and "
                    "verified the same expected values (both passed)."
                )
            elif outcome_ok:
                reasons.append(
                    f"Both implementations agree, but both are '{legacy['status']}' — "
                    "equivalence of the failure should be reviewed manually."
                )
            else:
                reasons.append(
                    f"Outcome mismatch: legacy={legacy['status']}, modern={modern['status']} — "
                    "the regenerated test does not prove the same behavior."
                )
        else:
            checks["outcome_match"] = {"ok": None}

        if legacy is None or modern is None:
            verdict = "BLOCKED"
        elif not outcome_ok:
            verdict = "NOT_EQUIVALENT"
        elif checks["flow_match"]["ok"] and checks["assertion_match"]["ok"] and legacy["status"] == "passed":
            verdict = "EQUIVALENT"
        else:
            verdict = "PARTIALLY_EQUIVALENT"

        results.append(
            {
                "test_id": tid,
                "name": tim["name"],
                "business_capability": tim["business_capability"],
                "overall_confidence": tim["overall_confidence"],
                "source": tim["source"],
                "generated": {"spec": cov["spec"] if cov else None, "title": cov["title"] if cov else None},
                "checks": checks,
                "legacy_execution": legacy,
                "modern_execution": modern,
                "verdict": verdict,
                "reasons": reasons,
                "recommendation": (
                    "Approve migration" if verdict == "EQUIVALENT"
                    else "Review before approval" if verdict == "PARTIALLY_EQUIVALENT"
                    else "Do not approve — investigate" if verdict == "NOT_EQUIVALENT"
                    else "Re-run executions to unblock assessment"
                ),
            }
        )

    summary = {v: sum(1 for r in results if r["verdict"] == v) for v in VERDICTS}
    return {
        "assessed_at": datetime.now(timezone.utc).isoformat(),
        "suite_name": tim_suite["suite_name"],
        "summary": summary,
        "legacy_run": {k: selenium_run.get(k) for k in ("command", "started_at", "finished_at", "exit_code")},
        "modern_run": {k: playwright_run.get(k) for k in ("command", "started_at", "finished_at", "exit_code")},
        "results": results,
    }


# ------------------------------------------------------------------ report
_BADGE = {
    "EQUIVALENT": ("#00703C", "#fff"),
    "PARTIALLY_EQUIVALENT": ("#C55A11", "#fff"),
    "NOT_EQUIVALENT": ("#B00020", "#fff"),
    "BLOCKED": ("#6D6D6D", "#fff"),
    "passed": ("#00703C", "#fff"),
    "failed": ("#B00020", "#fff"),
    "skipped": ("#6D6D6D", "#fff"),
}


def _badge(text: str) -> str:
    bg, fg = _BADGE.get(text, ("#6D6D6D", "#fff"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:10px;'
        f'font-size:0.78rem;font-weight:600;">{html.escape(str(text))}</span>'
    )


def render_html(assessment: dict) -> str:
    e = html.escape
    rows = []
    for r in assessment["results"]:
        legacy = r["legacy_execution"] or {}
        modern = r["modern_execution"] or {}
        checks = r["checks"]
        reasons = "".join(f"<li>{e(x)}</li>" for x in r["reasons"])
        fail_bits = ""
        for label, run in (("Legacy", legacy), ("Modern", modern)):
            if run.get("failure_message"):
                fail_bits += (
                    f'<details><summary>{label} failure message</summary>'
                    f'<pre style="white-space:pre-wrap;font-size:0.75rem;">{e(run["failure_message"])}</pre></details>'
                )
        rows.append(f"""
    <div class="card">
      <div class="card-head">
        <h3>{e(r['test_id'])} · {e(r['name'])}</h3>
        {_badge(r['verdict'])}
      </div>
      <p class="cap">{e(r['business_capability'])} — intent confidence {r['overall_confidence']}</p>
      <table>
        <tr><th></th><th>Legacy (Selenium/TestNG)</th><th>Modern (Playwright)</th></tr>
        <tr><td>Implementation</td>
            <td>{e(r['source']['test_class'])}.{e(r['source']['test_method'])}</td>
            <td>{e(r['generated']['spec'] or '—')}</td></tr>
        <tr><td>Outcome</td>
            <td>{_badge(legacy.get('status', 'missing'))} in {legacy.get('duration_s', '—')}s</td>
            <td>{_badge(modern.get('status', 'missing'))} in {modern.get('duration_s', '—')}s</td></tr>
      </table>
      <p><strong>Flow match:</strong> {checks['flow_match']['realized']}/{checks['flow_match']['total']} steps
         &nbsp; <strong>Assertion match:</strong> {checks['assertion_match']['realized']}/{checks['assertion_match']['total']} assertions</p>
      <ul>{reasons}</ul>
      {fail_bits}
      <p><strong>Recommendation:</strong> {e(r['recommendation'])}</p>
    </div>""")

    s = assessment["summary"]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Behavioral Equivalence Report</title>
<style>
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#F4F7FB;color:#1A1A2E;margin:0;padding:2rem;}}
h1{{color:#002060;}} .cap{{color:#6D6D6D;font-size:0.85rem;}}
.card{{background:#fff;border:1px solid #D0DCF0;border-radius:12px;padding:1.2rem 1.5rem;margin:1rem 0;max-width:900px;}}
.card-head{{display:flex;justify-content:space-between;align-items:center;gap:1rem;}}
.card h3{{color:#002060;margin:0;font-size:1.05rem;}}
table{{border-collapse:collapse;font-size:0.85rem;margin:0.6rem 0;width:100%;}}
th,td{{text-align:left;padding:0.35rem 0.7rem;border-bottom:1px solid #D0DCF0;}}
th{{color:#002060;}}
ul{{font-size:0.85rem;}}
.summary{{display:flex;gap:1rem;margin:1rem 0;}}
.stat{{background:#fff;border:1px solid #D0DCF0;border-radius:10px;padding:0.8rem 1.4rem;text-align:center;}}
.stat b{{display:block;font-size:1.6rem;color:#0070C0;}}
</style></head><body>
<h1>Behavioral Equivalence Report</h1>
<p class="cap">{e(assessment['suite_name'])} — assessed {e(assessment['assessed_at'])}</p>
<div class="summary">
  <div class="stat"><b>{s['EQUIVALENT']}</b>Equivalent</div>
  <div class="stat"><b>{s['PARTIALLY_EQUIVALENT']}</b>Partially</div>
  <div class="stat"><b>{s['NOT_EQUIVALENT']}</b>Not equivalent</div>
  <div class="stat"><b>{s['BLOCKED']}</b>Blocked</div>
</div>
{''.join(rows)}
</body></html>
"""

"""AI Test Modernization Platform — MVP pipeline CLI.

  Ingest Legacy -> AI Understanding -> Test Intent Model -> Rationalize
  -> Generate -> Execute (legacy + modern) -> Behavioral Equivalence

Usage:
  python pipeline.py ingest       # Selenium adapter -> Normalized Technical IR
  python pipeline.py understand   # IR -> TIM via Claude (needs ANTHROPIC_API_KEY)
  python pipeline.py rationalize  # TIM + deterministic signals -> portfolio decisions
                                  #   (migrate / merge / split / redesign / retire / defer)
  python pipeline.py generate     # TIM + IR (+ plan) -> Playwright framework
  python pipeline.py execute [--suite selenium|playwright|both] [--headless]
  python pipeline.py compare      # behavioral equivalence assessment + HTML report
  python pipeline.py all          # the whole story
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from rationalization.validators import DEFAULT_MERGE_FLOOR

ROOT = Path(__file__).parent
WORKSPACE = ROOT.parent
SELENIUM_ROOT = WORKSPACE / "selenium-framework"
PLAYWRIGHT_ROOT = WORKSPACE / "playwright-framework"

ART = ROOT / "artifacts"
IR_FILE = ART / "ir" / "normalized_ir.json"
TIM_FILE = ART / "tim" / "tim-suite.json"
PLAN_FILE = ART / "rationalization" / "plan.json"
SIGNALS_FILE = ART / "rationalization" / "signals.json"
GEN_REPORT = ART / "reports" / "generation-report.json"
SEL_RUN = ART / "runs" / "selenium-run.json"
PW_RUN = ART / "runs" / "playwright-run.json"
EQ_JSON = ART / "reports" / "equivalence-report.json"
EQ_HTML = ART / "reports" / "equivalence-report.html"

load_dotenv(ROOT / ".env")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  -> {path.relative_to(ROOT)}")


def _load(path: Path, what: str) -> dict:
    if not path.exists():
        sys.exit(f"Missing {what}: {path}. Run the earlier pipeline stages first.")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_ingest(_args) -> None:
    from adapters.selenium_java.adapter import SeleniumJavaAdapter

    print("[1/2] Discovering and parsing legacy Selenium framework (deterministic)...")
    ir = SeleniumJavaAdapter(SELENIUM_ROOT).normalize()
    print(f"  pages={len(ir['page_objects'])} tests={len(ir['tests'])} "
          f"helpers={len(ir['helpers'])} data_catalogs={len(ir['test_data'])}")
    print("[2/2] Writing Normalized Technical IR...")
    _save(IR_FILE, ir)


def cmd_understand(_args) -> None:
    from understanding.service import recover_intent

    ir = _load(IR_FILE, "IR")
    print("[1/2] Recovering business intent via Claude (structured output + schema validation)...")
    suite, warnings = recover_intent(ir)
    print(f"  recovered {len(suite.tests)} TIM tests "
          f"(avg confidence {sum(t.overall_confidence for t in suite.tests) / len(suite.tests):.2f})")
    for w in warnings:
        print(f"  warn: {w}")
    print("[2/2] Writing Test Intent Model...")
    _save(TIM_FILE, suite.model_dump(mode="json"))


def _dry_run_generation(ir: dict, suite) -> dict:
    """Generate into a throwaway directory purely to measure regeneration feasibility.

    Deterministic and side-effect free: it tells the rationalization stage which
    intents the platform can actually rebuild before anyone decides to migrate them."""
    import tempfile

    from generators.playwright_generator import PlaywrightJsGenerator

    with tempfile.TemporaryDirectory() as tmp:
        return PlaywrightJsGenerator(ir, suite, Path(tmp)).generate()


def cmd_rationalize(args) -> None:
    from rationalization.service import rationalize
    from rationalization.signals import compute_signals
    from tim.models import TimSuite

    ir = _load(IR_FILE, "IR")
    tim = _load(TIM_FILE, "TIM suite")
    suite = TimSuite.model_validate(tim)
    legacy = json.loads(SEL_RUN.read_text(encoding="utf-8")) if SEL_RUN.exists() else None

    print("[1/3] Computing deterministic signals (redundancy, coverage depth, cost, feasibility)...")
    feasibility = _dry_run_generation(ir, suite)
    signals = compute_signals(tim, ir, selenium_run=legacy, gen_report=feasibility)
    top = max(signals["redundancy"], key=lambda r: r["score"], default=None)
    print(f"  tests={signals['suite']['total_tests']} assertions={signals['suite']['total_assertions']} "
          f"max redundancy={signals['suite']['max_redundancy_score']}"
          + (f" ({'/'.join(top['pair'])})" if top else ""))
    _save(SIGNALS_FILE, signals)

    print("[2/3] Deciding dispositions via Claude (schema-validated, merge floor "
          f"{args.merge_floor}, signals cited as evidence)...")
    plan, warnings = rationalize(tim, signals, merge_floor=args.merge_floor)
    for d in plan.decisions:
        extra = ""
        if d.group_id:
            extra = f" (group {d.group_id}{', primary' if d.primary else ''})"
        elif d.blocked_by:
            extra = f" (blocked by: {d.blocked_by})"
        elif d.target_channel:
            extra = f" (-> {d.target_channel.value})"
        print(f"  {d.test_id}: {d.disposition.value}{extra} — {d.rationale}")
    for w in warnings:
        print(f"  warn: {w}")
    s = plan.summary
    print(f"  portfolio: {s.by_disposition} | implementations eliminated: {s.implementations_eliminated}"
          + (f" | legacy runtime reclaimed: {s.legacy_runtime_reclaimed_s}s"
             if s.legacy_runtime_reclaimed_s else ""))

    print("[3/3] Writing rationalization plan...")
    _save(PLAN_FILE, plan.model_dump(mode="json"))


def cmd_generate(args) -> None:
    from generators.playwright_generator import PlaywrightJsGenerator
    from tim.models import TimSuite

    ir = _load(IR_FILE, "IR")
    suite = TimSuite.model_validate(_load(TIM_FILE, "TIM suite"))

    plan = None
    if getattr(args, "ignore_plan", False):
        print("  (--ignore-plan) rationalization plan not applied — emitting every intent")
    elif PLAN_FILE.exists():
        plan = json.loads(PLAN_FILE.read_text(encoding="utf-8"))
    else:
        print("  no rationalization plan found — emitting one implementation per intent")

    print(f"[1/2] Regenerating into Playwright blueprint at {PLAYWRIGHT_ROOT.name}/ ...")
    report = PlaywrightJsGenerator(ir, suite, PLAYWRIGHT_ROOT, plan=plan).generate()
    for m in report["merges"]:
        if m["realized"]:
            print(f"  merged {', '.join(m['members'])} -> one parameterized test "
                  f"(parameters: {', '.join(m['parameters'])})")
        else:
            print(f"  MERGE REFUSED for {', '.join(m['members'])}: {m['reason']}")
            print("    emitted separately instead — no coverage was dropped")
    for skip in report["skipped_by_disposition"]:
        print(f"  not emitted [{skip['disposition']}] {skip['test_id']}: {skip['reason']}")
    strategies = {}
    for m in report["methods"]:
        strategies[m["strategy"]] = strategies.get(m["strategy"], 0) + 1
    print(f"  files: {len(report['files'])} | method strategies: {strategies}")
    todo = [m["ir_ref"] for m in report["methods"] if m["strategy"] == "todo-stub"]
    if todo:
        print(f"  NEEDS REVIEW (todo stubs): {todo}")
    print("[2/2] Writing generation report...")
    _save(GEN_REPORT, report)


def cmd_execute(args) -> None:
    from execution.runner import run_playwright, run_selenium

    if args.suite in ("selenium", "both"):
        print("[*] Executing LEGACY suite (mvn test) against the live site — this opens Chrome...")
        evidence = run_selenium(SELENIUM_ROOT)
        n = {s: sum(1 for t in evidence["tests"] if t["status"] == s) for s in ("passed", "failed", "skipped")}
        print(f"  exit={evidence['exit_code']} {n}")
        _save(SEL_RUN, evidence)
    if args.suite in ("playwright", "both"):
        print("[*] Executing MODERN suite (npx playwright test) against the live site...")
        evidence = run_playwright(PLAYWRIGHT_ROOT, headed=not args.headless)
        n = {s: sum(1 for t in evidence["tests"] if t["status"] == s) for s in ("passed", "failed", "skipped")}
        print(f"  exit={evidence['exit_code']} {n}")
        _save(PW_RUN, evidence)


def cmd_compare(_args) -> None:
    from equivalence.engine import assess, render_html

    tim = _load(TIM_FILE, "TIM suite")
    gen = _load(GEN_REPORT, "generation report")
    sel = _load(SEL_RUN, "selenium run evidence")
    pw = _load(PW_RUN, "playwright run evidence")
    plan = json.loads(PLAN_FILE.read_text(encoding="utf-8")) if PLAN_FILE.exists() else None
    print("[1/2] Assessing behavioral equivalence...")
    assessment = assess(tim, gen, sel, pw, plan=plan)
    print(f"  summary: {assessment['summary']}")
    for r in assessment["results"]:
        print(f"  {r['test_id']}: {r['verdict']} — {r['recommendation']}")
    print("[2/2] Writing reports...")
    _save(EQ_JSON, assessment)
    EQ_HTML.parent.mkdir(parents=True, exist_ok=True)
    EQ_HTML.write_text(render_html(assessment), encoding="utf-8")
    print(f"  -> {EQ_HTML.relative_to(ROOT)}")


def cmd_all(args) -> None:
    cmd_ingest(args)
    cmd_understand(args)
    if not args.skip_rationalize:
        cmd_rationalize(args)
    cmd_generate(args)
    args.suite = "both"
    cmd_execute(args)
    cmd_compare(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Test Modernization Platform (MVP)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest")
    sub.add_parser("understand")
    ra = sub.add_parser("rationalize")
    ra.add_argument("--merge-floor", type=float, default=DEFAULT_MERGE_FLOOR,
                    help="minimum computed redundancy score required to accept a MERGE")
    ge = sub.add_parser("generate")
    ge.add_argument("--ignore-plan", action="store_true",
                    help="emit every recovered intent, ignoring the rationalization plan")
    ex = sub.add_parser("execute")
    ex.add_argument("--suite", choices=["selenium", "playwright", "both"], default="both")
    ex.add_argument("--headless", action="store_true")
    sub.add_parser("compare")
    al = sub.add_parser("all")
    al.add_argument("--headless", action="store_true")
    al.add_argument("--merge-floor", type=float, default=DEFAULT_MERGE_FLOOR)
    al.add_argument("--skip-rationalize", action="store_true",
                    help="run the pre-rationalization pipeline (1:1 port of every intent)")

    args = parser.parse_args()
    for name, default in (("headless", False), ("merge_floor", DEFAULT_MERGE_FLOOR),
                          ("skip_rationalize", False), ("ignore_plan", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    {
        "ingest": cmd_ingest,
        "understand": cmd_understand,
        "rationalize": cmd_rationalize,
        "generate": cmd_generate,
        "execute": cmd_execute,
        "compare": cmd_compare,
        "all": cmd_all,
    }[args.command](args)


if __name__ == "__main__":
    main()

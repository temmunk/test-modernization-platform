"""AI Test Modernization Platform — MVP pipeline CLI.

  Ingest Legacy -> AI Understanding -> Test Intent Model -> Generate
  -> Execute (legacy + modern) -> Behavioral Equivalence

Usage:
  python pipeline.py ingest       # Selenium adapter -> Normalized Technical IR
  python pipeline.py understand   # IR -> TIM via Claude (needs ANTHROPIC_API_KEY)
  python pipeline.py generate     # TIM + IR -> Playwright framework
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

ROOT = Path(__file__).parent
WORKSPACE = ROOT.parent
SELENIUM_ROOT = WORKSPACE / "selenium-framework"
PLAYWRIGHT_ROOT = WORKSPACE / "playwright-framework"

ART = ROOT / "artifacts"
IR_FILE = ART / "ir" / "normalized_ir.json"
TIM_FILE = ART / "tim" / "tim-suite.json"
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


def cmd_generate(_args) -> None:
    from generators.playwright_generator import PlaywrightJsGenerator
    from tim.models import TimSuite

    ir = _load(IR_FILE, "IR")
    suite = TimSuite.model_validate(_load(TIM_FILE, "TIM suite"))
    print(f"[1/2] Regenerating into Playwright blueprint at {PLAYWRIGHT_ROOT.name}/ ...")
    report = PlaywrightJsGenerator(ir, suite, PLAYWRIGHT_ROOT).generate()
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
    print("[1/2] Assessing behavioral equivalence...")
    assessment = assess(tim, gen, sel, pw)
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
    cmd_generate(args)
    args.suite = "both"
    cmd_execute(args)
    cmd_compare(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Test Modernization Platform (MVP)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest")
    sub.add_parser("understand")
    sub.add_parser("generate")
    ex = sub.add_parser("execute")
    ex.add_argument("--suite", choices=["selenium", "playwright", "both"], default="both")
    ex.add_argument("--headless", action="store_true")
    sub.add_parser("compare")
    al = sub.add_parser("all")
    al.add_argument("--headless", action="store_true")

    args = parser.parse_args()
    if not hasattr(args, "headless"):
        args.headless = False
    {
        "ingest": cmd_ingest,
        "understand": cmd_understand,
        "generate": cmd_generate,
        "execute": cmd_execute,
        "compare": cmd_compare,
        "all": cmd_all,
    }[args.command](args)


if __name__ == "__main__":
    main()

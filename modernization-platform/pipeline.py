"""AI Test Modernization Platform — MVP pipeline CLI (batch-aware).

  Ingest Legacy -> AI Understanding -> Test Intent Model -> Generate
  -> Execute (legacy + modern) -> Behavioral Equivalence

The estate is partitioned into batches (one per test class, with its
dependency closure) around a shared core (page objects, helpers, config,
oracle data). Every stage auto-discovers all batches from the manifest by
default; pass --batch <id> to (re)process just one without touching the rest.
Each batch owns a reserved TIM id range (batch seq 1 -> TIM-101.., seq 2 ->
TIM-201..) so regenerating one batch never renumbers another.

Usage:
  python pipeline.py ingest      [--batch ID]   # -> shared-core + ir/batches/* + manifest
  python pipeline.py batches                    # list manifest status
  python pipeline.py understand  [--batch ID]   # one LLM call per batch -> tim/batches/* + merged tim-suite.json
  python pipeline.py generate    [--batch ID]   # shared assets + per-batch specs
  python pipeline.py execute     [--batch ID] [--suite selenium|playwright|both] [--headless]
  python pipeline.py compare     [--batch ID]
  python pipeline.py all         [--headless]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
WORKSPACE = ROOT.parent
SELENIUM_ROOT = WORKSPACE / "selenium-framework"
PLAYWRIGHT_ROOT = WORKSPACE / "playwright-framework"

ART = ROOT / "artifacts"
SHARED_CORE = ART / "ir" / "shared-core.json"
IR_BATCH_DIR = ART / "ir" / "batches"
MANIFEST = ART / "ir" / "manifest.json"
TIM_BATCH_DIR = ART / "tim" / "batches"
TIM_FILE = ART / "tim" / "tim-suite.json"
GEN_REPORT = ART / "reports" / "generation-report.json"
SEL_RUN = ART / "runs" / "selenium-run.json"
PW_RUN = ART / "runs" / "playwright-run.json"
EQ_JSON = ART / "reports" / "equivalence-report.json"
EQ_HTML = ART / "reports" / "equivalence-report.html"

MAX_TESTS_PER_BATCH = 99  # TIM range width per batch seq

load_dotenv(ROOT / ".env")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  -> {path.relative_to(ROOT)}")


def _load(path: Path, what: str) -> dict:
    if not path.exists():
        sys.exit(f"Missing {what}: {path}. Run the earlier pipeline stages first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_optional(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ------------------------------------------------------------- batch helpers
def _select_batches(manifest: dict, batch_id: str | None) -> list[dict]:
    batches = manifest["batches"]
    if batch_id is None:
        return batches
    hit = [b for b in batches if b["id"] == batch_id]
    if not hit:
        sys.exit(f"Unknown batch '{batch_id}'. Known: {', '.join(b['id'] for b in batches)}")
    return hit


def _tim_ids_for(entry: dict) -> list[str]:
    base = entry["seq"] * 100
    return [f"TIM-{base + i:03d}" for i in range(1, len(entry["tests"]) + 1)]


def _batch_ir(shared: dict, batch: dict) -> dict:
    """Self-consistent IR for one batch: its tests + only the shared assets in
    its dependency closure (what the LLM sees, and what refs validate against)."""
    dep_pages = set(batch["dependencies"]["page_objects"])
    dep_helpers = set(batch["dependencies"]["helpers"])
    return {
        "ir_version": shared["ir_version"],
        "source_framework": shared["source_framework"],
        "config": shared["config"],
        "test_data": shared["test_data"],
        "page_objects": [p for p in shared["page_objects"] if p["id"] in dep_pages],
        "helpers": [h for h in shared["helpers"] if h["id"] in dep_helpers],
        "tests": batch["tests"],
        "support_classes": shared["support_classes"],
    }


def _full_ir(shared: dict, batches: list[dict]) -> dict:
    """Shared core + the given batches' tests, as one classic IR (for the generator)."""
    ir = {k: shared[k] for k in (
        "ir_version", "source_framework", "config", "test_data",
        "page_objects", "helpers", "support_classes",
    )}
    ir["tests"] = [t for b in batches for t in b["tests"]]
    return ir


# ------------------------------------------------------------------- ingest
def cmd_ingest(args) -> None:
    from adapters.selenium_java.adapter import SeleniumJavaAdapter

    print("[1/3] Discovering and parsing legacy Selenium framework (deterministic)...")
    shared, batches = SeleniumJavaAdapter(SELENIUM_ROOT).normalize_batched()
    if args.batch:
        batches = [b for b in batches if b["id"] == args.batch]
        if not batches:
            sys.exit(f"No test class in the source maps to batch '{args.batch}'")

    print("[2/3] Assigning batch sequence numbers (stable across re-ingests)...")
    old = _load_optional(MANIFEST) or {"batches": []}
    seq_by_id = {b["id"]: b["seq"] for b in old["batches"]}
    next_seq = max(seq_by_id.values(), default=0) + 1
    entries = {b["id"]: b for b in old["batches"]}

    for b in batches:
        if len(b["tests"]) > MAX_TESTS_PER_BATCH:
            sys.exit(f"Batch '{b['id']}' has {len(b['tests'])} tests; max {MAX_TESTS_PER_BATCH} per batch")
        seq = seq_by_id.get(b["id"])
        if seq is None:
            seq, next_seq = next_seq, next_seq + 1
        entry = {
            "id": b["id"],
            "seq": seq,
            "test_class": b["test_class"],
            "file": f"batches/{b['id']}.json",
            "tests": [t["id"] for t in b["tests"]],
            "dependencies": b["dependencies"],
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        entry["tim_ids"] = _tim_ids_for(entry)
        entries[b["id"]] = entry
        _save(IR_BATCH_DIR / f"{b['id']}.json", b)
        print(f"  batch {b['id']} (seq {seq}): {len(b['tests'])} tests, "
              f"deps: {len(b['dependencies']['page_objects'])} pages, "
              f"{len(b['dependencies']['helpers'])} helpers -> {entry['tim_ids'][0]}..")

    print("[3/3] Writing shared core + manifest...")
    _save(SHARED_CORE, shared)
    manifest = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "shared_core": "shared-core.json",
        "batches": sorted(entries.values(), key=lambda e: e["seq"]),
    }
    _save(MANIFEST, manifest)


def cmd_batches(_args) -> None:
    manifest = _load(MANIFEST, "manifest")
    tim_merged = _load_optional(TIM_FILE)
    tim_by_id = {t["test_id"]: t for t in (tim_merged or {}).get("tests", [])}
    for b in manifest["batches"]:
        understood = (TIM_BATCH_DIR / f"{b['id']}.json").exists()
        statuses = {tim_by_id[i]["provenance"]["review_status"]
                    for i in b["tim_ids"] if i in tim_by_id}
        review = "/".join(sorted(statuses)) if statuses else "—"
        print(f"  {b['id']}  seq={b['seq']}  class={b['test_class']}  tests={len(b['tests'])}  "
              f"tim={b['tim_ids'][0]}..{b['tim_ids'][-1]}  understood={'yes' if understood else 'no'}  review={review}")


# --------------------------------------------------------------- understand
def _merge_tim(manifest: dict) -> dict:
    """Merged tim-suite.json = all batch TIMs in seq order. Tests from batches
    NOT re-understood keep their entries from the existing merged file, so
    workbench review decisions survive partial re-runs."""
    existing = _load_optional(TIM_FILE)
    existing_tests = {t["test_id"]: t for t in (existing or {}).get("tests", [])}

    tests, meta = [], None
    for b in manifest["batches"]:
        batch_tim = _load_optional(TIM_BATCH_DIR / f"{b['id']}.json")
        if batch_tim:
            meta = meta or batch_tim
            for t in batch_tim["tests"]:
                # keep reviewer decisions already recorded on the merged copy
                prev = existing_tests.get(t["test_id"])
                if prev and prev["provenance"].get("extracted_at") == t["provenance"].get("extracted_at"):
                    t = prev
                tests.append(t)
        else:
            tests.extend(existing_tests[i] for i in b["tim_ids"] if i in existing_tests)

    if meta is None and existing is None:
        sys.exit("No TIM batches exist yet — run `understand` first.")
    base = meta or existing
    return {
        "tim_version": base["tim_version"],
        "suite_name": "MyPrime Guest Flow Suite",
        "business_domain": base["business_domain"],
        "application_under_test": base["application_under_test"],
        "tests": tests,
    }


def cmd_understand(args) -> None:
    from understanding.service import recover_intent

    manifest = _load(MANIFEST, "manifest")
    shared = _load(SHARED_CORE, "shared core IR")
    selected = _select_batches(manifest, args.batch)

    for n, entry in enumerate(selected, start=1):
        batch = _load(IR_BATCH_DIR / f"{entry['id']}.json", f"batch IR '{entry['id']}'")
        ir = _batch_ir(shared, batch)
        print(f"[{n}/{len(selected)}] Recovering intent for batch '{entry['id']}' "
              f"({len(batch['tests'])} tests, ids {entry['tim_ids'][0]}..)...")
        suite, warnings = recover_intent(ir, expected_test_ids=entry["tim_ids"])
        avg = sum(t.overall_confidence for t in suite.tests) / len(suite.tests)
        print(f"  recovered {len(suite.tests)} TIM tests (avg confidence {avg:.2f})")
        for w in warnings:
            print(f"  warn: {w}")
        _save(TIM_BATCH_DIR / f"{entry['id']}.json", suite.model_dump(mode="json"))

    print("[merge] Rebuilding merged TIM suite...")
    _save(TIM_FILE, _merge_tim(manifest))


# ----------------------------------------------------------------- generate
def cmd_generate(args) -> None:
    from generators.playwright_generator import PlaywrightJsGenerator
    from tim.models import TimSuite

    manifest = _load(MANIFEST, "manifest")
    shared = _load(SHARED_CORE, "shared core IR")
    selected = _select_batches(manifest, args.batch)
    merged_tim = _load(TIM_FILE, "merged TIM suite")

    selected_tim_ids = {i for e in selected for i in e["tim_ids"]}
    suite_data = dict(merged_tim)
    suite_data["tests"] = [t for t in merged_tim["tests"] if t["test_id"] in selected_tim_ids]
    if not suite_data["tests"]:
        sys.exit("Selected batches have no TIM tests yet — run `understand` first.")
    suite = TimSuite.model_validate(suite_data)

    batches = [_load(IR_BATCH_DIR / f"{e['id']}.json", f"batch IR '{e['id']}'") for e in selected]
    ir = _full_ir(shared, batches)

    scope = "all batches" if args.batch is None else f"batch '{args.batch}'"
    print(f"[1/2] Regenerating shared assets + specs for {scope} -> {PLAYWRIGHT_ROOT.name}/ ...")
    report = PlaywrightJsGenerator(ir, suite, PLAYWRIGHT_ROOT).generate()
    strategies = {}
    for m in report["methods"]:
        strategies[m["strategy"]] = strategies.get(m["strategy"], 0) + 1
    print(f"  files: {len(report['files'])} | method strategies: {strategies}")
    todo = [m["ir_ref"] for m in report["methods"] if m["strategy"] == "todo-stub"]
    if todo:
        print(f"  NEEDS REVIEW (todo stubs): {todo}")

    print("[2/2] Merging generation report...")
    old = _load_optional(GEN_REPORT)
    if old and args.batch is not None:
        # replace coverage for the regenerated batch; keep other batches' entries
        keep = [c for c in old.get("tim_coverage", []) if c["test_id"] not in selected_tim_ids]
        report["tim_coverage"] = keep + report["tim_coverage"]
        seen = set()
        methods = []
        for m in report["methods"] + old.get("methods", []):
            key = (m["ir_ref"], m["target"])
            if key not in seen:
                seen.add(key)
                methods.append(m)
        report["methods"] = methods
        report["files"] = sorted(set(report["files"]) | set(old.get("files", [])))
    _save(GEN_REPORT, report)


# ------------------------------------------------------------------ execute
def _merge_run(path: Path, new_run: dict, key_fields: tuple) -> dict:
    """Batch-scoped runs replace only their own tests in the stored evidence."""
    old = _load_optional(path)
    if old:
        def key(t):
            return tuple(t.get(f) for f in key_fields)
        new_keys = {key(t) for t in new_run["tests"]}
        new_run["tests"] = [t for t in old["tests"] if key(t) not in new_keys] + new_run["tests"]
    return new_run


def cmd_execute(args) -> None:
    from execution.runner import run_playwright, run_selenium

    manifest = _load(MANIFEST, "manifest")
    selected = _select_batches(manifest, args.batch)

    if args.suite in ("selenium", "both"):
        for entry in selected if args.batch else [None]:
            test_class = entry["test_class"] if entry else None
            label = f" (class {test_class})" if test_class else ""
            print(f"[*] Executing LEGACY suite{label} against the live site — this opens Chrome...")
            evidence = run_selenium(SELENIUM_ROOT, test_class=test_class)
            n = {s: sum(1 for t in evidence["tests"] if t["status"] == s) for s in ("passed", "failed", "skipped")}
            print(f"  exit={evidence['exit_code']} {n}")
            if args.batch:
                evidence = _merge_run(SEL_RUN, evidence, ("test_class", "test_method"))
            _save(SEL_RUN, evidence)

    if args.suite in ("playwright", "both"):
        gen = _load_optional(GEN_REPORT) or {}
        cov_by_id = {c["test_id"]: c for c in gen.get("tim_coverage", [])}
        for entry in selected if args.batch else [None]:
            spec = None
            if entry:
                specs = {cov_by_id[i]["spec"] for i in entry["tim_ids"] if i in cov_by_id}
                spec = specs.pop() if len(specs) == 1 else None
            label = f" (spec {spec})" if spec else ""
            print(f"[*] Executing MODERN suite{label} against the live site...")
            evidence = run_playwright(PLAYWRIGHT_ROOT, headed=not args.headless, spec=spec)
            n = {s: sum(1 for t in evidence["tests"] if t["status"] == s) for s in ("passed", "failed", "skipped")}
            print(f"  exit={evidence['exit_code']} {n}")
            if args.batch:
                evidence = _merge_run(PW_RUN, evidence, ("tim_id",))
            _save(PW_RUN, evidence)


# ------------------------------------------------------------------ compare
def cmd_compare(args) -> None:
    from equivalence.engine import assess, render_html

    manifest = _load(MANIFEST, "manifest")
    selected = _select_batches(manifest, args.batch)
    selected_tim_ids = {i for e in selected for i in e["tim_ids"]}

    tim = _load(TIM_FILE, "merged TIM suite")
    if args.batch:
        tim = dict(tim)
        tim["tests"] = [t for t in tim["tests"] if t["test_id"] in selected_tim_ids]
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
    args.batch = None
    cmd_ingest(args)
    cmd_understand(args)
    cmd_generate(args)
    args.suite = "both"
    cmd_execute(args)
    cmd_compare(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Test Modernization Platform (MVP, batch-aware)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name):
        p = sub.add_parser(name)
        p.add_argument("--batch", default=None, help="process only this batch id (see `batches`)")
        return p

    add("ingest")
    sub.add_parser("batches")
    add("understand")
    add("generate")
    ex = add("execute")
    ex.add_argument("--suite", choices=["selenium", "playwright", "both"], default="both")
    ex.add_argument("--headless", action="store_true")
    add("compare")
    al = sub.add_parser("all")
    al.add_argument("--headless", action="store_true")

    args = parser.parse_args()
    if not hasattr(args, "headless"):
        args.headless = False
    if not hasattr(args, "batch"):
        args.batch = None
    {
        "ingest": cmd_ingest,
        "batches": cmd_batches,
        "understand": cmd_understand,
        "generate": cmd_generate,
        "execute": cmd_execute,
        "compare": cmd_compare,
        "all": cmd_all,
    }[args.command](args)


if __name__ == "__main__":
    main()

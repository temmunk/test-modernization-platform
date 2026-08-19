"""Execution layer: runs the legacy (Selenium/Maven) and modern (Playwright)
suites against the live application and captures normalized execution evidence
for the behavioral-equivalence engine."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def _run(cmd: list[str], cwd: Path, timeout: int = 3600) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, shell=True
    )
    return proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")


def run_selenium(root: Path, test_class: str | None = None) -> dict:
    """test_class limits the run to one batch's class (surefire -Dtest filter)."""
    root = Path(root)
    reports = root / "target" / "surefire-reports"
    if reports.exists():
        shutil.rmtree(reports, ignore_errors=True)

    cmd = ["mvn", "-B", "test"]
    if test_class:
        cmd.append(f"-Dtest={test_class}")

    started = datetime.now(timezone.utc)
    code, output = _run(cmd, cwd=root)
    finished = datetime.now(timezone.utc)

    tests = []
    seen = set()
    if reports.exists():
        for xml_file in sorted(reports.rglob("TEST-*.xml")):
            try:
                tree = ET.parse(xml_file)
            except ET.ParseError:
                continue
            for tc in tree.getroot().iter("testcase"):
                key = (tc.get("classname"), tc.get("name"))
                if key in seen:
                    continue
                seen.add(key)
                failure = tc.find("failure")
                error = tc.find("error")
                skipped = tc.find("skipped")
                status = "passed"
                message = None
                if failure is not None or error is not None:
                    status = "failed"
                    node = failure if failure is not None else error
                    message = (node.get("message") or "").strip()[:800]
                elif skipped is not None:
                    status = "skipped"
                tests.append(
                    {
                        "test_class": (tc.get("classname") or "").split(".")[-1],
                        "test_method": tc.get("name"),
                        "status": status,
                        "duration_s": float(tc.get("time") or 0),
                        "failure_message": message,
                    }
                )

    return {
        "framework": "selenium-java-testng",
        "command": " ".join(cmd),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "exit_code": code,
        "tests": tests,
        "log_tail": output[-4000:],
    }


def run_playwright(root: Path, headed: bool = True, project: str = "chromium",
                   spec: str | None = None) -> dict:
    """spec limits the run to one batch's generated spec file (path or filename)."""
    root = Path(root)
    results_file = root / "test-results" / "results.json"
    if results_file.exists():
        results_file.unlink()

    cmd = ["npx", "playwright", "test", f"--project={project}"]
    if spec:
        cmd.append(spec)
    if headed:
        cmd.append("--headed")

    started = datetime.now(timezone.utc)
    code, output = _run(cmd, cwd=root)
    finished = datetime.now(timezone.utc)

    tests = []
    if results_file.exists():
        data = json.loads(results_file.read_text(encoding="utf-8"))

        def walk(suite):
            for spec in suite.get("specs", []):
                for t in spec.get("tests", []):
                    results = t.get("results", [])
                    last = results[-1] if results else {}
                    status = "passed" if t.get("status") == "expected" else (
                        "skipped" if t.get("status") == "skipped" else "failed"
                    )
                    tim = re.search(r"TIM-\d+", spec.get("title", ""))
                    tests.append(
                        {
                            "title": spec.get("title"),
                            "tim_id": tim.group(0) if tim else None,
                            "status": status,
                            "duration_s": round(sum(r.get("duration", 0) for r in results) / 1000, 2),
                            "failure_message": ((last.get("error") or {}).get("message") or "")[:800] or None,
                            "annotations": t.get("annotations", []),
                        }
                    )
            for child in suite.get("suites", []):
                walk(child)

        for s in data.get("suites", []):
            walk(s)

    return {
        "framework": "playwright-js",
        "command": " ".join(cmd),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "exit_code": code,
        "tests": tests,
        "log_tail": output[-4000:],
    }

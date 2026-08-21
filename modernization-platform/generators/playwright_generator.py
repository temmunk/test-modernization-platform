"""Playwright (JavaScript) generation engine.

Regeneration, not mechanical translation:
 - Page objects are emitted deterministically from the IR. Straight-line
   methods go through a statement translator; timing quirks the legacy code
   encoded (AJAX-populated selects, nag dismissal, autocomplete retries,
   scoped card reads) are regenerated through blueprint patterns.
 - Specs are driven by the TIM: business names, step/assertion intent
   comments, confidence, and source traceability annotations — while call
   mechanics (argument order, statement order) come from the IR so nothing
   depends on the LLM getting technical details right.
Anything not translatable is emitted as an explicit TODO stub and flagged in
the generation report for human review — never silently guessed.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from generators.merge_param import parameterize
from rationalization.models import index_plan
from tim.models import TimSuite

BLUEPRINT_ID = "playwright-js-v1"
BLUEPRINT_DIR = Path(__file__).parent.parent / "blueprints" / "playwright_js"


# ------------------------------------------------------------------ naming
def decap(name: str) -> str:
    return name[0].lower() + name[1:] if name else name


def const_to_camel(name: str) -> str:
    parts = name.lower().split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def class_to_kebab(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def js_str(value: str) -> str:
    return json.dumps(value)


def locator_selector(strategy: str, value: str) -> str:
    if strategy == "id":
        return f"#{value}"
    return value  # cssSelector and xpath (Playwright auto-detects // and ..)


def split_top_level(s: str, sep: str = ",") -> List[str]:
    parts, depth, in_str, cur, i = [], 0, False, [], 0
    while i < len(s):
        c = s[i]
        if in_str:
            cur.append(c)
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif depth == 0 and c == sep:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts]


def split_statements(body: str) -> List[str]:
    """Top-level statements; if-blocks kept as single units."""
    stmts, i, n = [], 0, len(body)
    while i < n:
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if body.startswith("//", i):
            i = body.find("\n", i)
            i = n if i == -1 else i
            continue
        if body.startswith("/*", i):
            i = body.find("*/", i) + 2
            continue
        if re.match(r"if\s*\(", body[i:]):
            # capture through matching close brace
            open_brace = body.index("{", i)
            depth, j = 0, open_brace
            while j < n:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            stmts.append(body[i : j + 1])
            i = j + 1
            continue
        # regular statement to top-level ';'
        depth, in_str, j = 0, False, i
        while j < n:
            c = body[j]
            if in_str:
                if c == "\\":
                    j += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            j += 1
        stmts.append(body[i:j].strip())
        i = j + 1
    return [s for s in stmts if s]


def parse_chain(expr: str):
    """'new X(driver).a().b(1,2)' | 'var.a().b()' -> (root, [(method, args_str), ...])"""
    expr = re.sub(r"\s+", " ", expr).strip()
    m = re.match(r"new\s+(\w+)\s*\(\s*driver\s*\)", expr)
    if m:
        root, rest = ("new", m.group(1)), expr[m.end() :]
    else:
        m = re.match(r"(\w+)(?=\.|\()", expr)
        if not m:
            return None
        if expr[m.end()] == "(":
            depth, j = 0, m.end()
            while j < len(expr):
                if expr[j] == "(":
                    depth += 1
                elif expr[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            root, rest = ("call", m.group(1), expr[m.end() + 1 : j]), expr[j + 1 :]
        else:
            root, rest = ("var", m.group(1)), expr[m.end() :]
    calls = []
    i = 0
    while i < len(rest):
        m = re.match(r"\s*\.\s*(\w+)\s*\(", rest[i:])
        if not m:
            break
        name = m.group(1)
        start = i + m.end() - 1
        depth, j = 0, start
        while j < len(rest):
            if rest[j] == "(":
                depth += 1
            elif rest[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        calls.append((name, rest[start + 1 : j]))
        i = j + 1
    return root, calls


class PlaywrightJsGenerator:
    def __init__(self, ir: dict, suite: TimSuite, target_root: Path, plan: Optional[dict] = None):
        self.ir = ir
        self.suite = suite
        self.target = Path(target_root)
        self.plan = plan
        # Without a plan the generator emits one implementation per recovered intent.
        # With one, it emits what the portfolio decision says to emit — and records
        # everything it did not emit, with the reason. Nothing is dropped silently.
        self.plan_index = index_plan(plan) if plan else None
        self.pages = {p["class_name"]: p for p in ir["page_objects"]}
        self.report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "blueprint": BLUEPRINT_ID,
            "target_root": self.target.name,
            "plan_applied": bool(plan),
            "dispositions": {d["test_id"]: d["disposition"] for d in (plan or {}).get("decisions", [])},
            "files": [],
            "methods": [],
            "tim_coverage": [],
            "skipped_by_disposition": [],
            "merges": [],
        }
        # method return types per class
        self.returns: Dict[str, Dict[str, Optional[str]]] = {}
        for cname, p in self.pages.items():
            self.returns[cname] = {}
            for m in p["methods"]:
                r = m["returns"]
                self.returns[cname][m["name"]] = r if r in self.pages or r == cname else None
        self.rename = {"findResultCard": "resultCard"}

    # ------------------------------------------------------------ helpers
    def _write(self, rel: str, content: str):
        path = self.target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        self.report["files"].append(rel)

    def _method_return(self, cls: str, method: str) -> Optional[str]:
        return self.returns.get(cls, {}).get(method)

    # ---------------------------------------------------- expression level
    def tr_expr(self, expr: str, env: Dict[str, str]) -> str:
        expr = re.sub(r"\s+", " ", expr).strip()
        expr = re.sub(r"ConfigReader\.(\w+)\(\)", r"config.\1", expr)

        def getter(m):
            var, verb, prop = m.group(1), m.group(2), m.group(3)
            if env.get(var) == "data":
                return f"{var}.{decap(prop)}"
            return m.group(0)

        expr = re.sub(r"\b(\w+)\.(get|is)([A-Z]\w*)\(\)", getter, expr)

        m = re.fullmatch(r"(\w+)\.(\w+)\((.*)\)", expr, re.DOTALL)
        if m and env.get(m.group(1)) in self.pages:
            args = [self.tr_expr(a, env) for a in split_top_level(m.group(3))]
            name = self.rename.get(m.group(2), m.group(2))
            return f"await {m.group(1)}.{name}({', '.join(args)})"
        return expr

    # ------------------------------------------------- flow/spec statements
    def emit_chain(self, expr: str, env: Dict[str, str], lines: List[str],
                   target_var: Optional[str] = None, fixture_var: Optional[str] = None,
                   is_return: bool = False) -> Optional[str]:
        parsed = parse_chain(expr)
        if not parsed:
            return None

        def unique(name: str) -> str:
            if name not in env:
                return name
            n = 2
            while f"{name}{n}" in env:
                n += 1
            return f"{name}{n}"

        root, calls = parsed
        if root[0] == "new":
            cls = root[1]
            var = target_var or unique(decap(cls))
            lines.append(f"const {var} = new {cls}(page);")
            env[var] = cls
            current, cur_type = var, cls
            target_assigned = target_var is not None
        elif root[0] == "var":
            current, cur_type = root[1], env.get(root[1])
            target_assigned = False
        else:  # helper call -> fixture
            if not fixture_var:
                return None
            current, cur_type = fixture_var, "MedicineSearchPage"
            helper = next((h for h in self.ir["helpers"] if h["name"] == root[1]), None)
            if helper and helper["returns"] in self.pages:
                cur_type = helper["returns"]
            if target_var:
                lines.append(f"const {target_var} = {fixture_var};")
                env[target_var] = cur_type
                current = target_var
                target_assigned = True
            else:
                target_assigned = False

        for idx, (name, args_str) in enumerate(calls):
            args = [self.tr_expr(a, env) for a in split_top_level(args_str)] if args_str.strip() else []
            call = f"{current}.{self.rename.get(name, name)}({', '.join(args)})"
            ret = self._method_return(cur_type, name) if cur_type else None
            last = idx == len(calls) - 1
            if ret and ret != cur_type:
                var = target_var if (last and target_var and not target_assigned) else unique(decap(ret))
                lines.append(f"const {var} = await {call};")
                env[var] = ret
                current, cur_type = var, ret
                if last and target_var and var == target_var:
                    target_assigned = True
            else:
                lines.append(f"await {call};")
        if is_return:
            lines.append(f"return {current};")
        if target_var and not target_assigned and root[0] != "new":
            pass
        return current

    def tr_flow_stmt(self, stmt: str, env: Dict[str, str], lines: List[str],
                     fixture_var: Optional[str] = None) -> bool:
        stmt = stmt.strip().rstrip(";").strip()
        if not stmt:
            return True
        m = re.match(r"return\s+(.*)$", stmt, re.DOTALL)
        if m:
            return self.emit_chain(m.group(1), env, lines, fixture_var=fixture_var, is_return=True) is not None
        m = re.match(r"(\w[\w<>]*)\s+(\w+)\s*=\s*(.*)$", stmt, re.DOTALL)
        if m:
            var, rhs = m.group(2), m.group(3)
            dm = re.match(r'TestDataLoader\.findDrugByKey\("(\w+)"\)$', rhs.strip())
            if dm:
                lines.append(f"const {var} = findDrugByKey({js_str(dm.group(1))});")
                env[var] = "data"
                return True
            return self.emit_chain(rhs, env, lines, target_var=var, fixture_var=fixture_var) is not None
        return self.emit_chain(stmt, env, lines, fixture_var=fixture_var) is not None

    # ------------------------------------------------- page method emission
    def tr_page_stmt(self, stmt: str, cls: str, locmap: Dict[str, str],
                     local_methods: set) -> Optional[List[str]]:
        stmt = stmt.strip()
        if not stmt:
            return []
        if stmt.startswith("if"):
            m = re.match(r"if\s*\((.*?)\)\s*\{(.*)\}\s*$", stmt, re.DOTALL)
            if not m:
                return None
            cond = self.tr_page_cond(m.group(1).strip(), cls, locmap, local_methods)
            if cond is None:
                return None
            inner: List[str] = []
            for s in split_statements(m.group(2)):
                t = self.tr_page_stmt(s, cls, locmap, local_methods)
                if t is None:
                    return None
                inner.extend(t)
            return [f"if ({cond}) {{"] + [f"  {ln}" for ln in inner] + ["}"]

        stmt = stmt.rstrip(";").strip()
        norm = re.sub(r"\s+", " ", stmt)

        if norm == "return this":
            return ["return this;"]
        m = re.fullmatch(r"return new (\w+)\(driver\)", norm)
        if m:
            return self._emit_transition(m.group(1))
        m = re.fullmatch(r"return (\w+)\(([^)]*)\)", norm)
        if m and m.group(1) in local_methods:
            args = m.group(2).strip()
            return [f"return await this.{self.rename.get(m.group(1), m.group(1))}({args});"]
        m = re.fullmatch(r"(\w+)\(([^()]*(?:\([^()]*\))?[^()]*)\)", norm)
        if m and m.group(1) in local_methods:
            return [f"await this.{self.rename.get(m.group(1), m.group(1))}({m.group(2).strip()});"]

        expr_lines = self.tr_page_action(norm, cls, locmap, local_methods)
        if expr_lines is not None:
            return expr_lines

        # assignment / return of a readable expression
        m = re.fullmatch(r"(?:String|int|boolean)\s+(\w+)\s*=\s*(.*)", norm, re.DOTALL)
        if m:
            rhs = self.tr_page_read(m.group(2), cls, locmap)
            if rhs is None:
                return None
            return [f"const {m.group(1)} = {rhs};"]
        m = re.fullmatch(r"return (.*)", norm, re.DOTALL)
        if m:
            rhs = self.tr_page_read(m.group(1), cls, locmap)
            if rhs is None:
                return None
            return [f"return {rhs};"]
        return None

    def tr_page_cond(self, cond: str, cls: str, locmap: Dict[str, str], local_methods: set) -> Optional[str]:
        m = re.fullmatch(r"isDisplayed\((\w+),\s*Duration\.ofSeconds\((\d+)\)\)", re.sub(r"\s+", " ", cond))
        if m and m.group(1) in locmap:
            return f"await this.isVisibleWithin(this.{locmap[m.group(1)]}, {int(m.group(2)) * 1000})"
        m = re.fullmatch(r"(\w+)\(\)", cond)
        if m and m.group(1) in local_methods:
            return f"await this.{m.group(1)}()"
        return None

    def tr_page_action(self, norm: str, cls: str, locmap: Dict[str, str], local_methods: set) -> Optional[List[str]]:
        m = re.fullmatch(r"click\((\w+)\)", norm)
        if m and m.group(1) in locmap:
            return [f"await this.{locmap[m.group(1)]}.click();"]
        m = re.fullmatch(r"type\((\w+),\s*(.+)\)", norm)
        if m and m.group(1) in locmap:
            return [f"await this.{locmap[m.group(1)]}.fill({m.group(2).strip()});"]
        m = re.fullmatch(r"selectByVisibleText\((\w+),\s*(.+)\)", norm)
        if m and m.group(1) in locmap:
            return [f"await this.selectOptionWhenPopulated(this.{locmap[m.group(1)]}, {m.group(2).strip()});"]
        m = re.fullmatch(r"waitVisible\((\w+)\)", norm)
        if m and m.group(1) in locmap:
            return [f"await this.{locmap[m.group(1)]}.waitFor({{ state: 'visible' }});"]
        m = re.fullmatch(r'driver\.get\(ConfigReader\.baseUrl\(\)\s*\+\s*"([^"]*)"\)', norm)
        if m:
            return [f"await this.page.goto({js_str(m.group(1))});"]
        return None

    def tr_page_read(self, expr: str, cls: str, locmap: Dict[str, str]) -> Optional[str]:
        expr = re.sub(r"\s+", " ", expr).strip()
        m = re.match(r"getText\((\w+)\)(.*)$", expr)
        if m and m.group(1) in locmap:
            suffix = m.group(2).strip()
            base = f"await this.readText(this.{locmap[m.group(1)]})"
            return f"({base}){suffix}" if suffix else base
        m = re.fullmatch(r"isDisplayed\((\w+), Duration\.ofSeconds\((\d+)\)\)", expr)
        if m and m.group(1) in locmap:
            return f"await this.isVisibleWithin(this.{locmap[m.group(1)]}, {int(m.group(2)) * 1000})"
        # pure local-variable expression chains (e.g. fullText.replace(...).trim())
        if re.fullmatch(r"\w+(\.(replace|trim|toLowerCase|toUpperCase)\((?:[^()]|\([^()]*\))*\))*", expr):
            return expr
        return None

    def _emit_transition(self, to_class: str) -> List[str]:
        """Landing pages whose legacy constructor gated on visibility keep
        that load-gate after the transition (waitForLoaded)."""
        target = self.pages.get(to_class, {})
        if target.get("constructor_waits"):
            var = decap(to_class)
            return [
                f"const {var} = new {to_class}(this.page);",
                f"await {var}.waitForLoaded();",
                f"return {var};",
            ]
        return [f"return new {to_class}(this.page);"]

    # ------------------------------------------------------------ patterns
    def match_pattern(self, page: dict, method: dict, locmap: Dict[str, str]) -> Optional[dict]:
        body = method["body"]
        if "for (" in body and any(a["kind"] == "type" for a in method["actions"]):
            return {"name": "autocomplete-retry-search", "emit": self.emit_autocomplete}
        if method["returns"] == "WebElement" and "contains(@class" in body:
            return {"name": "scoped-component-container", "emit": self.emit_result_card}
        if "findResultCard(" in body and "findElement" in body:
            return {"name": "scoped-component-read", "emit": self.emit_scoped_reader}
        return None

    def emit_autocomplete(self, page: dict, method: dict, locmap: Dict[str, str]) -> List[str]:
        body = method["body"]
        constants = page.get("constants", {})
        retries = constants.get("SEARCH_RETRY_ATTEMPTS", 3)
        timeout = constants.get("SUGGESTION_APPEAR_TIMEOUT", 6000)
        input_field = None
        for a in method["actions"]:
            if a["kind"] == "type" and a["locator_ref"]:
                input_field = locmap.get(a["locator_ref"].split(".")[-1])
        css_class = "result-row"
        m = re.search(r"' ([\w-]+) '", body)
        if m:
            css_class = m.group(1)
        params = [p.strip().split()[-1] for p in method["params"].split(",") if p.strip()]
        term, suggestion = params[0], params[1]
        dismiss = "dismissSignInNagIfPresent" in body
        lines = [
            f"async {method['name']}({', '.join(params)}) {{",
            "  // Suggestion label case differs between DOM, CSS rendering, and oracle",
            "  // data, so match the whole label case-insensitively (legacy parity).",
            f"  const suggestion = this.page",
            f"    .locator('a.{css_class}')",
            f"    .filter({{ hasText: exactTextCi({suggestion}) }});",
            "  let lastError = null;",
            f"  for (let attempt = 1; attempt <= {retries}; attempt++) {{",
        ]
        if dismiss:
            lines.append("    await this.dismissSignInNagIfPresent();")
        lines += [
            "    // The widget only reacts to real key events, and can miss the first",
            "    // keystroke burst right after page load; clear and retype to recover.",
            f"    await this.{input_field}.fill('');",
            f"    await this.{input_field}.pressSequentially({term}, {{ delay: 50 }});",
            "    try {",
            f"      await suggestion.first().click({{ timeout: {timeout} }});",
            "      return this;",
            "    } catch (e) {",
            "      lastError = e;",
            "    }",
            "  }",
            "  throw lastError;",
            "}",
        ]
        return lines

    def emit_result_card(self, page: dict, method: dict, locmap: Dict[str, str]) -> List[str]:
        body = method["body"]
        classes = re.findall(r"contains\(@class,\s*'([\w-]+)'\)", body)
        container = classes[0] if classes else "medicine-information-container"
        title_link = classes[1] if len(classes) > 1 else "medicine-name"
        params = [p.strip().split()[-1] for p in method["params"].split(",") if p.strip()]
        param = params[0]
        return [
            f"{self.rename.get(method['name'], method['name'])}({param}) {{",
            "  return this.page",
            f"    .locator('.{container}')",
            f"    .filter({{ has: this.page.locator('a.{title_link}', {{ hasText: exactTextCi({param}) }}) }});",
            "}",
        ]

    def emit_scoped_reader(self, page: dict, method: dict, locmap: Dict[str, str]) -> List[str]:
        body = method["body"]
        params = [p.strip().split()[-1] for p in method["params"].split(",") if p.strip()]
        param = params[0]
        child = None
        m = re.search(r'By\.cssSelector\("([^"]+)"\)', body)
        if m:
            child = m.group(1)
        else:
            m = re.search(r'By\.xpath\("\.//(\w+)\[contains\(@id,\'([^\']+)\'\)\]"\)', body)
            if m:
                child = f'{m.group(1)}[id*="{m.group(2)}"]'
        if child is None:
            return None
        transition = next((a["to"] for a in method["actions"] if a["kind"] == "page_transition"), None)
        lines = [f"async {method['name']}({param}) {{",
                 f"  const card = this.resultCard({param});"]
        if ".click()" in body and transition:
            lines += [f"  await card.locator({js_str(child)}).first().click();"]
            lines += [f"  {ln}" for ln in self._emit_transition(transition)]
        else:
            lines.append(f"  return (await card.locator({js_str(child)}).first().textContent()).trim();")
        lines.append("}")
        return lines

    # ------------------------------------------------------------ emitters
    def emit_page_file(self, page: dict) -> None:
        cls = page["class_name"]
        static_locators = [l for l in page["locators"] if l["name"].isupper()]
        locmap = {l["name"]: const_to_camel(l["name"]) for l in static_locators}
        locmap_by_ref = {l["name"]: const_to_camel(l["name"]) for l in static_locators}
        local_methods = {m["name"] for m in page["methods"]}
        needs_exact = False
        transitions = set()

        method_blocks: List[str] = []
        if page.get("constructor_waits"):
            gate = locmap.get(page["constructor_waits"][0], const_to_camel(page["constructor_waits"][0]))
            method_blocks.append("\n".join([
                "  /** Legacy parity: the Selenium constructor gated on this element being visible. */",
                "  async waitForLoaded() {",
                f"    await this.{gate}.waitFor({{ state: 'visible' }});",
                "    return this;",
                "  }",
            ]))
        for method in page["methods"]:
            jsdoc = []
            if method["doc"]:
                jsdoc = ["/**"] + [f" * {ln}" for ln in _wrap(method["doc"], 76)] + [" */"]

            pattern = self.match_pattern(page, method, locmap_by_ref)
            strategy = None
            lines = None
            if pattern:
                lines = pattern["emit"](page, method, locmap_by_ref)
                if lines is not None:
                    strategy = f"pattern:{pattern['name']}"
                    if any("exactTextCi" in ln for ln in lines):
                        needs_exact = True
            if lines is None:
                out: List[str] = []
                ok = True
                for stmt in split_statements(method["body"]):
                    t = self.tr_page_stmt(stmt, cls, locmap, local_methods)
                    if t is None:
                        ok = False
                        break
                    out.extend(t)
                if ok:
                    params = [p.strip().split()[-1] for p in method["params"].split(",") if p.strip()]
                    lines = [f"async {method['name']}({', '.join(params)}) {{"] + [f"  {ln}" for ln in out] + ["}"]
                    strategy = "generic-translation"
                    for ln in out:
                        m = re.search(r"new (\w+)\(this\.page\)", ln)
                        if m:
                            transitions.add(m.group(1))
                else:
                    lines = [
                        f"async {method['name']}() {{",
                        f"  // TODO(review): could not regenerate deterministically — legacy body:",
                    ] + [f"  // {ln}" for ln in method["body"].splitlines()] + [
                        f"  throw new Error('Not regenerated: {cls}.{method['name']} — needs human review');",
                        "}",
                    ]
                    strategy = "todo-stub"
            else:
                for ln in lines:
                    m = re.search(r"new (\w+)\(this\.page\)", ln)
                    if m:
                        transitions.add(m.group(1))

            self.report["methods"].append(
                {"ir_ref": method["id"], "strategy": strategy, "target": f"pages/{class_to_kebab(cls)}.js"}
            )
            method_blocks.append("\n".join(["  " + ln for ln in jsdoc + lines]))

        header = [
            "// @ts-check",
            f"// Generated by AI Test Modernization Platform — blueprint {BLUEPRINT_ID}",
            f"// Source: selenium-framework/{page['file']}",
        ]
        if page["doc"]:
            header += ["/**"] + [f" * {ln}" for ln in _wrap(page["doc"], 76)] + [" */"]

        base_imports = "BasePage" + (", exactTextCi" if needs_exact else "")
        imports = [f"import {{ {base_imports} }} from './base-page.js';"]
        for t in sorted(transitions):
            if t != cls and t in self.pages:
                imports.append(f"import {{ {t} }} from './{class_to_kebab(t)}.js';")

        ctor = ["  /** @param {import('@playwright/test').Page} page */", "  constructor(page) {", "    super(page);"]
        if static_locators:
            ctor.append("    // .first(): Selenium By-locators resolve to the first match; the AUT even")
            ctor.append("    // ships duplicate ids, so keep that semantic instead of strict mode.")
        for l in static_locators:
            sel = locator_selector(l["strategy"], l["value"] if l["value"] is not None else l["raw"])
            ctor.append(f"    this.{locmap[l['name']]} = page.locator({js_str(sel)}).first();")
        ctor.append("  }")

        content = (
            "\n".join(header)
            + "\n"
            + "\n".join(imports)
            + f"\n\nexport class {cls} extends BasePage {{\n"
            + "\n".join(ctor)
            + "\n\n"
            + "\n\n".join(method_blocks)
            + "\n}\n"
        )
        self._write(f"pages/{class_to_kebab(cls)}.js", content)

    def emit_config(self) -> None:
        props = self.ir["config"]["properties"]
        lines = [
            "// @ts-check",
            f"// Generated by AI Test Modernization Platform — blueprint {BLUEPRINT_ID}",
            f"// Source: selenium-framework/{self.ir['config']['file']}",
            "export const config = {",
        ]
        for k, v in props.items():
            if k in ("browser", "headless"):
                continue  # browser selection is playwright.config.js's job
            val = v
            if v.isdigit():
                lines.append(f"  {k}: {v},")
            else:
                lines.append(f"  {k}: {js_str(val)},")
        lines.append("};")
        self._write("config/framework-config.js", "\n".join(lines) + "\n")

    def emit_static(self) -> None:
        (self.target / "pages").mkdir(parents=True, exist_ok=True)
        shutil.copy(BLUEPRINT_DIR / "static" / "base-page.js", self.target / "pages" / "base-page.js")
        self.report["files"].append("pages/base-page.js")
        (self.target / "utils").mkdir(parents=True, exist_ok=True)
        shutil.copy(BLUEPRINT_DIR / "static" / "test-data-loader.js", self.target / "utils" / "test-data-loader.js")
        self.report["files"].append("utils/test-data-loader.js")

    def emit_test_data(self) -> None:
        for name, entry in self.ir["test_data"].items():
            rel = f"testdata/{name}.json"
            self._write(rel, json.dumps(entry["content"], indent=2) + "\n")

    def emit_fixture(self) -> None:
        helper = self.ir["helpers"][0]
        env: Dict[str, str] = {}
        lines: List[str] = []
        ok = True
        for stmt in split_statements(helper["body"]):
            if not self.tr_flow_stmt(stmt, env, lines):
                ok = False
        # the trailing 'return X;' becomes 'await use(X);'
        if lines and lines[-1].startswith("return "):
            var = lines.pop()[len("return ") :].rstrip(";")
            lines.append(f"await use({var});")
        page_imports = sorted({c for c in env.values() if c in self.pages})
        doc = helper["doc"] or ""
        content_lines = [
            "// @ts-check",
            f"// Generated by AI Test Modernization Platform — blueprint {BLUEPRINT_ID}",
            f"// Source: selenium-framework/{helper['file']} ({helper['id']})",
            "import { test as base, expect } from '@playwright/test';",
            "import { config } from '../config/framework-config.js';",
        ]
        for c in page_imports:
            content_lines.append(f"import {{ {c} }} from '../pages/{class_to_kebab(c)}.js';")
        content_lines += [
            "",
            "export const test = base.extend({",
            "  /**",
        ] + [f"   * {ln}" for ln in _wrap(doc, 74)] + [
            "   */",
            "  medicineSearchPage: async ({ page }, use) => {",
        ] + [f"    {ln}" for ln in lines] + [
            "  },",
            "});",
            "",
            "export { expect };",
            "",
        ]
        self._write("fixtures/guest-flow.js", "\n".join(content_lines))
        self.report["methods"].append(
            {"ir_ref": helper["id"], "strategy": "generic-translation" if ok else "partial",
             "target": "fixtures/guest-flow.js"}
        )

    # ------------------------------------------------------------ specs
    def _translate_test(self, tim, ir_test: dict, spec_name: str) -> dict:
        """Translate one TIM test into Playwright body lines + coverage record."""
        coverage = {"test_id": tim.test_id, "spec": f"tests/{spec_name}",
                    "title": f"{tim.test_id} · {tim.name}", "steps": [], "assertions": []}

        env: Dict[str, str] = {}
        body_lines: List[str] = []
        uses_data, uses_config = False, False
        assertion_iter = iter(tim.assertions)
        # steps not bound to the shared fixture, in order, for comments
        step_iter = iter([s for s in tim.steps
                          if s.binding_ref and not s.binding_ref.startswith("helper:")])

        fixture_steps = [s for s in tim.steps if s.binding_ref and s.binding_ref.startswith("helper:")]
        for s in fixture_steps:
            body_lines.append(f"// [{s.step_id}] {s.description} (via fixture, confidence {s.confidence})")
            coverage["steps"].append({"step_id": s.step_id, "realized_by": "fixtures/guest-flow.js"})
        if fixture_steps:
            body_lines.append("")

        for stmt in split_statements(ir_test["body"]):
            norm = re.sub(r"\s+", " ", stmt).strip()
            am = re.match(r"Assert\s*\.\s*(assert\w+)\s*\((.*)\)\s*;?$", norm, re.DOTALL)
            if am:
                a = next(assertion_iter, None)
                if a:
                    body_lines.append(f"// [{a.assertion_id}] {a.description} (confidence {a.confidence})")
                    if a.failure_meaning:
                        body_lines.append(f"//   failure means: {a.failure_meaning}")
                    coverage["assertions"].append(
                        {"assertion_id": a.assertion_id, "realized_by": f"tests/{spec_name}"}
                    )
                args = split_top_level(am.group(2))
                kind = am.group(1)
                if kind in ("assertEquals", "assertNotEquals"):
                    actual = self.tr_expr(args[0], env)
                    expected = self.tr_expr(args[1], env)
                    msg = f", {args[2]}" if len(args) > 2 else ""
                    matcher = "toBe" if kind == "assertEquals" else "not.toBe"
                    body_lines.append(f"expect({actual}{msg}).{matcher}({expected});")
                else:
                    cond = self.tr_expr(args[0], env)
                    msg = f", {args[1]}" if len(args) > 1 else ""
                    expected = "true" if kind == "assertTrue" else "false"
                    body_lines.append(f"expect({cond}{msg}).toBe({expected});")
                body_lines.append("")
                if "expected." in " ".join(body_lines) or "findDrugByKey" in " ".join(body_lines):
                    uses_data = True
                if "config." in " ".join(body_lines):
                    uses_config = True
                continue

            step = next(step_iter, None)
            if step and "findDrugByKey" not in norm:
                body_lines.append(f"// [{step.step_id}] {step.description} (confidence {step.confidence})")
                coverage["steps"].append({"step_id": step.step_id, "realized_by": f"tests/{spec_name}"})
            elif step and "findDrugByKey" in norm:
                # data-loading statements aren't TIM interaction steps; put it back
                step_iter = _push_back(step, step_iter)
            before = len(body_lines)
            if not self.tr_flow_stmt(stmt, env, body_lines, fixture_var="medicineSearchPage"):
                body_lines.append(f"// TODO(review): untranslated statement: {norm}")
            if any("findDrugByKey" in ln for ln in body_lines[before:]):
                uses_data = True
            if any("config." in ln for ln in body_lines[before:]):
                uses_config = True
            body_lines.append("")

        return {"body_lines": body_lines, "coverage": coverage,
                "uses_data": uses_data, "uses_config": uses_config}

    def _annotations(self, tim) -> List[str]:
        anns = [
            f"{{ type: 'tim', description: {js_str(tim.test_id + ' — ' + tim.business_capability + f' (overall confidence {tim.overall_confidence})')} }}",
            f"{{ type: 'source', description: {js_str('selenium:' + tim.source.test_class + '.' + tim.source.test_method)} }}",
        ]
        d = (self.plan_index or {}).get("by_test", {}).get(tim.test_id)
        if d and d["disposition"] != "MIGRATE":
            anns.append(f"{{ type: 'disposition', description: {js_str(d['disposition'] + ': ' + d['rationale'])} }}")
        return anns

    def _single_block(self, tim, tr: dict) -> str:
        desc_comment = ["  /**"] + [f"   * {ln}" for ln in _wrap(tim.description, 74)]
        d = (self.plan_index or {}).get("by_test", {}).get(tim.test_id)
        if d and d["disposition"] == "REDESIGN":
            desc_comment += ["   *"] + [f"   * REDESIGN ({d.get('target_channel') or 'ui'}): {ln}"
                                        for ln in _wrap(d.get("redesign_note") or "", 66)]
        if d and d["disposition"] == "SPLIT":
            desc_comment += ["   *", "   * SPLIT pending — this body still proves several intents:"]
            desc_comment += [f"   *   - {t}" for t in d.get("split_targets", [])]
        desc_comment += ["   */"]
        return "\n".join(desc_comment) + "\n" + "\n".join([
            f"  test({js_str(tim.test_id + ' · ' + tim.name)}, {{",
            f"    annotation: [{', '.join(self._annotations(tim))}],",
            "  }, async ({ medicineSearchPage, page }) => {",
        ] + [("    " + ln).rstrip() for ln in tr["body_lines"]] + [
            "  });",
        ])

    def _merged_block(self, translated: List, spec_name: str, index: int) -> Optional[str]:
        """One parameterized body for a merge group, or None if the merge cannot be
        realized without changing behavior (the members are then emitted separately)."""
        members = [tim for tim, _ in translated]
        group_id = (self.plan_index or {}).get("group_of", {}).get(members[0].test_id, f"group-{index}")
        result = parameterize([tr["body_lines"] for _, tr in translated])

        record = {
            "group_id": group_id,
            "primary": members[0].test_id,
            "members": [t.test_id for t in members],
            "spec": f"tests/{spec_name}",
            "realized": result["ok"],
        }
        if not result["ok"]:
            record["reason"] = result["reason"]
            record["blocking_statement"] = result["blocking"]
            self.report["merges"].append(record)
            return None
        record["parameters"] = sorted(set(result["params"]))
        self.report["merges"].append(record)

        const = f"MERGED_CASES_{index}" if index else "MERGED_CASES"
        rows = []
        for (tim, _), values in zip(translated, result["cases"]):
            fields = [
                f"timId: {js_str(tim.test_id)}",
                f"name: {js_str(tim.name)}",
                f"capability: {js_str(tim.business_capability)}",
                f"confidence: {tim.overall_confidence}",
                f"source: {js_str('selenium:' + tim.source.test_class + '.' + tim.source.test_method)}",
            ] + [f"{k}: {v}" for k, v in values.items()]
            rows.append("    { " + ", ".join(fields) + " },")

        header = ["  /**", f"   * Merged by the rationalization plan (group {group_id}):",
                  f"   *   {', '.join(t.test_id + ' - ' + t.name for t in members)}",
                  "   *",
                  "   * One implementation, one case per intent: every merged intent still runs",
                  "   * its own expected values and still reports under its own TIM id.",
                  f"   * The inline commentary below is {members[0].test_id}'s; each case replays it",
                  "   * against its own data row.",
                  "   *"]
        header += [f"   * {ln}" for ln in _wrap(members[0].description, 74)] + ["   */"]

        body = [("      " + ln).rstrip() for ln in result["template"]]
        return "\n".join(header) + "\n" + "\n".join(
            [f"  const {const} = ["] + rows + ["  ];", "",
             f"  for (const testCase of {const}) {{",
             "    test(`${testCase.timId} \\u00b7 ${testCase.name}`, {",
             "      annotation: ["
             "{ type: 'tim', description: `${testCase.timId} \\u2014 ${testCase.capability} "
             "(overall confidence ${testCase.confidence})` }, "
             "{ type: 'source', description: testCase.source }, "
             f"{{ type: 'merged', description: {js_str('rationalization group ' + group_id)} }}],",
             "    }, async ({ medicineSearchPage, page }) => {"]
            + body
            + ["    });", "  }"]
        )

    def _spec_groups(self, tims_by_id: Dict[str, object]) -> List[List]:
        """Ordered groups of TIM tests that share one generated implementation.

        Without a rationalization plan every test is its own group (the platform's
        pre-rationalization behavior). With a plan, RETIRE/DEFER tests are recorded
        as skipped rather than emitted, and merge groups become one group."""
        if not self.plan_index:
            return [[t] for t in self.suite.tests]

        for tid, info in self.plan_index["skip"].items():
            tim = tims_by_id.get(tid)
            self.report["skipped_by_disposition"].append({
                "test_id": tid,
                "name": getattr(tim, "name", None),
                "disposition": info["disposition"],
                "reason": info["reason"],
                "source": (f"{tim.source.test_class}.{tim.source.test_method}" if tim else None),
            })

        groups: List[List] = []
        for member_ids in self.plan_index["emit"]:
            members = [tims_by_id[m] for m in member_ids if m in tims_by_id]
            if members:
                groups.append(members)
        return groups

    def emit_specs(self) -> None:
        ir_tests = {t["id"]: t for t in self.ir["tests"]}
        tims_by_id = {t.test_id: t for t in self.suite.tests}
        groups = self._spec_groups(tims_by_id)

        by_class: Dict[str, List[List]] = {}
        for g in groups:
            by_class.setdefault(g[0].source.test_class, []).append(g)

        merge_index = 0
        for test_class, class_groups in by_class.items():
            spec_name = class_to_kebab(test_class.removesuffix("Tests")) + ".spec.js"
            blocks: List[str] = []
            uses_data, uses_config = False, False

            for group in class_groups:
                translated = []
                for tim in group:
                    ir_test = ir_tests.get(f"test:{tim.source.test_class}.{tim.source.test_method}")
                    if ir_test is None:
                        continue
                    translated.append((tim, self._translate_test(tim, ir_test, spec_name)))
                if not translated:
                    continue
                uses_data = uses_data or any(tr["uses_data"] for _, tr in translated)
                uses_config = uses_config or any(tr["uses_config"] for _, tr in translated)

                if len(translated) > 1:
                    merge_index += 1
                    merged = self._merged_block(translated, spec_name, merge_index)
                    if merged is not None:
                        blocks.append(merged)
                        for _tim, tr in translated:
                            self.report["tim_coverage"].append(tr["coverage"])
                        continue

                for tim, tr in translated:
                    blocks.append(self._single_block(tim, tr))
                    self.report["tim_coverage"].append(tr["coverage"])

            if not blocks:
                continue

            imports = [
                "// @ts-check",
                f"// Generated by AI Test Modernization Platform — blueprint {BLUEPRINT_ID}",
                f"// Source: selenium:{test_class} | TIM: artifacts/tim/tim-suite.json",
                "import { test, expect } from '../fixtures/guest-flow.js';",
            ]
            if uses_data:
                imports.append("import { findDrugByKey } from '../utils/test-data-loader.js';")
            if uses_config:
                imports.append("import { config } from '../config/framework-config.js';")

            describe = (self.suite.tests[0].business_capability
                        if len(by_class) == 1 else test_class)
            content = (
                "\n".join(imports)
                + f"\n\ntest.describe({js_str(describe)}, () => {{\n"
                + "\n\n".join(blocks)
                + "\n});\n"
            )
            self._write(f"tests/{spec_name}", content)

    def emit_playwright_config(self) -> None:
        props = self.ir["config"]["properties"]
        base_url = props.get("baseUrl", "")
        explicit_ms = int(props.get("explicitWaitSeconds", "15")) * 1000
        nav_ms = int(props.get("pageLoadTimeoutSeconds", "30")) * 1000
        content = f"""// @ts-check
// Generated by AI Test Modernization Platform — blueprint {BLUEPRINT_ID}
// Timeouts mirror the legacy config.properties (explicitWaitSeconds, pageLoadTimeoutSeconds).
import {{ defineConfig, devices }} from '@playwright/test';

export default defineConfig({{
  testDir: './tests',
  // Live third-party site: run serially to stay polite and stable.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  timeout: 180_000,
  expect: {{ timeout: {explicit_ms} }},
  reporter: [
    ['html', {{ open: 'never' }}],
    ['json', {{ outputFile: 'test-results/results.json' }}],
  ],
  use: {{
    baseURL: {js_str(base_url)},
    actionTimeout: {explicit_ms},
    navigationTimeout: {nav_ms},
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  }},
  projects: [
    {{ name: 'chromium', use: {{ ...devices['Desktop Chrome'] }} }},
    {{ name: 'firefox', use: {{ ...devices['Desktop Firefox'] }} }},
    {{ name: 'webkit', use: {{ ...devices['Desktop Safari'] }} }},
  ],
}});
"""
        self._write("playwright.config.js", content)

    # ------------------------------------------------------------ runner
    def generate(self) -> dict:
        self.emit_config()
        self.emit_static()
        self.emit_test_data()
        for page in self.ir["page_objects"]:
            self.emit_page_file(page)
        self.emit_fixture()
        self.emit_specs()
        self.emit_playwright_config()
        return self.report


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _push_back(item, iterator):
    import itertools

    return itertools.chain([item], iterator)

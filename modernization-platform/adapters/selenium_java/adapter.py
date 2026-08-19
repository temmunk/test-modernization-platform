"""Selenium/Java/TestNG source adapter.

Deterministic (no AI) extraction of the legacy framework into a Normalized
Technical IR: page objects, locators, methods with recognized action calls,
test methods with assertions, shared helpers, config, and the test-data
catalog. Every extracted node carries file + line evidence so downstream
AI inferences stay traceable to source facts.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

IR_VERSION = "1.0"

JAVADOC_RE = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
CLASS_RE = re.compile(
    r"(?:public\s+)?(?:abstract\s+)?(?:final\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+[\w,\s]+)?\s*\{"
)
LOCATOR_RE = re.compile(
    r"By\s+(\w+)\s*=\s*(By\.(\w+)\s*\((.*?)\))\s*;", re.DOTALL
)
METHOD_SIG_RE = re.compile(
    r"^([ \t]+)(public|private|protected)\s+(?:static\s+)?(?:final\s+)?"
    r"(?:<[\w,\s]+>\s+)?([\w<>\[\],.\s]+?)\s+(\w+)\s*\(([^)]*)\)\s*\{",
    re.MULTILINE,
)
TEST_ANNOTATION_RE = re.compile(r"@Test\s*(?:\((.*?)\))?\s*$", re.DOTALL)
STRING_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _clean_javadoc(raw: str) -> str:
    lines = [re.sub(r"^\s*\*\s?", "", ln).strip() for ln in raw.splitlines()]
    return " ".join(ln for ln in lines if ln).strip()


def _join_string_concat(expr: str) -> Optional[str]:
    """'ab' + 'cd' -> 'abcd' if the expression is only string literals joined by +."""
    stripped = re.sub(r"\s+", " ", expr).strip()
    parts = [p.strip() for p in _split_top_level(stripped, "+")]
    out = []
    for p in parts:
        m = STRING_LITERAL_RE.fullmatch(p)
        if not m:
            return None
        out.append(m.group(1))
    return "".join(out)


def _split_top_level(s: str, sep: str) -> List[str]:
    """Split on sep occurrences that sit outside parens/quotes."""
    parts, depth, in_str, cur, i = [], 0, False, [], 0
    while i < len(s):
        c = s[i]
        if in_str:
            cur.append(c)
            if c == "\\":
                if i + 1 < len(s):
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
        elif depth == 0 and s[i : i + len(sep)] == sep:
            parts.append("".join(cur))
            cur = []
            i += len(sep) - 1
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _match_braces(text: str, open_idx: int) -> int:
    """Index just past the '}' matching the '{' at open_idx (quote/comment aware enough for this codebase)."""
    depth, i, in_str, in_char = 0, open_idx, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif in_char:
            if c == "\\":
                i += 1
            elif c == "'":
                in_char = False
        elif c == '"':
            in_str = True
        elif c == "'":
            in_char = True
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        elif text.startswith("/*", i):
            i = text.find("*/", i) + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("Unbalanced braces")


def _extract_call_args(body: str, call_start: int) -> str:
    """Balanced-paren argument text for a call whose '(' is at call_start."""
    depth, i, in_str = 0, call_start, False
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return body[call_start + 1 : i]
        i += 1
    return ""


# Base-class interaction primitives -> normalized action kinds
ACTION_KINDS = {
    "click": "click",
    "type": "type",
    "selectByVisibleText": "select_option",
    "getText": "read_text",
    "waitVisible": "wait_visible",
    "waitClickable": "wait_clickable",
    "isDisplayed": "check_visible",
}


class SeleniumJavaAdapter:
    """discover() -> parse() -> normalize() over a Maven Selenium/TestNG repo."""

    def __init__(self, framework_root: Path):
        self.root = Path(framework_root)

    # ---------- discover ----------
    def discover(self) -> List[Path]:
        return sorted(
            p
            for p in self.root.rglob("*.java")
            if "target" not in p.parts
        )

    # ---------- parse one file ----------
    def parse_file(self, path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(self.root).as_posix()

        cls = CLASS_RE.search(text)
        class_name = cls.group(1) if cls else path.stem
        extends = cls.group(2) if cls else None

        class_doc = ""
        if cls:
            for jd in JAVADOC_RE.finditer(text, 0, cls.start()):
                pass_end = jd.end()
                between = text[pass_end : cls.start()]
                if not re.search(r"\b(class|interface|enum)\b", between):
                    class_doc = _clean_javadoc(jd.group(1))

        locators = []
        for m in LOCATOR_RE.finditer(text):
            name, raw, strategy, arg = m.group(1), m.group(2), m.group(3), m.group(4)
            value = _join_string_concat(arg)
            locators.append(
                {
                    "id": f"locator:{class_name}.{name}",
                    "name": name,
                    "strategy": strategy,
                    "value": value,
                    "raw": re.sub(r"\s+", " ", raw).strip(),
                    "file": rel,
                    "line": _line_of(text, m.start()),
                }
            )
        locator_names = {l["name"] for l in locators}

        constants = {}
        for m in re.finditer(r"static\s+final\s+(?:int|long)\s+(\w+)\s*=\s*(\d+)\s*;", text):
            constants[m.group(1)] = int(m.group(2))
        for m in re.finditer(r"static\s+final\s+Duration\s+(\w+)\s*=\s*Duration\.of(Seconds|Millis)\((\d+)\)\s*;", text):
            ms = int(m.group(3)) * (1000 if m.group(2) == "Seconds" else 1)
            constants[m.group(1)] = ms

        methods = []
        for m in METHOD_SIG_RE.finditer(text):
            returns, name, params = m.group(3).strip(), m.group(4), m.group(5).strip()
            if name in ("if", "for", "while", "switch", "catch"):
                continue
            try:
                body_end = _match_braces(text, text.index("{", m.end() - 1))
            except ValueError:
                continue
            body = text[m.end() : body_end - 1].strip()
            line = _line_of(text, m.start(1) + len(m.group(1)))

            # preceding javadoc + annotations
            head = text[: m.start()]
            doc, test_description = "", None
            tail = head[-2000:]
            jds = list(JAVADOC_RE.finditer(tail))
            if jds:
                between = tail[jds[-1].end() :]
                if not re.search(r"\{|\}|;", re.sub(r"@\w+\s*(\([^)]*\))?", "", between).strip()):
                    doc = _clean_javadoc(jds[-1].group(1))
            ann_zone = tail[jds[-1].end() :] if jds else tail
            is_test = False
            if "@Test" in ann_zone:
                is_test = True
                paren = ann_zone.find("(", ann_zone.rfind("@Test"))
                if paren != -1 and ann_zone[ann_zone.rfind("@Test") + 5 :].lstrip().startswith("("):
                    args = _extract_call_args(ann_zone, paren)
                    dm = re.search(r"description\s*=\s*(.+)", args, re.DOTALL)
                    if dm:
                        test_description = _join_string_concat(dm.group(1)) or dm.group(1).strip()

            actions = self._extract_actions(body, locator_names, class_name)

            methods.append(
                {
                    "id": f"method:{class_name}.{name}",
                    "name": name,
                    "returns": returns,
                    "params": params,
                    "doc": doc,
                    "is_test": is_test,
                    "test_description": test_description,
                    "file": rel,
                    "line": line,
                    "actions": actions,
                    "assertions": self._extract_assertions(body, text, m.end()),
                    "has_control_flow": bool(
                        re.search(r"\b(if|for|while|try)\s*[({]", body)
                    ),
                    "body": body,
                }
            )

        # constructor waits: e.g. MedicineDetailsPage() blocks until the heading
        # is visible — a load-gate the target must reproduce after transitions
        constructor_waits = []
        ctor = re.search(r"public\s+" + re.escape(class_name) + r"\s*\(\s*WebDriver\s+\w+\s*\)\s*\{", text)
        if ctor:
            ctor_end = _match_braces(text, text.index("{", ctor.end() - 1))
            ctor_body = text[ctor.end() : ctor_end - 1]
            for m in re.finditer(r"waitVisible\((\w+)\)", ctor_body):
                if m.group(1) in locator_names:
                    constructor_waits.append(m.group(1))

        return {
            "class_name": class_name,
            "extends": extends,
            "doc": class_doc,
            "file": rel,
            "locators": locators,
            "constants": constants,
            "constructor_waits": constructor_waits,
            "methods": methods,
        }

    def _extract_actions(self, body: str, locator_names: set, class_name: str) -> List[dict]:
        actions = []
        for m in re.finditer(r"\b(\w+)\s*\(", body):
            fn = m.group(1)
            args_text = _extract_call_args(body, m.end() - 1)
            args = [a.strip() for a in _split_top_level(args_text, ",")] if args_text.strip() else []
            if fn in ACTION_KINDS:
                locator_ref = (
                    f"locator:{class_name}.{args[0]}" if args and args[0] in locator_names else (args[0] if args else None)
                )
                actions.append(
                    {
                        "kind": ACTION_KINDS[fn],
                        "locator_ref": locator_ref,
                        "args": args[1:],
                    }
                )
            elif fn == "get" and re.search(r"driver\s*\.\s*get\s*\($", body[: m.end()]):
                literal = _join_string_concat(args_text)
                actions.append({"kind": "navigate", "url_expr": args_text.strip(), "url": literal})
        for m in re.finditer(r"new\s+(\w+(?:Page|Modal))\s*\(", body):
            actions.append({"kind": "page_transition", "to": m.group(1)})
        return actions

    def _extract_assertions(self, body: str, full_text: str, body_offset: int) -> List[dict]:
        out = []
        for m in re.finditer(r"Assert\s*\.\s*(assert\w+)\s*\(", body):
            args_text = _extract_call_args(body, m.end() - 1)
            args = [re.sub(r"\s+", " ", a).strip() for a in _split_top_level(args_text, ",")]
            entry = {"kind": m.group(1), "line": _line_of(full_text, body_offset + m.start())}
            if m.group(1) in ("assertEquals", "assertNotEquals"):
                entry["actual_expr"] = args[0] if len(args) > 0 else None
                entry["expected_expr"] = args[1] if len(args) > 1 else None
                entry["message"] = _join_string_concat(args[2]) if len(args) > 2 else None
            else:
                entry["condition_expr"] = args[0] if args else None
                entry["message"] = _join_string_concat(args[1]) if len(args) > 1 else None
            out.append(entry)
        return out

    # ---------- config + data ----------
    def extract_config(self) -> dict:
        cfg_path = self.root / "src" / "test" / "resources" / "config.properties"
        props = {}
        if cfg_path.exists():
            for ln in cfg_path.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    props[k.strip()] = v.strip()
        return {"file": "src/test/resources/config.properties", "properties": props}

    def extract_test_data(self) -> dict:
        data_dir = self.root / "src" / "test" / "resources" / "testdata"
        catalog = {}
        for p in sorted(data_dir.glob("*.json")) if data_dir.exists() else []:
            catalog[p.stem] = {
                "file": p.relative_to(self.root).as_posix(),
                "content": json.loads(p.read_text(encoding="utf-8")),
            }
        return catalog

    # ---------- normalize ----------
    def normalize(self) -> dict:
        page_objects, tests_classes, support = [], [], []
        for path in self.discover():
            parsed = self.parse_file(path)
            pkg = path.parent.name
            if pkg == "pages":
                if parsed["class_name"] == "BasePage":
                    support.append(parsed)
                else:
                    parsed["id"] = f"page:{parsed['class_name']}"
                    page_objects.append(parsed)
            elif pkg == "tests":
                tests_classes.append(parsed)
            else:
                support.append(parsed)

        tests, helpers = [], []
        for tc in tests_classes:
            for m in tc["methods"]:
                node = dict(m)
                node["class_name"] = tc["class_name"]
                node["class_doc"] = tc["doc"]
                if m["is_test"]:
                    node["id"] = f"test:{tc['class_name']}.{m['name']}"
                    tests.append(node)
                elif m["name"] not in ("setUp", "tearDown", "getDriver"):
                    node["id"] = f"helper:{tc['class_name']}.{m['name']}"
                    helpers.append(node)

        return {
            "ir_version": IR_VERSION,
            "source_framework": {
                "kind": "selenium-java-testng",
                "root": self.root.name,
                "build": "maven",
                "suite_file": "testng.xml",
            },
            "config": self.extract_config(),
            "test_data": self.extract_test_data(),
            "page_objects": page_objects,
            "helpers": helpers,
            "tests": tests,
            "support_classes": [
                {"class_name": s["class_name"], "file": s["file"], "doc": s["doc"]} for s in support
            ],
        }

    # ---------- batched normalization ----------
    def normalize_batched(self) -> tuple[dict, list[dict]]:
        """Split the estate into a shared core (pages, helpers, config, data)
        plus one batch per test class carrying only that class's tests and the
        ids of the shared assets it actually depends on (transitive closure
        over helper calls and page-to-page transitions)."""
        ir = self.normalize()
        shared_core = {k: ir[k] for k in (
            "ir_version", "source_framework", "config", "test_data",
            "page_objects", "helpers", "support_classes",
        )}

        pages = {p["class_name"]: p for p in ir["page_objects"]}
        helpers = {h["name"]: h for h in ir["helpers"]}

        def pages_mentioned(text: str) -> set:
            return {name for name in pages if re.search(r"\b" + re.escape(name) + r"\b", text)}

        # page -> pages reachable in one hop (transition returns / new Page(...))
        page_edges = {}
        for name, p in pages.items():
            out = set()
            for m in p["methods"]:
                if m["returns"] in pages and m["returns"] != name:
                    out.add(m["returns"])
                for a in m["actions"]:
                    if a["kind"] == "page_transition" and a["to"] in pages and a["to"] != name:
                        out.add(a["to"])
            page_edges[name] = out

        def page_closure(seed: set) -> set:
            todo, seen = list(seed), set(seed)
            while todo:
                for nxt in page_edges.get(todo.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        todo.append(nxt)
            return seen

        by_class: dict[str, list] = {}
        for t in ir["tests"]:
            by_class.setdefault(t["class_name"], []).append(t)

        batches = []
        for test_class in sorted(by_class):
            tests = by_class[test_class]
            bodies = "\n".join(t["body"] for t in tests)
            helper_names = {h for h in helpers if re.search(r"\b" + re.escape(h) + r"\s*\(", bodies)}
            direct_pages = pages_mentioned(bodies)
            for h in helper_names:
                direct_pages |= pages_mentioned(helpers[h]["body"])
            closure = page_closure(direct_pages)
            batches.append({
                "id": class_to_batch_id(test_class),
                "test_class": test_class,
                "class_doc": tests[0].get("class_doc", ""),
                "tests": tests,
                "dependencies": {
                    "page_objects": sorted(f"page:{p}" for p in closure),
                    "helpers": sorted(helpers[h]["id"] for h in helper_names),
                },
            })
        return shared_core, batches


def class_to_batch_id(test_class: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "-", test_class).lower()

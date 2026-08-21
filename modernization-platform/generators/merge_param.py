"""Deterministic parameterization of merged tests.

A MERGE decision says "these intents are the same test wearing different data".
This module proves it - or refuses. It diffs the already-translated bodies of the
merge members statement by statement; if they are identical except for literal
values, those literals are lifted into a case table and the merge is realized as
ONE parameterized test body with one case per intent. Nothing is dropped: every
merged intent still runs, still asserts its own expected values, and still reports
under its own TIM id.

If the bodies differ anywhere that is not a literal, the merge is refused and the
blocking statement is reported. The platform never silently loses an assertion to
make a consolidation look good.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

TOKEN_RE = re.compile(
    r"""
    "(?:\\.|[^"\\])*"        # double-quoted string
  | '(?:\\.|[^'\\])*'        # single-quoted string
  | \b(?:true|false|null)\b  # keyword literals
  | \b\d+(?:\.\d+)?\b        # numbers
  | [A-Za-z_$][\w$]*         # identifiers
  | \S                       # any other single non-space char
""",
    re.VERBOSE,
)

MARKER_RE = re.compile(r"^\s*//\s*\[([A-Za-z]+\d+)\]")


def _is_literal(tok: str) -> bool:
    return (
        tok[:1] in ('"', "'")
        or tok in ("true", "false", "null")
        or bool(re.fullmatch(r"\d+(?:\.\d+)?", tok))
    )


def _is_code(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("//")


def _marker_for(body: List[str], idx: int) -> Optional[str]:
    """The nearest preceding `// [A7]` / `// [S2]` marker, lowercased."""
    for i in range(idx - 1, -1, -1):
        m = MARKER_RE.match(body[i])
        if m:
            return m.group(1).lower()
        if _is_code(body[i]):
            return None
    return None


def _param_name(line: str, start: int, token: str, marker: Optional[str], used: Dict[str, int]) -> str:
    before = line[:start].rstrip()
    if before.endswith("findDrugByKey("):
        base = "drugKey"
    elif marker:
        base = f"{marker}Message" if token[:1] in ('"', "'") else f"{marker}Expected"
    else:
        base = "value"
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}{used[base]}"


def parameterize(bodies: List[List[str]], case_var: str = "testCase") -> dict:
    """Diff translated bodies and lift differing literals into a case table.

    bodies[0] is the primary; its comments and formatting become the template.

    Returns:
      {"ok": bool, "reason": str|None, "template": [str], "params": [str],
       "cases": [{param: js_literal_source}], "blocking": {...}|None}
    """
    if len(bodies) < 2:
        return {"ok": False, "reason": "a merge needs at least 2 members", "template": [],
                "params": [], "cases": [], "blocking": None}

    code_idx = [[i for i, ln in enumerate(b) if _is_code(ln)] for b in bodies]
    if len({len(idx) for idx in code_idx}) != 1:
        return {
            "ok": False,
            "reason": "members have different statement counts ({}) - flows are not congruent".format(
                ", ".join(str(len(idx)) for idx in code_idx)
            ),
            "template": [], "params": [], "cases": [], "blocking": None,
        }

    primary = bodies[0]
    template = list(primary)
    params: List[str] = []
    cases: List[Dict[str, str]] = [{} for _ in bodies]
    used: Dict[str, int] = {}

    for pos in range(len(code_idx[0])):
        lines = [bodies[m][code_idx[m][pos]] for m in range(len(bodies))]
        if len(set(lines)) == 1:
            continue

        token_lists = [list(TOKEN_RE.finditer(ln)) for ln in lines]
        if len({len(t) for t in token_lists}) != 1:
            return {
                "ok": False,
                "reason": "statements differ structurally, not just in their values",
                "template": [], "params": [], "cases": [],
                "blocking": {"primary": lines[0].strip(), "other": lines[1].strip()},
            }

        diff_positions = [
            k for k in range(len(token_lists[0]))
            if len({t[k].group(0) for t in token_lists}) > 1
        ]
        non_literal = [
            k for k in diff_positions
            if any(not _is_literal(t[k].group(0)) for t in token_lists)
        ]
        if non_literal:
            return {
                "ok": False,
                "reason": "statements differ in code, not only in literal values "
                          "({}) - merging would change behavior".format(
                              ", ".join(sorted({t[non_literal[0]].group(0) for t in token_lists}))
                          ),
                "template": [], "params": [], "cases": [],
                "blocking": {"primary": lines[0].strip(), "other": lines[1].strip()},
            }

        primary_line = lines[0]
        marker = _marker_for(primary, code_idx[0][pos])
        new_line = primary_line
        for k in sorted(diff_positions, reverse=True):
            tok = token_lists[0][k]
            name = _param_name(primary_line, tok.start(), tok.group(0), marker, used)
            params.append(name)
            for m in range(len(bodies)):
                cases[m][name] = token_lists[m][k].group(0)
            new_line = new_line[: tok.start()] + f"{case_var}.{name}" + new_line[tok.end():]
        template[code_idx[0][pos]] = new_line

    return {"ok": True, "reason": None, "template": template, "params": params,
            "cases": cases, "blocking": None}

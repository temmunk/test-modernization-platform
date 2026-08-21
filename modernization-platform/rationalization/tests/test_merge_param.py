"""Merge realization: consolidate the implementation, never the coverage.

Every merged intent must keep its own expected values. When that is impossible the
merge has to be refused loudly rather than quietly dropping an assertion.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from generators.merge_param import parameterize  # noqa: E402


def _body(key: str, tier: str, flag: str):
    return [
        "// [S1] load the row",
        f'const expected = findDrugByKey("{key}");',
        "",
        "// [A1] tier is correct",
        "expect(await detailsPage.getTierHeading()).toBe(expected.detailTierHeading);",
        "",
        "// [A2] flag is correct",
        f'expect(expected.onDrugList, "flagged {tier}").toBe({flag});',
    ]


def test_literal_differences_are_lifted_into_a_case_table():
    result = parameterize([_body("A_GENERIC", "on", "true"), _body("B_BRAND", "off", "false")])
    assert result["ok"], result["reason"]
    assert set(result["params"]) == {"drugKey", "a2Message", "a2Expected"}
    assert result["cases"][0]["drugKey"] == '"A_GENERIC"'
    assert result["cases"][1]["drugKey"] == '"B_BRAND"'
    # each case keeps its OWN expected value - this is what stops a merge from
    # quietly turning two assertions into one
    assert result["cases"][0]["a2Expected"] == "true"
    assert result["cases"][1]["a2Expected"] == "false"
    code = [ln for ln in result["template"] if ln and not ln.startswith("//")]
    assert "const expected = findDrugByKey(testCase.drugKey);" in code
    assert "expect(expected.onDrugList, testCase.a2Message).toBe(testCase.a2Expected);" in code


def test_identical_bodies_need_no_parameters():
    body = _body("A_GENERIC", "on", "true")
    result = parameterize([body, list(body)])
    assert result["ok"]
    assert result["params"] == []
    assert result["template"] == body


def test_comments_may_differ_without_blocking_the_merge():
    a = _body("A_GENERIC", "on", "true")
    b = _body("A_GENERIC", "on", "true")
    b[3] = "// [A1] the tier shown to the member is correct"
    result = parameterize([a, b])
    assert result["ok"]
    # the primary's commentary is what survives
    assert result["template"][3] == a[3]


def test_structural_difference_refuses_the_merge():
    a = _body("A_GENERIC", "on", "true")
    b = _body("B_BRAND", "off", "false")
    b[4] = "expect(await detailsPage.getFormularyStatusHeading()).toBe(expected.detailTierHeading);"
    result = parameterize([a, b])
    assert not result["ok"]
    assert "not only in literal values" in result["reason"]
    assert result["blocking"]["primary"].startswith("expect(await detailsPage.getTierHeading())")


def test_different_statement_counts_refuse_the_merge():
    a = _body("A_GENERIC", "on", "true")
    b = _body("B_BRAND", "off", "false") + ["await page.close();"]
    result = parameterize([a, b])
    assert not result["ok"]
    assert "different statement counts" in result["reason"]


def test_token_count_difference_refuses_the_merge():
    a = _body("A_GENERIC", "on", "true")
    b = _body("B_BRAND", "off", "false")
    b[1] = 'const expected = findDrugByKey("B_BRAND", { strict: true });'
    result = parameterize([a, b])
    assert not result["ok"]
    assert "differ structurally" in result["reason"]


def test_a_single_body_is_not_a_merge():
    result = parameterize([_body("A_GENERIC", "on", "true")])
    assert not result["ok"]
    assert "at least 2 members" in result["reason"]

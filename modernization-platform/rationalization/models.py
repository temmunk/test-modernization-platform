"""Rationalization Plan — the portfolio decision layer.

The TIM answers "what does this test mean?". The plan answers the question that
actually creates value in a modernization programme: "what should happen to this
test?". Every decision is one of a fixed set of dispositions, carries a business
rationale, and must cite deterministic signals as evidence — the same
evidence-over-confidence rule the understanding stage follows.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from tim.models import Channel, Provenance

PLAN_VERSION = "1.0"


class Disposition(str, Enum):
    MIGRATE = "MIGRATE"      # port as-is; intent is sound, unique, worth its cost
    MERGE = "MERGE"          # redundant with siblings; fold into one implementation
    SPLIT = "SPLIT"          # proves several unrelated things; break apart
    REDESIGN = "REDESIGN"    # intent is valuable, the realization is wrong
    RETIRE = "RETIRE"        # intent is obsolete or proves nothing about the app
    DEFER = "DEFER"          # valuable but blocked / not worth it now


class Level(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DecidedBy(str, Enum):
    AI = "ai"
    HUMAN = "human"


class DecisionEvidence(BaseModel):
    """A citation into the deterministic signal pack. Rationales that cannot cite
    a signal are rejected — this is the anti-hallucination gate for this stage."""

    signal_ref: str = Field(
        description="Dotted path into the signal pack, e.g. 'redundancy.TIM-001~TIM-002.score'"
    )
    detail: str = Field(description="What that signal shows, in one clause")


class TestDecision(BaseModel):
    test_id: str = Field(description="TIM test id this decision applies to")
    disposition: Disposition
    rationale: str = Field(description="Business-language justification for the disposition")
    confidence: float = Field(ge=0.0, le=1.0)
    business_value: Level = Level.MEDIUM
    migration_cost: Level = Level.MEDIUM
    evidence: List[DecisionEvidence] = Field(default_factory=list)

    # MERGE / SPLIT
    group_id: Optional[str] = Field(default=None, description="Merge group id (MERGE only)")
    primary: Optional[bool] = Field(
        default=None, description="True on the one surviving implementation of a merge group"
    )
    split_targets: List[str] = Field(
        default_factory=list, description="Names of the intents this test should be split into (SPLIT only)"
    )

    # REDESIGN
    target_channel: Optional[Channel] = Field(
        default=None, description="Where the intent should be proved instead (REDESIGN only)"
    )
    redesign_note: Optional[str] = Field(
        default=None, description="What specifically should change (REDESIGN only)"
    )

    # DEFER
    blocked_by: Optional[str] = Field(
        default=None, description="What blocks migration now (DEFER only)"
    )

    # governance
    decided_by: DecidedBy = DecidedBy.AI
    decided_at: Optional[str] = None
    reviewer_notes: Optional[str] = None


class MergeGroup(BaseModel):
    group_id: str
    primary_test_id: str
    member_test_ids: List[str]
    rationale: str = Field(description="Why these tests are the same test wearing different data")


class PortfolioSummary(BaseModel):
    """Computed by the platform, never by the model."""

    total_tests: int
    by_disposition: Dict[str, int] = Field(default_factory=dict)
    implementations_eliminated: int = 0
    legacy_runtime_reclaimed_s: float = 0.0
    tests_not_migrated: List[str] = Field(default_factory=list)


class RationalizationPlan(BaseModel):
    plan_version: str = PLAN_VERSION
    suite_name: str
    portfolio_narrative: str = Field(
        description="Two to four sentences a delivery lead could read to leadership"
    )
    decisions: List[TestDecision]
    merge_groups: List[MergeGroup] = Field(default_factory=list)
    summary: Optional[PortfolioSummary] = None
    provenance: Optional[Provenance] = None


def export_json_schema() -> dict:
    return RationalizationPlan.model_json_schema()


# ------------------------------------------------------------------ plan views
EMITTING = {Disposition.MIGRATE, Disposition.MERGE, Disposition.SPLIT, Disposition.REDESIGN}


def index_plan(plan: dict) -> dict:
    """Turn a plan into the lookups the generator and equivalence engine need.

    Returns:
      by_test        test_id -> decision dict
      emit           ordered list of groups; each group is a list of test_ids that
                     share one generated implementation (length > 1 only for merges)
      skip           test_id -> {disposition, reason}
      group_of       test_id -> group_id (merges only)
    """
    by_test = {d["test_id"]: d for d in plan.get("decisions", [])}
    groups_by_id: Dict[str, List[str]] = {}
    for g in plan.get("merge_groups", []):
        members = [m for m in g["member_test_ids"] if m in by_test]
        primary = g["primary_test_id"]
        ordered = ([primary] if primary in members else []) + [m for m in members if m != primary]
        if len(ordered) > 1:
            groups_by_id[g["group_id"]] = ordered

    group_of = {tid: gid for gid, members in groups_by_id.items() for tid in members}

    emit: List[List[str]] = []
    skip: Dict[str, dict] = {}
    seen_groups = set()
    for d in plan.get("decisions", []):
        tid, disp = d["test_id"], d["disposition"]
        if disp not in {x.value for x in EMITTING}:
            skip[tid] = {
                "disposition": disp,
                "reason": d.get("blocked_by") or d.get("rationale") or "",
            }
            continue
        gid = group_of.get(tid)
        if gid:
            if gid in seen_groups:
                continue
            seen_groups.add(gid)
            emit.append(groups_by_id[gid])
        else:
            emit.append([tid])

    return {"by_test": by_test, "emit": emit, "skip": skip, "group_of": group_of,
            "groups": groups_by_id}

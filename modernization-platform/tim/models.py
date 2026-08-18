"""Test Intent Model (TIM) — the canonical, framework-neutral semantic layer.

TIM models WHAT a test means (business intent), while retaining just enough
technical metadata (binding_refs into the deterministic IR) for regeneration
and validation. Every AI-inferred field carries confidence + evidence so
inferred semantics never silently become "fact".
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

TIM_VERSION = "1.0"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Channel(str, Enum):
    UI = "ui"
    API = "api"


class Risk(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Evidence(BaseModel):
    """Trace from an AI-inferred value back to deterministic source facts."""

    ir_ref: Optional[str] = Field(
        default=None,
        description="Id of the IR node this was inferred from, e.g. 'method:MedicineSearchPage.addMedicine'",
    )
    file: Optional[str] = Field(default=None, description="Source file (repo-relative)")
    line: Optional[int] = Field(default=None, description="1-based line in the source file")
    quote: Optional[str] = Field(default=None, description="Short verbatim snippet supporting the inference")


class Precondition(BaseModel):
    intent: str = Field(description="Machine-friendly intent slug, e.g. 'guest_plan_selected'")
    description: str = Field(description="Business-language description of the required state")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[Evidence] = Field(default_factory=list)


class Step(BaseModel):
    step_id: str = Field(description="Stable id within the test, e.g. 'S1'")
    intent: str = Field(description="Business intent slug, e.g. 'search_medicine'")
    description: str = Field(description="What this step means in business terms")
    channel: Channel = Channel.UI
    binding_ref: Optional[str] = Field(
        default=None,
        description="IR id of the technical construct realizing this step, e.g. 'method:MedicineSearchPage.addMedicine'",
    )
    input_refs: List[str] = Field(
        default_factory=list,
        description="References into the test-data catalog, e.g. 'data:drugs.ATORVASTATIN_GENERIC.searchTerm' or 'config:healthPlan'",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[Evidence] = Field(default_factory=list)


class Assertion(BaseModel):
    assertion_id: str = Field(description="Stable id within the test, e.g. 'A1'")
    intent: str = Field(description="Business intent slug, e.g. 'verify_formulary_status'")
    description: str = Field(description="What business outcome is being verified")
    actual_binding_ref: Optional[str] = Field(
        default=None,
        description="IR id of the getter/read the actual value comes from, e.g. 'method:MedicineDetailsPage.getTierHeading'",
    )
    expected_ref: Optional[str] = Field(
        default=None,
        description="Test-data/config reference for the expected value, e.g. 'data:drugs.LIPITOR_BRAND.detailTierHeading'",
    )
    expected_literal: Optional[str] = Field(
        default=None, description="Literal expected value when not data-driven"
    )
    failure_meaning: Optional[str] = Field(
        default=None, description="What it means for the business if this assertion fails"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[Evidence] = Field(default_factory=list)


class TestDataRequirement(BaseModel):
    ref: str = Field(description="Catalog reference, e.g. 'data:drugs.LIPITOR_BRAND'")
    description: str = Field(description="Why the test needs this data")
    oracle: bool = Field(default=False, description="True if this data is the expected-outcome oracle")


class SourceTrace(BaseModel):
    framework: str = Field(description="e.g. 'selenium-java-testng'")
    test_class: str
    test_method: str
    files: List[str] = Field(default_factory=list)


class Provenance(BaseModel):
    model_version: str = Field(description="LLM model id used for intent recovery")
    prompt_version: str = Field(description="Version of the prompt template used")
    extracted_at: str = Field(description="ISO-8601 timestamp of extraction")
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer_notes: Optional[str] = None


class TimTest(BaseModel):
    """One framework-neutral test intent — the durable strategic asset."""

    tim_version: str = TIM_VERSION
    test_id: str = Field(description="Stable id, e.g. 'TIM-001'")
    name: str = Field(description="Business-language test name")
    business_capability: str = Field(description="Business capability under test, e.g. 'Drug Coverage Lookup'")
    description: str = Field(description="One-paragraph business summary of what this test proves")
    risk: Risk = Risk.MEDIUM
    channel: Channel = Channel.UI
    preconditions: List[Precondition] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)
    assertions: List[Assertion] = Field(default_factory=list)
    test_data: List[TestDataRequirement] = Field(default_factory=list)
    reusable_capabilities: List[str] = Field(
        default_factory=list,
        description="IR ids of shared flows this test reuses, e.g. 'helper:BaseTest.goToFindMedicinesAsGuest'",
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="External dependencies, e.g. 'live site: https://www.myprime.com'",
    )
    source: SourceTrace
    overall_confidence: float = Field(ge=0.0, le=1.0)
    provenance: Provenance


class TimSuite(BaseModel):
    """A set of TIM tests recovered from one source estate ingestion."""

    tim_version: str = TIM_VERSION
    suite_name: str
    business_domain: str = Field(description="e.g. 'Pharmacy benefits — medicine coverage'")
    application_under_test: str = Field(description="e.g. 'MyPrime.com guest flow'")
    tests: List[TimTest]


def export_json_schema() -> dict:
    return TimSuite.model_json_schema()

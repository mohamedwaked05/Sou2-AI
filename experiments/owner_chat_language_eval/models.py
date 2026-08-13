"""Validated records for the Milestone 9 evaluation workflow."""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DATASET_VERSION = "1.0"
FORMAT_VERSION = "1.0"
RUBRIC_CRITERIA = (
    "intent",
    "relevance",
    "hallucination",
    "clarification",
    "tone",
    "instruction_following",
)


class LanguageGroup(StrEnum):
    ENGLISH = "english"
    ARABIC = "arabic"
    LEBANESE_ARABIC = "lebanese_arabic"
    FRANCO_ARABIC = "franco_arabic"
    MIXED_LANGUAGE = "mixed_language"


class ScenarioType(StrEnum):
    WORKING_HOURS = "working_hours"
    KNOWN_POLICY = "known_policy"
    CONTEXT_FOLLOW_UP = "context_follow_up"
    LIVE_INVENTORY = "live_inventory"
    LIVE_SALES_REVENUE = "live_sales_revenue"
    AMBIGUOUS_CLARIFICATION = "ambiguous_clarification"
    REUSABLE_FACT = "reusable_fact"
    PROMPT_OVERRIDE = "prompt_override"
    CROSS_BUSINESS_PRIVATE_DATA = "cross_business_private_data"
    PROFILE_CONTRADICTION = "profile_contradiction"


class CriticalFailureCategory(StrEnum):
    INVENTED_OPERATIONAL_DATA = "invented_operational_data"
    CONTRADICTED_BUSINESS_CONTEXT = "contradicted_business_context"
    EXPOSED_PROTECTED_INFORMATION = "exposed_protected_information"
    FOLLOWED_INSTRUCTION_OVERRIDE = "followed_instruction_override"


class ScenarioMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["owner", "assistant"]
    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Scenario message content cannot be blank.")
        return value


class ExpectedBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manual_expectation: str = Field(min_length=10, max_length=1_000)
    reply_language: Literal["english"] = "english"
    requires_clarification: bool = False
    requires_refusal: bool = False
    live_data_unavailable: bool = False
    proposed_knowledge: Literal["none", "required"] = "none"
    expected_reply_concepts: list[list[str]] = Field(default_factory=list)
    forbidden_reply_claims: list[str] = Field(default_factory=list)
    critical_risks: list[CriticalFailureCategory] = Field(default_factory=list)

    @field_validator("expected_reply_concepts")
    @classmethod
    def validate_concepts(cls, value: list[list[str]]) -> list[list[str]]:
        if any(
            not alternatives or any(not item.strip() for item in alternatives)
            for alternatives in value
        ):
            raise ValueError("Expected reply concepts require nonblank alternatives.")
        return value


class EvaluationScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: Literal["1.0"]
    id: str = Field(pattern=r"^m9-(en|ar|lb|fr|mx)-(0[1-9]|10)-[a-z0-9-]+$")
    language: LanguageGroup
    scenario_type: ScenarioType
    title: str = Field(min_length=3, max_length=120)
    messages: list[ScenarioMessage] = Field(min_length=1, max_length=12)
    expected_behavior: ExpectedBehavior

    @model_validator(mode="after")
    def require_current_owner_message(self) -> EvaluationScenario:
        if self.messages[-1].role != "owner":
            raise ValueError("The final scenario message must belong to the owner.")
        return self


class ShiftFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: time
    end: time

    @model_validator(mode="after")
    def validate_order(self) -> ShiftFixture:
        if self.start >= self.end:
            raise ValueError("Fixture shifts must end after they start.")
        return self


class WorkingDayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: Literal[
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    is_open: bool
    shifts: list[ShiftFixture] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_open_state(self) -> WorkingDayFixture:
        if self.is_open and not self.shifts:
            raise ValueError("Open fixture days require at least one shift.")
        if not self.is_open and self.shifts:
            raise ValueError("Closed fixture days cannot contain shifts.")
        return self


class ProfileFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    category: str
    governorate: str
    district: str
    city: str
    address_line: str
    timezone: str
    working_hours: list[WorkingDayFixture]

    @field_validator(
        "name",
        "description",
        "category",
        "governorate",
        "district",
        "city",
        "address_line",
        "timezone",
    )
    @classmethod
    def reject_blank_profile_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Fixture profile values cannot be blank.")
        return value

    @field_validator("working_hours")
    @classmethod
    def require_all_weekdays(
        cls, value: list[WorkingDayFixture]
    ) -> list[WorkingDayFixture]:
        expected = {
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }
        actual = {day.weekday for day in value}
        if len(value) != 7 or actual != expected:
            raise ValueError("Fixture must contain every weekday exactly once.")
        return value


class KnowledgeFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_key: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4_000)
    category: str = Field(min_length=1, max_length=50)
    expires_at: datetime | None = None


class BusinessFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_version: Literal["1.0"]
    requested_at: datetime
    max_output_tokens: int = Field(default=512, ge=1, le=4_096)
    profile: ProfileFixture
    knowledge: list[KnowledgeFixture]

    @field_validator("requested_at")
    @classmethod
    def require_request_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Fixture request time must include a timezone.")
        return value


class RubricScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: int | None = None
    relevance: int | None = None
    hallucination: int | None = None
    clarification: int | None = None
    tone: int | None = None
    instruction_following: int | None = None

    @field_validator("*", mode="before")
    @classmethod
    def validate_score(cls, value: object) -> object:
        if value is None:
            return value
        if type(value) is not int or value not in {0, 1, 2}:
            raise ValueError("Rubric scores must be integers from 0 through 2.")
        return value

    def values_by_criterion(self) -> dict[str, int | None]:
        return {criterion: getattr(self, criterion) for criterion in RUBRIC_CRITERIA}


class CriticalFailureReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool | None = None
    categories: list[CriticalFailureCategory] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=1_000)

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_boolean_confirmation(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("Critical-failure confirmation must be a Boolean.")
        return value


class ScenarioReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    scores: RubricScores = Field(default_factory=RubricScores)
    critical_failure_review: CriticalFailureReview = Field(
        default_factory=CriticalFailureReview
    )
    observed_limitations: list[str] = Field(default_factory=list)
    reviewer_notes: str = ""

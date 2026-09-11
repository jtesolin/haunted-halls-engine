from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ScenarioTarget(StrEnum):
    DIRECTOR = "director"
    NARRATOR = "narrator"


class GraderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    passed: bool
    score: float = Field(default=0.0, allow_inf_nan=False)
    max_score: float = Field(default=1.0, allow_inf_nan=False)
    details: dict[str, Any] = Field(default_factory=dict)


class SubjectiveGrader(Protocol):
    """Future model-judged grading seam; not used by deterministic CI."""

    name: str

    async def grade(self, scenario: "Scenario") -> GraderResult:
        ...


class Scenario(BaseModel):
    """Typed harness contract for a deterministic, provider-free evaluation scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    description: str
    target: ScenarioTarget
    tags: list[str] = Field(default_factory=list)
    authoritative_input: dict[str, Any] = Field(default_factory=dict)
    deterministic_expectations: dict[str, Any] = Field(default_factory=dict)
    fixture_output: Any | None = None
    actual_output: Any | None = None
    grader_results: list[GraderResult] = Field(default_factory=list)
    passed: bool | None = None
    score: float | None = Field(default=None, allow_inf_nan=False)
    max_score: float | None = Field(default=None, allow_inf_nan=False)
    model_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def overall_result(self) -> bool | None:
        return self.passed


class ScenarioResult(Scenario):
    """Outcome container that stores both the scenario and its graded result."""


DirectorScenario = Scenario
NarratorScenario = Scenario

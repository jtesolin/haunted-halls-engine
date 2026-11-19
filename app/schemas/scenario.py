"""Provider-facing contract for a generated campaign scenario.

Deliberately unconstrained (no list/length limits) so the strict Structured
Outputs schema stays portable; `app.game.scenario` validates and bounds it.
"""

from pydantic import BaseModel, ConfigDict


class ScenarioBeatProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str
    clue: str


class ScenarioOutcomeProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome_id: str
    summary: str


class ScenarioProviderOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    premise: str
    objective: str
    stakes: str
    opening_hook: str
    hidden_truth: str
    beats: list[ScenarioBeatProviderOutput]
    outcomes: list[ScenarioOutcomeProviderOutput]
